#!/usr/bin/env bash
# Run the opencode test suite in batches, each batch in its own bun process.
# Avoids the single-process resource accumulation that makes the heavy
# socket/LSP/PTY suites cascade-fail when all 242 files share one event loop.
#
#   - umask 0022 so file-permission tests match upstream/CI expectations
#   - separate process per batch: memory + handles reset, no 9.2GB climb
#   - --no-orphans: SIGKILL leaked descendant subprocesses on each batch exit
#   - --timeout: headroom for inherently slow websocket/auth handshakes
#
# Light dirs run as one batch (the whole dir in one process). Heavy dirs that
# bind real HTTP/websocket/PTY/MCP servers run file-by-file: 48 socket-binding
# files in one process re-create the saturation, so each file gets its own
# process. Empirically this took test/server from 31 fails to 0.
#
# Usage: script/test-batched.sh [timeout_ms]
set -u
umask 0022

cd "$(dirname "$0")/.." || exit 2
TIMEOUT="${1:-20000}"

# Dirs whose tests spawn servers/sockets/subprocesses and must run per-file.
HEAVY=" server session project mcp acp pty "

fail_batches=()
flaky_batches=()
run() { # run() <label> <path...>
  local label="$1"; shift
  echo "=============================================================="
  echo "### $label"
  echo "=============================================================="
  bun test --no-orphans --timeout="$TIMEOUT" "$@" && return 0
  # Retry once. An intermittent Effect interrupt-squash on instance disposal
  # ("All fibers interrupted without error") surfaces as an inter-test error
  # with zero assertion failures, and bun exits non-zero for it. A real failure
  # reproduces on the retry; a teardown squash clears. Flaky passes are reported
  # separately so they are never silently masked.
  echo "--- $label failed; retrying once ---"
  if bun test --no-orphans --timeout="$TIMEOUT" "$@"; then
    flaky_batches+=("$label")
    return 0
  fi
  fail_batches+=("$label")
}

for d in test/*/; do
  [ -n "$(find "$d" -name '*.test.ts' -print -quit)" ] || continue
  name="$(basename "$d")"
  if [[ "$HEAVY" == *" $name "* ]]; then
    while IFS= read -r f; do run "$f" "$f"; done < <(find "$d" -name '*.test.ts' | sort)
    continue
  fi
  run "$d" "$d"
done

echo "=============================================================="
if [ ${#flaky_batches[@]} -gt 0 ]; then
  printf 'FLAKY BATCHES — passed on retry (%d):\n' "${#flaky_batches[@]}"
  printf '  %s\n' "${flaky_batches[@]}"
fi
if [ ${#fail_batches[@]} -eq 0 ]; then
  echo "ALL BATCHES PASSED"
  exit 0
fi
printf 'FAILED BATCHES (%d):\n' "${#fail_batches[@]}"
printf '  %s\n' "${fail_batches[@]}"
exit 1
