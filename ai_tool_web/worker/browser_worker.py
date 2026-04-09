"""
worker/browser_worker.py — Browser worker entry point.

One worker = one Chrome process = one session at a time.

Usage:
  python -m worker.browser_worker --id worker-1

Docker:
  CMD python -m worker.browser_worker --id ${WORKER_ID}
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Append LLM_base to sys.path (append, not insert, so our state/ package takes priority)
_LLM_BASE = Path(__file__).parent.parent.parent / "LLM_base"
if str(_LLM_BASE) not in sys.path:
    sys.path.append(str(_LLM_BASE))

from config import LOCK_TTL_S
from store import worker_registry
from store import job_queue
from store import session_store
from store.redis_client import get_async_redis, get_sync_redis
from worker import heartbeat, job_handler

import logging.handlers as _lh

_LOG_FORMAT = '{"time":"%(asctime)s","level":"%(levelname)s","worker":"%(name)s","msg":"%(message)s"}'

def _setup_logging() -> None:
    from config import LOG_DIR
    log_file = LOG_DIR / "system" / "worker.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        _lh.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ),
    ]
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=handlers)

_setup_logging()
_log = logging.getLogger(__name__)


async def main(worker_id: str) -> None:
    # Isolate browser session per worker — each worker gets its own Chrome context
    os.environ["AGENT_BROWSER_SESSION"] = worker_id

    async_r = get_async_redis()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        _log.error("OPENAI_API_KEY not set — worker will fail on LLM calls")

    started_at_iso = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()

    await worker_registry.register(async_r, worker_id, "idle", "", started_at=started_at_iso)
    hb_task = asyncio.create_task(heartbeat.run(worker_id, async_r))
    _log.info(f"[{worker_id}] Worker started, waiting for jobs")

    try:
        while True:
            session_id = await job_queue.pop_job(async_r, timeout=30)
            if session_id is None:
                continue

            # Atomic lock: only one worker gets the session
            locked = await async_r.set(
                f"lock:session:{session_id}",
                worker_id,
                nx=True,
                ex=LOCK_TTL_S,
            )
            if not locked:
                _log.warning(f"[{worker_id}] Session {session_id} already locked — skipping")
                continue

            _log.info(f"[{worker_id}] Picked up session {session_id}")
            await worker_registry.update(async_r, worker_id, status="busy", current_session=session_id)
            await session_store.update_async(async_r, session_id,
                                             status="assigned", assigned_worker=worker_id)

            sync_r = get_sync_redis()
            try:
                await asyncio.to_thread(
                    job_handler.run_job_sync,
                    session_id,
                    worker_id,
                    api_key,
                    sync_r,
                )
            except Exception as exc:
                _log.error(f"[{worker_id}] Unhandled error in session {session_id}: {exc}", exc_info=True)
            finally:
                sync_r.close()
                await async_r.delete(f"lock:session:{session_id}")
                await worker_registry.update(async_r, worker_id, status="idle", current_session="")
                _log.info(f"[{worker_id}] Released session {session_id}")

    except asyncio.CancelledError:
        _log.info(f"[{worker_id}] Worker shutting down")
    finally:
        hb_task.cancel()
        await async_r.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Browser worker")
    parser.add_argument("--id", default="worker-1", help="Unique worker ID")
    parser.add_argument("--count", type=int, default=1, help="Spawn N worker processes (manager mode)")
    args = parser.parse_args()

    if args.count > 1:
        import signal
        import subprocess

        _log.info(f"[manager] Spawning {args.count} workers...")
        procs: list[subprocess.Popen] = []
        for i in range(1, args.count + 1):
            p = subprocess.Popen(
                [sys.executable, "-m", "worker.browser_worker", "--id", f"worker-{i}"],
                env=os.environ.copy(),
            )
            procs.append(p)
            _log.info(f"[manager] Started worker-{i} (pid={p.pid})")

        def _shutdown(signum, frame):
            _log.info("[manager] Shutting down all workers...")
            for p in procs:
                p.terminate()
            for p in procs:
                p.wait()
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        # Monitor: restart any worker that crashes
        while True:
            import time
            time.sleep(5)
            for idx, p in enumerate(procs):
                if p.poll() is not None:
                    worker_id = f"worker-{idx + 1}"
                    _log.warning(f"[manager] {worker_id} exited (code={p.returncode}), restarting...")
                    new_p = subprocess.Popen(
                        [sys.executable, "-m", "worker.browser_worker", "--id", worker_id],
                        env=os.environ.copy(),
                    )
                    procs[idx] = new_p
                    _log.info(f"[manager] Restarted {worker_id} (pid={new_p.pid})")
    else:
        asyncio.run(main(args.id))
