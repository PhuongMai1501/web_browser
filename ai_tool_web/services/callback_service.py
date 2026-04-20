"""
services/callback_service.py — POST events về Supervisor Agent qua callback URL.

Luồng 2 (Callback Mode):
  Worker chạy → POST callback_url {type: "step", ...}
  Worker hỏi  → POST callback_url {type: "ask", ...}
  Worker xong  → POST callback_url {type: "done", ...}
  Worker lỗi   → POST callback_url {type: "failed", ...}

Không raise exception — lỗi callback được log, worker tiếp tục bình thường.
Non-blocking: step events gửi trong background thread, không chặn worker.
Terminal events (done/failed/cancelled) gửi blocking để đảm bảo delivery.
"""

import hashlib
import hmac
import json
import logging
import threading
import time

import requests

from config import CALLBACK_MAX_RETRIES, CALLBACK_TIMEOUT_S

_log = logging.getLogger(__name__)

_RETRY_DELAYS = [1, 3, 8]  # backoff giữa các lần retry
_TERMINAL_TYPES = frozenset({"done", "failed", "cancelled", "timed_out"})


class CallbackService:
    """
    POST event payloads về callback_url của Supervisor Agent.

    - step events: gửi non-blocking (background thread)
    - ask/done/failed: gửi blocking (đảm bảo Sup-Agent nhận)

    Usage:
        cb = CallbackService("http://sup/webhook", secret="hmac-key")
        cb.send(session_id, "step", {"step": 1, "action": "click", ...})
        cb.send(session_id, "ask",  {"step": 3, "message": "Cần OTP"})
        cb.send(session_id, "done", {"step": 8, "message": "OK", ...})
    """

    def __init__(self, callback_url: str, callback_secret: str = "") -> None:
        self._url = callback_url
        self._secret = callback_secret

    def send(
        self,
        session_id: str,
        event_type: str,
        payload: dict,
    ) -> bool:
        """
        POST event tới callback_url.

        - step: non-blocking (fire-and-forget trong background thread)
        - ask/done/failed/cancelled: blocking (retry + backoff)

        Returns True ngay lập tức cho non-blocking sends.
        """
        if event_type not in _TERMINAL_TYPES and event_type != "ask":
            # Non-blocking: fire-and-forget
            t = threading.Thread(
                target=self._send_with_retry,
                args=(session_id, event_type, payload),
                daemon=True,
            )
            t.start()
            return True

        # Blocking: đảm bảo delivery cho terminal + ask events
        return self._send_with_retry(session_id, event_type, payload)

    def _send_with_retry(
        self,
        session_id: str,
        event_type: str,
        payload: dict,
    ) -> bool:
        """
        POST event với retry + backoff.

        Body:
          {"session_id": "abc", "type": "step", "payload": {...}}

        Headers (nếu có secret):
          X-Callback-Signature: sha256=<hmac hex>

        Returns True nếu POST thành công (2xx), False nếu fail sau retries.
        """
        body = {
            "session_id": session_id,
            "type": event_type,
            "payload": payload,
        }
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self._secret:
            sig = hmac.new(
                self._secret.encode("utf-8"),
                body_bytes,
                hashlib.sha256,
            ).hexdigest()
            headers["X-Callback-Signature"] = f"sha256={sig}"

        max_attempts = min(CALLBACK_MAX_RETRIES, len(_RETRY_DELAYS)) + 1

        for attempt in range(max_attempts):
            try:
                resp = requests.post(
                    self._url,
                    data=body_bytes,
                    headers=headers,
                    timeout=CALLBACK_TIMEOUT_S,
                    proxies={"http": None, "https": None},
                )
                if resp.ok:
                    _log.debug(
                        "Callback OK [%s] session=%s type=%s",
                        resp.status_code, session_id, event_type,
                    )
                    return True

                _log.warning(
                    "Callback HTTP %s [attempt %d/%d] session=%s type=%s body=%s",
                    resp.status_code, attempt + 1, max_attempts,
                    session_id, event_type, resp.text[:200],
                )
            except requests.Timeout:
                _log.warning(
                    "Callback timeout (%ds) [attempt %d/%d] session=%s type=%s",
                    CALLBACK_TIMEOUT_S, attempt + 1, max_attempts,
                    session_id, event_type,
                )
            except Exception as e:
                _log.warning(
                    "Callback error [attempt %d/%d] session=%s type=%s: %s",
                    attempt + 1, max_attempts, session_id, event_type, e,
                )

            # Retry backoff
            if attempt < max_attempts - 1:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                time.sleep(delay)

        _log.error(
            "Callback FAILED after %d attempts — session=%s type=%s url=%s",
            max_attempts, session_id, event_type, self._url,
        )
        return False
