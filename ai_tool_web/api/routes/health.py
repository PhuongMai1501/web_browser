from fastapi import APIRouter
from redis.asyncio import Redis

from store import job_queue, worker_registry
from store.redis_client import get_async_redis

router = APIRouter()


@router.get("/v1/health")
async def health():
    redis: Redis = get_async_redis()
    workers = await worker_registry.get_all(redis)
    busy = sum(1 for w in workers if w.get("status") == "busy")
    q_len = await job_queue.queue_length(redis)
    return {
        "status": "ok",
        "workers_alive": len(workers),
        "workers_busy": busy,
        "queue_length": q_len,
    }
