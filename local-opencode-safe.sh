#!/usr/bin/env bash
set -euo pipefail

# local-opencode-safe.sh
#
# Purpose:
# - Launch local OpenCode like opencode-local does.
# - Add automatic safety snapshots (tracked + untracked files).
# - Optionally guard destructive git subcommands during the OpenCode session.
#
# Default behavior:
# 1) Merge upstream + origin updates (same style as opencode-local).
# 2) Build packages/opencode.
# 3) Save a safety snapshot.
# 4) Start an auto-snapshot loop (every 300s).
# 5) Run OpenCode with a guarded PATH that blocks risky git commands.
#
# Snapshot files are written under:
#   .scripts/opencode-safety/
# (this directory is ignored by .gitignore in this repo)

usage() {
  cat <<'EOF'
Usage:
  ./local-opencode-safe.sh [wrapper options] [-- opencode args]

Wrapper options:
  --no-update            Skip upstream/origin merge step.
  --no-build             Skip build step.
  --no-guard             Do not guard git subcommands in this run.
  --no-snapshot-loop     Take one snapshot at start only (no periodic snapshots).
  --snapshot-interval N  Auto-snapshot interval in seconds (default: 300).
  --snapshot-only        Create a snapshot and exit.
  --mouse                Enable mouse support (OPENCODE_DISABLE_MOUSE=0).
  --no-mouse             Disable mouse support (OPENCODE_DISABLE_MOUSE=1, default).
  --help                 Show this help.

Environment overrides:
  OPENCODE_SAFE_ALLOW_GIT_DESTRUCTIVE=1
    Allows blocked git subcommands for this run.
  OPENCODE_SAFE_BLOCKED_GIT_SUBCOMMANDS
    Space-separated list of blocked subcommands.
    Default: "reset checkout restore stash clean rebase merge"

Examples:
  ./local-opencode-safe.sh
  ./local-opencode-safe.sh --no-update -- --continue
  ./local-opencode-safe.sh --snapshot-only
  OPENCODE_SAFE_ALLOW_GIT_DESTRUCTIVE=1 ./local-opencode-safe.sh --no-guard

Recovery notes:
  - Tracked changes snapshot: .scripts/opencode-safety/<ts>.tracked.patch
      git apply .scripts/opencode-safety/<ts>.tracked.patch
  - Untracked files snapshot: .scripts/opencode-safety/<ts>.untracked.tgz
      tar -xzf .scripts/opencode-safety/<ts>.untracked.tgz
EOF
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

update_repo=1
build_project=1
use_guard=1
use_snapshot_loop=1
snapshot_interval=300
snapshot_only=0
disable_mouse="${OPENCODE_DISABLE_MOUSE:-1}"
declare -a opencode_args=()

while (($#)); do
  case "$1" in
    --no-update)
      update_repo=0
      shift
      ;;
    --no-build)
      build_project=0
      shift
      ;;
    --no-guard)
      use_guard=0
      shift
      ;;
    --no-snapshot-loop)
      use_snapshot_loop=0
      shift
      ;;
    --snapshot-interval)
      shift
      snapshot_interval="${1:-}"
      if [[ -z "$snapshot_interval" || ! "$snapshot_interval" =~ ^[0-9]+$ || "$snapshot_interval" -lt 30 ]]; then
        echo "Invalid --snapshot-interval. Use an integer >= 30 seconds." >&2
        exit 1
      fi
      shift
      ;;
    --snapshot-only)
      snapshot_only=1
      shift
      ;;
    --mouse)
      disable_mouse=0
      shift
      ;;
    --no-mouse)
      disable_mouse=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      opencode_args+=("$@")
      break
      ;;
    *)
      opencode_args+=("$1")
      shift
      ;;
  esac
done

