"""
config.py — Centralized constants for ai_tool_web.

Phase 1a: constants only (no Redis).
Phase 1b: Redis connection will be added here.
"""

import os
from pathlib import Path

# ── Steps ─────────────────────────────────────────────────────────────────────
MAX_STEPS_CAP = 30
MIN_STEPS = 3

# ── Session timing ─────────────────────────────────────────────────────────────
SESSION_TTL_S = 600          # in-memory cleanup TTL (seconds after completion)
ASK_TIMEOUT_S = 300          # cancel if no /resume within 5 minutes
SESSION_HARD_CAP_S = 600     # hard kill session after 10 minutes

# ── Worker lock (Phase 1b) ─────────────────────────────────────────────────────
LOCK_TTL_S = 60
LOCK_RENEW_S = 15

# ── Redis (Phase 1b) ──────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── Artifacts ─────────────────────────────────────────────────────────────────
_DEFAULT_ARTIFACTS = str(Path(__file__).parent.parent / "LLM_base" / "artifacts")
ARTIFACTS_ROOT = Path(os.getenv("ARTIFACTS_ROOT", _DEFAULT_ARTIFACTS))

# ── Logging ───────────────────────────────────────────────────────────────────
_DEFAULT_LOG_DIR = str(Path(__file__).parent.parent / "logs")
LOG_DIR = Path(os.getenv("LOG_DIR", _DEFAULT_LOG_DIR))
