#!/usr/bin/env bash
# opencode-run.sh — build opencode-analysis image if needed, then run it
#
# Usage:
#   ./opencode-run.sh [OPTIONS] [WORKSPACE]
#
# Options:
#   --oel 9|10      OEL version to build/run (default: 9)
#   --config DIR    Path to config directory, mounted as OPENCODE_CONFIG_DIR
#   --rebuild       Force image rebuild even if it exists
#   --no-build      Never build; fail if image is missing
#   --mouse         Enable mouse support
#   --no-mouse      Disable mouse support (default)
#   --              Pass remaining args through to opencode
#
# Examples:
#   ./opencode-run.sh .
#   ./opencode-run.sh --oel 10 /path/to/project
#   ./opencode-run.sh --config ~/private/opencode-config /path/to/project
#   ./opencode-run.sh --rebuild --config ~/private/opencode-config .
#   ./opencode-run.sh --mouse .
#   ./opencode-run.sh -- serve --port 4096
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --oel)      OEL_VERSION="$2"; shift 2 ;;
        --config)   CONFIG_DIR="$2"; shift 2 ;;
        --rebuild)  REBUILD=1; shift ;;
        --no-build) NO_BUILD=1; shift ;;
        --mouse)    DISABLE_MOUSE=0; shift ;;
        --no-mouse) DISABLE_MOUSE=1; shift ;;
        --)         shift; EXTRA_ARGS+=("$@"); break ;;
        -*)         echo "Unknown option: $1" >&2; exit 1 ;;
        *)          WORKSPACE="$1"; shift ;;
    esac
done

# Default workspace to current directory
WORKSPACE="${WORKSPACE:-${PWD}}"
IMAGE="opencode-analysis:oel${OEL_VERSION}"

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
    -e "OPENCODE_DISABLE_MOUSE=${DISABLE_MOUSE}"
)

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