if [ ${#opencode_args[@]} -eq 0 ] && [ "$snapshot_only" -eq 0 ]; then
  opencode_args=(--continue)
fi

snapshot_dir="$root/.scripts/opencode-safety"
mkdir -p "$snapshot_dir"

snapshot_once() {
  local reason="${1:-manual}"
  local ts
  ts="$(date +%Y%m%d-%H%M%S)"
  local base="$snapshot_dir/${ts}.${reason}"
  local tracked_patch="${base}.tracked.patch"
  local untracked_list="${base}.untracked.list"
  local untracked_tgz="${base}.untracked.tgz"

  git diff --binary HEAD > "$tracked_patch" || true
  git ls-files --others --exclude-standard -z > "$untracked_list" || true

  if [ -s "$untracked_list" ]; then
    tar --null -czf "$untracked_tgz" --files-from "$untracked_list"
  fi
  rm -f "$untracked_list"

  echo "Safety snapshot saved: $tracked_patch"
  if [ -f "$untracked_tgz" ]; then
    echo "Untracked snapshot saved: $untracked_tgz"
  fi
}

if [ "$snapshot_only" -eq 1 ]; then
  snapshot_once "manual"
  exit 0
fi

if [ "$update_repo" -eq 1 ]; then
  echo "Updating repository..."
  branch="$(git rev-parse --abbrev-ref HEAD)"
  if ! git remote get-url upstream >/dev/null 2>&1; then
    git remote add upstream https://github.com/anomalyco/opencode.git
  fi

  git fetch upstream
  upstream_ref="upstream/${branch}"
  if ! git show-ref --verify --quiet "refs/remotes/${upstream_ref}"; then
    upstream_ref="upstream/dev"
  fi

  if ! git merge --no-edit "${upstream_ref}"; then
    if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
      echo "Merge in progress with conflicts."
    else
      echo "git merge ${upstream_ref} failed. Resolve and re-run."
      exit 1
    fi
  fi

  git fetch origin
  origin_ref="origin/${branch}"
  if git show-ref --verify --quiet "refs/remotes/${origin_ref}"; then
    if ! git merge --no-edit "${origin_ref}"; then
      if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
        echo "Merge in progress with conflicts."
      else
        echo "git merge ${origin_ref} failed. Resolve and re-run."
        exit 1
      fi
    fi
  fi

  conflicts="$(git diff --name-only --diff-filter=U || true)"
  if [ -n "$conflicts" ]; then
    echo "Conflicts detected:"
    echo "$conflicts"
    while [ -n "$conflicts" ]; do
      read -rp "Resolve conflicts, then press Enter to continue (Ctrl+C to abort)..." _
      conflicts="$(git diff --name-only --diff-filter=U || true)"
      if [ -n "$conflicts" ]; then
        echo "Still conflicted:"
        echo "$conflicts"
      fi
    done
  fi
fi

if [ "$build_project" -eq 1 ]; then
  echo "Building opencode..."
  bun run --cwd packages/opencode script/build.ts --single
fi

snapshot_once "startup"

snapshot_loop_pid=""
git_guard_dir=""
real_git="$(command -v git)"

cleanup() {
  if [ -n "$snapshot_loop_pid" ]; then
    kill "$snapshot_loop_pid" >/dev/null 2>&1 || true
  fi
  if [ -n "$git_guard_dir" ]; then
    rm -rf "$git_guard_dir"
  fi
}
trap cleanup EXIT INT TERM

if [ "$use_snapshot_loop" -eq 1 ]; then
  (
    while true; do
      sleep "$snapshot_interval"
      snapshot_once "auto"
    done
  ) &
  snapshot_loop_pid="$!"
  echo "Auto-snapshot loop running every ${snapshot_interval}s (pid $snapshot_loop_pid)."
fi

if [ "$use_guard" -eq 1 ]; then
  git_guard_dir="$(mktemp -d "$snapshot_dir/git-guard.XXXXXX")"
  cat > "$git_guard_dir/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

real_git="${OPENCODE_SAFE_REAL_GIT:?OPENCODE_SAFE_REAL_GIT is required}"
blocked="${OPENCODE_SAFE_BLOCKED_GIT_SUBCOMMANDS:-reset checkout restore stash clean rebase merge}"
allow="${OPENCODE_SAFE_ALLOW_GIT_DESTRUCTIVE:-0}"

args=("$@")
sub=""
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  a="${args[$i]}"
  case "$a" in
    -C|-c|--git-dir|--work-tree)
      i=$((i + 2))
      continue
      ;;
    --*|-*)
      i=$((i + 1))
      continue
      ;;
    *)
      sub="$a"
      break
      ;;
  esac
done

if [ -n "$sub" ] && [ "$allow" != "1" ]; then
  for cmd in $blocked; do
    if [ "$sub" = "$cmd" ]; then
      echo "[opencode-safe] blocked git subcommand: $sub" >&2
      echo "[opencode-safe] set OPENCODE_SAFE_ALLOW_GIT_DESTRUCTIVE=1 to allow intentionally." >&2
      exit 2
    fi
  done
fi

exec "$real_git" "$@"
EOF
  chmod +x "$git_guard_dir/git"
  export OPENCODE_SAFE_REAL_GIT="$real_git"
  export OPENCODE_SAFE_BLOCKED_GIT_SUBCOMMANDS="${OPENCODE_SAFE_BLOCKED_GIT_SUBCOMMANDS:-reset checkout restore stash clean rebase merge}"
  export PATH="$git_guard_dir:$PATH"
  echo "Git guard enabled. Blocked: $OPENCODE_SAFE_BLOCKED_GIT_SUBCOMMANDS"
fi

echo "Launching OpenCode..."
OPENCODE_DISABLE_MOUSE="$disable_mouse" ./packages/opencode/dist/opencode-linux-x64/bin/opencode "${opencode_args[@]}"
