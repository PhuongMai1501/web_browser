#!/bin/bash
# start_api.sh — Start FastAPI API server
# Usage: ./start_api.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_ENV="/root/miniconda3/envs/tool_web"
cd "$SCRIPT_DIR"

# Load environment
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
  echo "[API] Loaded .env"
else
  echo "[API] ERROR: .env not found."
  exit 1
fi

mkdir -p "${ARTIFACTS_ROOT:-$SCRIPT_DIR/LLM_base/artifacts}"
mkdir -p "${LOG_DIR:-$SCRIPT_DIR/logs}/system"

export PYTHONPATH="$SCRIPT_DIR/ai_tool_web:${PYTHONPATH:-}"

echo "[API] Starting FastAPI on port 9000..."
cd "$SCRIPT_DIR/ai_tool_web"
exec "$CONDA_ENV/bin/uvicorn" api.app:app \
  --host 0.0.0.0 \
  --port 9000 \
  --workers 1 \
  --log-level info
