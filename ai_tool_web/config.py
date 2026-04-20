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

# ── LLM ──────────────────────────────────────────────────────────────────────
LLM_MODEL         = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_S     = int(os.getenv("LLM_TIMEOUT", "60"))
LLM_MAX_RETRIES   = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RETRY_DELAYS  = [1, 3, 8]

# ── Browser ──────────────────────────────────────────────────────────────────
BROWSER_TIMEOUT_S = int(os.getenv("BROWSER_TIMEOUT", "30"))

# ── Upload / CDN ───────────────────────────────────────────────────────────────
UPLOAD_URL     = os.getenv("UPLOAD_URL", "")           # http://upload.dsc.net
PUBLIC_CDN_URL = os.getenv("PUBLIC_CDN_URL", "")       # https://cdn.fstats.ai
UPLOAD_BUCKET  = os.getenv("UPLOAD_BUCKET", "changchatbot")
UPLOAD_KEY     = os.getenv("UPLOAD_KEY", "")
UPLOAD_SECRET  = os.getenv("UPLOAD_SECRET", "")
UPLOAD_ENABLED = bool(UPLOAD_URL and UPLOAD_KEY and UPLOAD_SECRET)
UPLOAD_TIMEOUT_S   = int(os.getenv("UPLOAD_TIMEOUT", "15"))
UPLOAD_MAX_RETRIES = int(os.getenv("UPLOAD_MAX_RETRIES", "3"))

# ── System Log Upload ────────────────────────────────────────────────────────
LOG_UPLOAD_INTERVAL_S = int(os.getenv("LOG_UPLOAD_INTERVAL_SEC", "30"))
LOG_MAX_BUFFER_SIZE   = int(os.getenv("LOG_MAX_BUFFER_SIZE", "100"))

# ── Screenshot ───────────────────────────────────────────────────────────────
MAX_SCREENSHOTS_RETAIN = int(os.getenv("MAX_SCREENSHOTS_RETAIN", "10"))

# ── Callback Mode ────────────────────────────────────────────────────────────
CALLBACK_TIMEOUT_S    = int(os.getenv("CALLBACK_TIMEOUT", "10"))
CALLBACK_MAX_RETRIES  = int(os.getenv("CALLBACK_MAX_RETRIES", "3"))
