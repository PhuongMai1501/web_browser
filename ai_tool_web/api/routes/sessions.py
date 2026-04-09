import json
import os
import uuid

from fastapi import APIRouter, HTTPException

from config import MAX_STEPS_CAP, MIN_STEPS
from models import RunRequest, SessionCreatedResponse, SessionStatusResponse
from store import job_queue, session_store
from store.redis_client import get_async_redis

router = APIRouter()


@router.post("/v1/sessions", response_model=SessionCreatedResponse, status_code=201)
async def create_session(req: RunRequest):
    redis = get_async_redis()

    if await job_queue.is_over_capacity(redis):
        raise HTTPException(503, detail="Queue is full. Try again later.")

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(500, detail="OPENAI_API_KEY not set")

    max_steps = max(MIN_STEPS, min(req.max_steps, MAX_STEPS_CAP))
    session_id = str(uuid.uuid4())

    scenario_config = {
        "scenario": req.scenario,
        "context": req.context,
        "goal": req.goal,
        "url": req.url,
        "max_steps": max_steps,
    }

    await session_store.create_async(
        redis,
        session_id=session_id,
        scenario=req.scenario,
        max_steps=max_steps,
        scenario_config=scenario_config,
    )
    q_pos = await job_queue.push_job(redis, session_id)

    return SessionCreatedResponse(
        session_id=session_id,
        status="queued",
        stream_url=f"/v1/sessions/{session_id}/stream",
        created_at="",   # filled by session_store; return minimal info
        queue_position=q_pos,
    )


@router.get("/v1/sessions/{session_id}", response_model=SessionStatusResponse)
async def get_session(session_id: str):
    redis = get_async_redis()
    sess = await session_store.get_async(redis, session_id)
    if not sess:
        raise HTTPException(404, detail="Session not found")

    return SessionStatusResponse(
        session_id=sess["session_id"],
        status=sess["status"],
        scenario=sess["scenario"],
        current_step=int(sess.get("current_step", 0)),
        max_steps=int(sess.get("max_steps", 0)),
        created_at=sess.get("created_at", ""),
        assigned_worker=sess.get("assigned_worker", ""),
        ask_deadline_at=sess.get("ask_deadline_at") or None,
        error_msg=sess.get("error_msg") or None,
        finished_at=sess.get("finished_at") or None,
    )
