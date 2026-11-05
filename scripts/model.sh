#!/usr/bin/env bash
# Run a command inside the Linux model container (D13), e.g.:
#   scripts/model.sh uv run python -m adatta.model.timing
# The repo is mounted at /app; settings read /app/.env themselves (docker --env-file
# would keep quotes around values).
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=adatta-model
if [[ "${REBUILD:-0}" == 1 ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -q -f docker/model.Dockerfile -t "$IMAGE" . >/dev/null
fi
exec docker run --rm -v "$PWD":/app -e UV_PROJECT_ENVIRONMENT=/opt/venv \
  "$IMAGE" "$@"
