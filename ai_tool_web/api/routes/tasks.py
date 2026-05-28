"""
api/routes/tasks.py — Task-centric endpoints (primary public API cho Sup Agent).

Mọi endpoint mặc định tác động đến iteration LATEST (theo iteration number)
của task. Sup Agent chỉ cần track task_id, không phải session_id.

Endpoints (Phương án B Dual API):
  POST   /v1/tasks/{task_id}/run         → tạo iteration mới (primary)
  GET    /v1/tasks/{task_id}/stream      → SSE stream iteration latest
  POST   /v1/tasks/{task_id}/resume      → answer cho ask_user của iter waiting
  POST   /v1/tasks/{task_id}/cancel      → cancel iter latest non-terminal
  GET    /v1/tasks/{task_id}/result      → result iter latest done
  GET    /v1/tasks/{task_id}/status      → combo status + iteration info

Session-level endpoints (vẫn giữ cho admin/debug + A/B testing):
  /v1/sessions/{session_id}/{stream|resume|cancel|result|...}

LEGACY:
  POST /v1/sessions  → backward compat, deprecated.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.sse_stream import sse_generator
from models import (
    CancelResponse,
    ResumeRequest,
    ResumeResponse,
    RunRequest,
    SessionCreatedResponse,
    SessionStatusResponse,
)
from api.routes.sessions import create_session
from api.routes.result import get_result as get_result_handler
from store import session_store
from store.redis_client import get_async_redis


router = APIRouter(tags=["tasks"])

_TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out"})
_NON_TERMINAL = frozenset({"queued", "assigned", "running", "waiting_for_user"})


async def _list_task_iterations(redis, task_id: str) -> list[dict]:
    """Lấy tất cả session thuộc task_id, sort theo iteration tăng dần."""
    all_sessions = await session_store.list_async(redis)
    matched = [s for s in all_sessions if s.get("task_id") == task_id]
    matched.sort(key=lambda s: int(s.get("iteration") or 0))
    return matched


async def _get_latest_iteration(
    redis,
    task_id: str,
    status_filter: Optional[frozenset] = None,
) -> Optional[dict]:
    """Trả về session dict của iteration latest (theo iteration number desc).

    status_filter: nếu set, chỉ lấy session có status trong filter.
    Returns None nếu không tìm thấy.
    """
    matched = await _list_task_iterations(redis, task_id)
    if status_filter:
        matched = [s for s in matched if s.get("status") in status_filter]
    if not matched:
        return None
    # Latest = iteration cao nhất (đã sort tăng dần ở trên, lấy cuối)
    return matched[-1]


def _no_iteration_404(task_id: str, reason: str = "") -> HTTPException:
    detail = f"Task '{task_id}' không có iteration nào"
    if reason:
        detail += f" ({reason})"
    return HTTPException(404, detail=detail)


class TaskRunRequest(BaseModel):
    """Body cho POST /v1/tasks/{task_id}/run.

    Giống RunRequest nhưng KHÔNG có field task_id (lấy từ URL path).

    extra="allow": capture field client gửi không khớp schema vào model_extra
    để debug Sup Agent typo field name. Forward sang RunRequest qua model_dump.
    """
    model_config = ConfigDict(extra="allow")

    # Mode (chọn 1 trong 3)
    scenario: str = "chang_login"
    scenario_yaml: Optional[str] = None
    query: Optional[str] = None
    query_site_hint: Optional[str] = None

    # Common
    name: Optional[str] = Field(default=None, max_length=120)
    goal: Optional[str] = None
    url: Optional[str] = None
    context: Optional[dict] = None
    max_steps: int = Field(default=20, ge=3, le=30)
    callback_url: Optional[str] = None
    callback_secret: Optional[str] = None
    ask_missing_inputs: Optional[bool] = None
    cancel_prev_iterations: bool = True


@router.post(
    "/v1/tasks/{task_id}/run",
    response_model=SessionCreatedResponse,
    status_code=201,
)
async def run_task(
    req: TaskRunRequest,
    task_id: str = Path(
        ...,
        min_length=1,
        max_length=128,
        description="ID logic của task (Sup Agent gen 1 lần cho 1 conversation).",
    ),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    """Tạo iteration mới trong task.

    Pipeline:
      1. Build RunRequest từ body + task_id từ URL
      2. Delegate sang create_session (logic chính nằm ở /v1/sessions handler)
      3. create_session sẽ:
         - INCR atomic counter → iteration number
         - Auto-cancel prev iterations cùng task_id (nếu cancel_prev=true)
         - Tạo session execution unit mới
         - Trả về SessionCreatedResponse với task_id, iteration, session_id

    Response giống POST /v1/sessions — Sup Agent dùng session_id từ response
    để attach SSE stream `/v1/sessions/{session_id}/stream`.
    """
    # Convert TaskRunRequest → RunRequest, inject task_id từ URL.
    # Pydantic v2 model_dump exclude None để tránh override default trong RunRequest.
    body = req.model_dump(exclude_none=True)
    body["task_id"] = task_id

    run_req = RunRequest(**body)
    return await create_session(run_req, x_user_id=x_user_id)


# ── Task-centric proxies ─────────────────────────────────────────────────────

class TaskStatusResponse(BaseModel):
    """Tổng hợp status của task — combo session info + iteration metadata."""
    task_id: str
    has_iterations: bool
    current_iteration: int = 0
    current_session_id: str = ""
    current_status: str = ""
    total_iterations: int = 0
    has_running: bool = False
    has_waiting_for_user: bool = False
    latest_done_iteration: int = 0       # iteration number của latest done (0 nếu chưa có)
    latest_done_session_id: str = ""


@router.get("/v1/tasks/{task_id}/status", response_model=TaskStatusResponse)
async def task_status(
    task_id: str = Path(..., min_length=1, max_length=128),
):
    """Status hiện tại của task — combo info giúp Sup Agent biết:
       - Iteration nào đang chạy (current_*)
       - Có iteration nào đã done không (latest_done_*)
       - Còn iteration nào blocking ask_user không
    """
    redis = get_async_redis()
    iterations = await _list_task_iterations(redis, task_id)
    if not iterations:
        return TaskStatusResponse(task_id=task_id, has_iterations=False)

    latest = iterations[-1]
    has_running = any(s.get("status") in _NON_TERMINAL for s in iterations)
    has_waiting = any(s.get("status") == "waiting_for_user" for s in iterations)

    done_iters = [s for s in iterations if s.get("status") == "done"]
    latest_done = max(done_iters, key=lambda s: int(s.get("iteration") or 0)) \
        if done_iters else None

    return TaskStatusResponse(
        task_id=task_id,
        has_iterations=True,
        current_iteration=int(latest.get("iteration") or 0),
        current_session_id=latest.get("session_id", ""),
        current_status=latest.get("status", ""),
        total_iterations=len(iterations),
        has_running=has_running,
        has_waiting_for_user=has_waiting,
        latest_done_iteration=int(latest_done.get("iteration") or 0) if latest_done else 0,
        latest_done_session_id=latest_done.get("session_id", "") if latest_done else "",
    )


@router.get("/v1/tasks/{task_id}/stream")
async def stream_task(
    task_id: str = Path(..., min_length=1, max_length=128),
    lastEventId: Optional[int] = Query(default=None),
):
    """SSE stream iteration latest của task.

    Iteration latest có thể đã done (replay events) hoặc đang chạy (live stream).
    Nếu user POST run mới (auto-cancel iter cũ) → stream hiện tại nhận event
    `cancelled` rồi close → Sup Agent mở lại /v1/tasks/{id}/stream cho iter mới.
    """
    redis = get_async_redis()
    latest = await _get_latest_iteration(redis, task_id)
    if not latest:
        raise _no_iteration_404(task_id, "chưa có session nào")

    session_id = latest["session_id"]
    last_event_id = lastEventId if lastEventId is not None else 0

    return StreamingResponse(
        sse_generator(redis, session_id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            # Expose session_id để Sup Agent biết iter nào đang stream
            "X-Session-Id": session_id,
            "X-Iteration": str(latest.get("iteration", "")),
        },
    )


@router.post("/v1/tasks/{task_id}/resume", response_model=ResumeResponse)
async def resume_task(
    req: ResumeRequest,
    task_id: str = Path(..., min_length=1, max_length=128),
):
    """Push answer vào iteration đang waiting_for_user.

    Pre-condition: phải có iteration `status=waiting_for_user`. Lỗi:
      - 404: không có iteration nào
      - 409 SESSION_NOT_WAITING: iteration latest không ở waiting state
      - 409 MULTIPLE_WAITING: >1 iteration cùng waiting (chỉ xảy ra với
                              cancel_prev_iterations=false) — dùng
                              /v1/sessions/{id}/resume để target cụ thể.
    """
    redis = get_async_redis()
    iterations = await _list_task_iterations(redis, task_id)
    if not iterations:
        raise _no_iteration_404(task_id)

    waiting = [s for s in iterations if s.get("status") == "waiting_for_user"]
    if not waiting:
        raise HTTPException(409, detail="SESSION_NOT_WAITING")
    if len(waiting) > 1:
        raise HTTPException(
            409,
            detail=(
                f"MULTIPLE_WAITING — task có {len(waiting)} iterations đang waiting. "
                f"Dùng /v1/sessions/{{session_id}}/resume để target cụ thể."
            ),
        )

    target = waiting[0]
    session_id = target["session_id"]
    msg = json.dumps({"type": "answer", "answer": req.answer}, ensure_ascii=False)
    await redis.rpush(f"resume:{session_id}", msg)

    return ResumeResponse(status="resumed", session_id=session_id)


@router.post("/v1/tasks/{task_id}/cancel", response_model=CancelResponse)
async def cancel_task(
    task_id: str = Path(..., min_length=1, max_length=128),
    all: bool = Query(
        default=False,
        description="True → cancel TẤT CẢ iterations non-terminal. False (default) → chỉ latest.",
    ),
):
    """Cancel iteration của task.

    Default: cancel iteration latest non-terminal.
    Với ?all=true: cancel toàn bộ iterations non-terminal (use case: user
    chạy parallel benchmark với cancel_prev_iterations=false, muốn kill hết).
    """
    redis = get_async_redis()
    iterations = await _list_task_iterations(redis, task_id)
    if not iterations:
        raise _no_iteration_404(task_id)

    non_terminal = [s for s in iterations if s.get("status") in _NON_TERMINAL]
    if not non_terminal:
        raise HTTPException(409, detail="SESSION_FINISHED — không có iteration nào đang chạy")

    targets = non_terminal if all else [non_terminal[-1]]

    total_steps_completed = 0
    last_session_id = ""
    for target in targets:
        sid = target["session_id"]
        await session_store.update_async(redis, sid, cancel_requested="1")
        if target.get("status") == "waiting_for_user":
            msg = json.dumps({"type": "cancel"}, ensure_ascii=False)
            await redis.rpush(f"resume:{sid}", msg)
        total_steps_completed += int(target.get("current_step", 0))
        last_session_id = sid

    return CancelResponse(
        status="cancelled",
        steps_completed=total_steps_completed,
    )


@router.get("/v1/tasks/{task_id}/result")
async def task_result(
    task_id: str = Path(..., min_length=1, max_length=128),
    strategy: str = Query(
        default="latest_done",
        description=(
            "Iteration nào để lấy result:\n"
            "- latest_done (default): iter cuối có status=done\n"
            "- latest: iter cuối bất kỳ (có thể chưa done)\n"
            "- latest_terminal: iter cuối có status terminal (done/failed/cancelled)"
        ),
    ),
    iteration: Optional[int] = Query(
        default=None,
        description="Iteration number cụ thể (override strategy)",
    ),
):
    """Result của iteration trong task.

    Trả về JSON content của result.json (giống GET /v1/sessions/{id}/result).
    404 nếu không có iteration phù hợp hoặc result chưa ready.
    """
    redis = get_async_redis()
    iterations = await _list_task_iterations(redis, task_id)
    if not iterations:
        raise _no_iteration_404(task_id)

    # Pick target iteration theo strategy hoặc iteration number explicit
    target = None
    if iteration is not None:
        target = next((s for s in iterations if int(s.get("iteration") or 0) == iteration), None)
        if not target:
            raise HTTPException(404, detail=f"Iteration {iteration} not found in task {task_id}")
    elif strategy == "latest_done":
        done = [s for s in iterations if s.get("status") == "done"]
        target = done[-1] if done else None
        if not target:
            raise HTTPException(404, detail="Không có iteration nào ở status=done")
    elif strategy == "latest_terminal":
        terminal = [s for s in iterations if s.get("status") in _TERMINAL]
        target = terminal[-1] if terminal else None
        if not target:
            raise HTTPException(404, detail="Không có iteration nào ở terminal state")
    elif strategy == "latest":
        target = iterations[-1]
    else:
        raise HTTPException(
            422,
            detail=f"Invalid strategy '{strategy}'. Use: latest_done | latest | latest_terminal",
        )

    # Delegate sang result handler (đọc result.json local file)
    return await get_result_handler(target["session_id"])
