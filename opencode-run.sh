#!/usr/bin/env bash
# opencode-run.sh — build opencode-analysis image if needed, then run it
#
# Usage:
#   ./opencode-run.sh [OPTIONS] [WORKSPACE]
#
# Options:
#   --oel 9|10           OEL version to build/run (default: 9)
#   --config DIR         Path to config directory, mounted as OPENCODE_CONFIG_DIR
#   --logs DIR           Base directory for run logs (default: ~/opencode-analysis-logs)
#   --rebuild            Force image rebuild even if it exists
#   --no-build           Never build; fail if image is missing
#   --mouse              Enable mouse support
#   --no-mouse           Disable mouse support (default)
#   --dangerously-open   Disable analysis-mode restrictions (for personal/open-source use)
#   --                   Pass remaining args through to opencode
#
# Examples:
#   ./opencode-run.sh .                                                  # analysis mode (default)
#   ./opencode-run.sh --oel 10 /path/to/project
#   ./opencode-run.sh --config ~/private/opencode-config /path/to/project
#   ./opencode-run.sh --rebuild --config ~/private/opencode-config .
#   ./opencode-run.sh --mouse .
#   ./opencode-run.sh --logs /mnt/audit-store .
#   ./opencode-run.sh --dangerously-open .                               # open mode — your own projects
#   ./opencode-run.sh --dangerously-open --config ~/my-config .          # fully open, custom LLM config
#   ./opencode-run.sh -- serve --port 4096
#
# Modes:
#   Default (analysis) mode — safe for running against untrusted or customer code:
#     - Subprocess spawns sandboxed in a network namespace (bash tool cannot exfiltrate)
#     - Session sharing disabled (no upload to opncd.ai)
#     - Auto-update disabled (no outbound version check)
#     - LSP binary download disabled
#     - security.mode: internal-only in baked-in config (webfetch/websearch/provider
#       validated; only localhost and private IPs allowed unless --config overrides)
#
#   --dangerously-open mode — for your own open-source projects or personal use:
#     Unsets the ENV-based restrictions above so the image behaves closer to upstream.
#     The baked-in config (security.mode: internal-only) still applies unless you also
#     pass --config with a config that sets a different security mode.
#
# Logs:
#   A timestamped subdirectory is automatically created under LOG_BASE for each run
#   and bind-mounted over the container's opencode data directory.  Preserves
#   audit.jsonl, application logs (log/), and the session sqlite database even
#   though the container runs with --rm.  The LLM only has access to /workspace.
#
#   Log path printed to stderr at startup:
#     Run logs: /home/user/opencode-analysis-logs/20260304-143000-12345/
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OEL_VERSION=9
REBUILD=0
NO_BUILD=0
CONFIG_DIR=""
WORKSPACE=""
EXTRA_ARGS=()
DISABLE_MOUSE="${OPENCODE_DISABLE_MOUSE:-1}"
LOG_BASE="${OPENCODE_ANALYSIS_LOG_DIR:-${HOME}/opencode-analysis-logs}"
DANGEROUSLY_OPEN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --oel)              OEL_VERSION="$2"; shift 2 ;;
        --config)           CONFIG_DIR="$2"; shift 2 ;;
        --logs)             LOG_BASE="$2"; shift 2 ;;
        --rebuild)          REBUILD=1; shift ;;
        --no-build)         NO_BUILD=1; shift ;;
        --mouse)            DISABLE_MOUSE=0; shift ;;
        --no-mouse)         DISABLE_MOUSE=1; shift ;;
        --dangerously-open) DANGEROUSLY_OPEN=1; shift ;;
        --)                 shift; EXTRA_ARGS+=("$@"); break ;;
        -*)                 echo "Unknown option: $1" >&2; exit 1 ;;
        *)                  WORKSPACE="$1"; shift ;;
    esac
done

# Default workspace to current directory
WORKSPACE="${WORKSPACE:-${PWD}}"
IMAGE="opencode-analysis:oel${OEL_VERSION}"

# ─── LOG DIR ─────────────────────────────────────────────────────────────────
# Per-run timestamped directory on the host, bind-mounted over the container's
# opencode data dir (~/.local/share/opencode).  Captures audit.jsonl, log/,
# and the session sqlite database.  Created before the container starts so
# Docker mounts a host-owned directory (avoids root-owned mount target).
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_DIR="${LOG_BASE}/${RUN_ID}"
mkdir -p "${LOG_DIR}/log" "${LOG_DIR}/bin"
echo "Run logs: ${LOG_DIR}" >&2

# ─── BUILD ───────────────────────────────────────────────────────────────────
IMAGE_EXISTS=0
docker image inspect "$IMAGE" &>/dev/null && IMAGE_EXISTS=1

if [[ "$NO_BUILD" -eq 1 && "$IMAGE_EXISTS" -eq 0 ]]; then
    echo "Error: image $IMAGE not found and --no-build is set" >&2
    exit 1
fi

if [[ "$REBUILD" -eq 1 ]] || [[ "$IMAGE_EXISTS" -eq 0 ]]; then
    echo "Building $IMAGE ..."
    docker build -t "$IMAGE" \
        -f "${SCRIPT_DIR}/Dockerfile.analysis" \
        --build-arg OEL_VERSION="${OEL_VERSION}" \
        "${SCRIPT_DIR}"
fi

# ─── RUN ─────────────────────────────────────────────────────────────────────
RUN_ARGS=(
    --rm -it
    --user "$(id -u):$(id -g)"
    -v "$(realpath "$WORKSPACE"):/workspace"
    -v "${LOG_DIR}:/home/coder/.local/share/opencode"
    -e "OPENCODE_DISABLE_MOUSE=${DISABLE_MOUSE}"
)

# --dangerously-open: override the ENV-based analysis-mode restrictions.
# The baked-in security.mode:internal-only config still applies unless
# --config provides a replacement.
if [[ "$DANGEROUSLY_OPEN" -eq 1 ]]; then
    echo "WARNING: --dangerously-open: analysis-mode restrictions disabled" >&2
    RUN_ARGS+=(
        -e OPENCODE_SPAWN_SANDBOX=0
        -e OPENCODE_DISABLE_SHARE=0
        -e OPENCODE_DISABLE_AUTOUPDATE=0
        -e OPENCODE_DISABLE_LSP_DOWNLOAD=0
        -e OPENCODE_DISABLE_PROJECT_CONFIG=0
    )
fi

# Inject private config directory if provided
if [[ -n "$CONFIG_DIR" ]]; then
    RUN_ARGS+=(-v "$(realpath "$CONFIG_DIR"):/run/opencode-config:ro")
    RUN_ARGS+=(-e OPENCODE_CONFIG_DIR=/run/opencode-config)
fi

# Pass through common API key env vars if set in the calling environment
for var in ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY GEMINI_API_KEY; do
    [[ -n "${!var:-}" ]] && RUN_ARGS+=(-e "$var")
done

exec docker run "${RUN_ARGS[@]}" "$IMAGE" "${EXTRA_ARGS[@]}"
