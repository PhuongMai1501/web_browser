#!/bin/bash
# start_worker.sh — Start browser workers
# Usage: ./start_worker.sh [count]
# Example: ./start_worker.sh 50   (default: 1)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_ENV="/root/miniconda3/envs/browser_local"
cd "$SCRIPT_DIR"

COUNT="${1:-1}"

# Load environment
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
  echo "[Worker] Loaded .env"
else
  echo "[Worker] ERROR: .env not found."
  exit 1
fi

mkdir -p "${ARTIFACTS_ROOT:-$SCRIPT_DIR/LLM_base/artifacts}"
mkdir -p "${LOG_DIR:-$SCRIPT_DIR/logs}/system"

export PYTHONPATH="$SCRIPT_DIR/ai_tool_web:${PYTHONPATH:-}"

echo "[Worker] Starting $COUNT worker(s)..."
cd "$SCRIPT_DIR/ai_tool_web"
exec "$CONDA_ENV/bin/python" -m worker.browser_worker --count "$COUNT"
