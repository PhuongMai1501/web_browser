import asyncio
import json
import logging
import os
import uuid

from fastapi import APIRouter, Header, HTTPException

from config import MAX_STEPS_CAP, MIN_STEPS
from models import RunRequest, SessionCreatedResponse, SessionStatusResponse
from services import scenario_service
from services.scenario_generator import generate_yaml
from services.scenario_service import ContextValidationError
from services.yaml_normalizer import normalize_yaml
from store import job_queue, session_store
from store.redis_client import get_async_redis

router = APIRouter()
_log = logging.getLogger(__name__)

# Session status nào không phải terminal — auto-cancel prev iterations chỉ
# tác động đến các session này.
_NON_TERMINAL = frozenset({"queued", "assigned", "running", "waiting_for_user"})

# Task counter key trong Redis — INCR atomic để gán iteration number.
_TASK_COUNTER_KEY = "task:{}:counter"

# TTL cho task counter — keep dài hơn SESSION_TTL_S để tránh reset iteration
# khi user comeback sau vài giờ. 24h là default reasonable.
_TASK_COUNTER_TTL_S = 24 * 3600


async def _cancel_prev_iterations(redis, task_id: str, new_session_id: str) -> int:
    """Cancel mọi session non-terminal cùng task_id (trừ session đang tạo).

    Pattern giống /v1/browser/kill-all nhưng scope theo task_id.
    Worker check `cancel_requested` ở mỗi step → graceful exit (5-10s).
    Session đang waiting_for_user → push cancel signal qua resume queue.

    Returns: số session đã cancel.
    """
    if not task_id:
        return 0

    cancelled = 0
    keys = await redis.keys("session:*")
    for key in keys:
        key_str = key.decode() if isinstance(key, bytes) else key
        # Skip nested keys (session:{id}:screenshots, :annotated)
        if key_str.count(":") > 1:
            continue
        sid = key_str.split(":", 1)[1]
        if sid == new_session_id:
            continue   # không tự cancel mình

        sess = await session_store.get_async(redis, sid)
        if not sess:
            continue
        if sess.get("task_id") != task_id:
            continue
        if sess.get("status") not in _NON_TERMINAL:
            continue

        # Mark cancel + giữ status để worker tự transition sang "cancelled"
        # (không force status ở đây để worker có cơ hội ghi result.json).
        await session_store.update_async(redis, sid, cancel_requested="1")

        # Unblock waiting_for_user nếu có
        if sess.get("status") == "waiting_for_user":
            msg = json.dumps({"type": "cancel"}, ensure_ascii=False)
            await redis.rpush(f"resume:{sid}", msg)

        cancelled += 1
        _log.info(
            "Auto-cancelled session %s (task=%s, iter=%s) for new iteration %s",
            sid, task_id, sess.get("iteration", "?"), new_session_id,
        )
    return cancelled


async def _next_iteration(redis, task_id: str) -> int:
    """INCR atomic counter cho task → trả iteration number cho session mới."""
    if not task_id:
        return 1
    key = _TASK_COUNTER_KEY.format(task_id)
    iteration = await redis.incr(key)
    await redis.expire(key, _TASK_COUNTER_TTL_S)
    return int(iteration)


def _looks_like_yaml(text: str) -> bool:
    """Heuristic: text trông như YAML scenario, không phải NL description.

    Nhận diện user paste YAML vào field query (UI mode nhầm) → server promote
    sang scenario_yaml để tránh gọi LLM gen YAML từ YAML.

    Pattern: bắt đầu với `id:` (key bắt buộc của scenario) + có ít nhất 1
    key đặc trưng (steps:/mode:/start_url:/inputs:).
    """
    if not text:
        return False
    head = text.lstrip()[:200]
    if not head.lower().startswith("id:"):
        return False
    markers = ("\nsteps:", "\nmode:", "\nstart_url:", "\ninputs:")
    return any(m in text for m in markers)


_SECRET_CTX_PATTERN = __import__("re").compile(
    r"(password|pwd|secret|token|api[_-]?key)", __import__("re").I
)


def _mask_context_secrets(context: dict | None) -> dict:
    """Mask secret-shaped keys trong context để log không leak credentials."""
    if not context:
        return {}
    masked: dict = {}
    for k, v in context.items():
        if _SECRET_CTX_PATTERN.search(k):
            masked[k] = "***MASKED***" if v not in (None, "") else v
        else:
            masked[k] = v
    return masked


def _build_request_flow(req, user_id, source, model, tokens_in, tokens_out) -> dict:
    """Build dict audit trail của 1 request từ Sup Agent.

    Worker sẽ ghi thành request_flow.json + upload MinIO. User check sau khi
    session done để biết Sup Agent đã gửi gì + API quyết định gì.
    """
    from datetime import datetime, timezone
    return {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": f"POST /v1/tasks/{req.task_id}/run" if req.task_id else "POST /v1/sessions",
        "user_id": (user_id or "").strip(),
        "task_id": req.task_id or "",
        "raw_body": {
            "scenario": req.scenario,
            "query": req.query,
            "query_site_hint": req.query_site_hint,
            "scenario_yaml_provided": bool(req.scenario_yaml),
            "scenario_yaml_len": len(req.scenario_yaml or ""),
            "context": _mask_context_secrets(req.context),
            "goal": req.goal,
            "url": req.url,
            "max_steps": req.max_steps,
            "name": req.name,
            "callback_url": req.callback_url,
            "ask_missing_inputs": req.ask_missing_inputs,
            "cancel_prev_iterations": req.cancel_prev_iterations,
        },
        "decision": {
            "source": source,  # scenario_yaml_provided | yaml_promoted_from_query | llm_gen | scenario_id_lookup
            "llm_model": model or None,
            "llm_tokens_in": tokens_in or None,
            "llm_tokens_out": tokens_out or None,
        },
        "final_scenario_yaml": req.scenario_yaml or None,
    }


def _convert_missing_required_to_ask(spec, context):
    """Đổi inputs[].source=context required nhưng thiếu trong request context
    sang source=ask_user + prepend ask_user steps để worker hỏi user runtime
    qua SSE thay vì reject 422.

    Returns (new_spec, converted_names). Nếu không có gì đổi → trả về spec gốc.

    Lưu ý: bỏ qua input đã có trong steps là `action: ask_user` (LLM YAML đôi
    khi đã include sẵn) để không double-ask cùng 1 field.
    """
    ctx = context or {}
    if not spec.inputs:
        return spec, []

    # Tập field đã có ask_user step → không cần prepend lại
    existing_ask_fields = {
        s.field for s in (spec.steps or []) if s.action == "ask_user" and s.field
    }

    new_inputs = []
    converted: list[str] = []
    converted_meta: list[tuple[str, str]] = []  # (name, prompt) để build ask_user steps
    for inp in spec.inputs:
        is_missing = (
            inp.source == "context"
            and inp.required
            and (inp.name not in ctx or ctx[inp.name] in (None, ""))
        )
        if is_missing:
            new_inputs.append(inp.model_copy(update={"source": "ask_user"}))
            converted.append(inp.name)
            if inp.name not in existing_ask_fields:
                prompt = inp.description or f"Vui lòng nhập {inp.name}"
                converted_meta.append((inp.name, prompt))
        else:
            new_inputs.append(inp)
    if not converted:
        return spec, []

    # Build ask_user steps + prepend vào spec.steps. Dùng FlowStep từ module
    # scenarios để Pydantic validate kèm các field default.
    from scenarios.flow_models import FlowStep

    ask_steps = [
        FlowStep(action="ask_user", field=name, prompt=prompt)
        for name, prompt in converted_meta
    ]
    new_steps = ask_steps + list(spec.steps or [])

    return spec.model_copy(update={"inputs": new_inputs, "steps": new_steps}), converted


@router.post("/v1/sessions", response_model=SessionCreatedResponse, status_code=201)
async def create_session(
    req: RunRequest,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    """Tạo session iteration.

    [Legacy endpoint — backward compat]
    Sup Agent mới nên dùng `POST /v1/tasks/{task_id}/run` (task-centric).
    Endpoint này vẫn hoạt động đầy đủ — task_id lấy từ body (auto-gen UUID
    nếu rỗng). Sẽ deprecate sau khi WebUI migrate xong.
    """
    redis = get_async_redis()

    if await job_queue.is_over_capacity(redis):
        raise HTTPException(503, detail="Queue is full. Try again later.")

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(500, detail="OPENAI_API_KEY not set")

    # Cả scenario_yaml + query cùng có: ưu tiên scenario_yaml, bỏ qua query.
    # Pattern Sup Agent: cache YAML rồi resend kèm query gốc để audit/log,
    # không tốn LLM call lại. Query khi đó chỉ là metadata.
    if req.scenario_yaml and req.query:
        _log.info(
            "[task=%s] Both scenario_yaml + query set → use scenario_yaml, "
            "ignore query (kept as audit metadata: %r)",
            req.task_id or "(auto)", req.query[:100],
        )

    # Track xem YAML đến từ đâu để response trả về đúng metadata cho Sup Agent.
    # request_source enum:
    #   "scenario_yaml_provided"     — Sup Agent paste YAML inline (cache)
    #   "yaml_promoted_from_query"   — user paste YAML vào field query nhầm
    #   "llm_gen"                    — LLM gen YAML từ NL query
    #   "scenario_id_lookup"         — dùng scenario_id, lookup DB (không YAML)
    generated_from_query = False
    request_source: str = "scenario_id_lookup"
    if req.scenario_yaml:
        request_source = "scenario_yaml_provided"
    gen_model_used: str = ""
    gen_tokens_in: int = 0
    gen_tokens_out: int = 0

    # Mode `query` — LLM gen YAML rồi rơi xuống flow scenario_yaml chung.
    # Sup Agent gửi NL description, API gen YAML, validate, chạy.
    # CHỈ gen khi scenario_yaml CHƯA có — nếu Sup Agent gửi sẵn YAML (cache),
    # skip LLM call để tiết kiệm cost/latency.
    if req.query and not req.scenario_yaml:
        q = req.query.strip()
        if not q:
            raise HTTPException(422, detail="'query' không được rỗng.")

        # Heuristic: user paste YAML vào field query (chọn nhầm mode UI).
        # Tránh gọi LLM gen lại YAML từ YAML — promote sang scenario_yaml.
        if _looks_like_yaml(q):
            _log.info(
                "[task=%s] query field looks like YAML (starts with id:/mode:/steps:) "
                "→ promote sang scenario_yaml, skip LLM gen",
                req.task_id or "(auto)",
            )
            req.scenario_yaml = q
            req.query = None
            request_source = "yaml_promoted_from_query"
        else:
            # generate_yaml gọi OpenAI sync (~3-5s) → off-thread để không block event loop.
            gen = await asyncio.to_thread(
                generate_yaml,
                description=q,
                site_hint=req.query_site_hint,
            )
            gen_model_used = gen.model
            gen_tokens_in = gen.tokens_in
            gen_tokens_out = gen.tokens_out
            if not gen.ok:
                raise HTTPException(
                    502,
                    detail=f"LLM generate fail: {gen.error}",
                )
            # Inject YAML đã gen vào scenario_yaml flow phía dưới — KHÔNG lưu DB,
            # chỉ tồn tại trong scenario_config của session này.
            req.scenario_yaml = gen.yaml
            generated_from_query = True
            request_source = "llm_gen"

    # Hai mode (sau khi query đã chuyển thành scenario_yaml):
    #  - YAML inline: req.scenario_yaml set → parse ad-hoc spec, không cần DB lookup
    #  - DB lookup (default): scenario_service.get_async(req.scenario)
    if req.scenario_yaml:
        # Auto-gen scenario id để tracking (không lưu DB).
        # Prefix `_q_` cho YAML sinh từ query, `_custom_` cho YAML user paste —
        # phân biệt trong log + admin monitor.
        id_prefix = "_q_" if generated_from_query else "_custom_"
        custom_id = f"{id_prefix}{uuid.uuid4().hex[:8]}"
        result = normalize_yaml(req.scenario_yaml, force_id=custom_id)
        if not result.parse_ok:
            error_msgs = [f"{e.field}: {e.message}" for e in result.errors]
            # Log warning nếu YAML hỏng mà backend kèm query — debug sau dễ
            # trace query gốc dẫn đến YAML cache bị corrupt.
            if req.query and not generated_from_query:
                _log.warning(
                    "[task=%s] YAML invalid + có query=%r → fail-fast 422 "
                    "(không fallback LLM gen). Errors: %s",
                    req.task_id or "(auto)", req.query[:100],
                    "; ".join(error_msgs),
                )
            raise HTTPException(
                422,
                detail=(
                    f"YAML {'gen từ query' if generated_from_query else 'inline'} "
                    f"parse fail: {'; '.join(error_msgs)}"
                ),
            )
        if not result.validation_ok or result.spec is None:
            error_msgs = [f"{e.field}: {e.message}" for e in result.errors]
            if req.query and not generated_from_query:
                _log.warning(
                    "[task=%s] YAML invalid (validation) + có query=%r → fail-fast 422. "
                    "Errors: %s",
                    req.task_id or "(auto)", req.query[:100],
                    "; ".join(error_msgs),
                )
            raise HTTPException(
                422,
                detail=(
                    f"YAML {'gen từ query' if generated_from_query else 'inline'} "
                    f"validation fail: {'; '.join(error_msgs)}"
                ),
            )
        spec = result.spec
        # Override req.scenario để log nhất quán (kể cả user truyền tên khác)
        req.scenario = spec.id
    else:
        # Load + validate spec tại thời điểm enqueue; embed snapshot để job
        # không bị ảnh hưởng nếu admin sửa spec giữa chừng.
        spec = await scenario_service.get_async(redis, req.scenario)
        if spec is None:
            raise HTTPException(404, detail=f"Scenario '{req.scenario}' không tồn tại")
        if not spec.enabled:
            raise HTTPException(409, detail=f"Scenario '{req.scenario}' đang bị disabled")

    # Auto-inject inputs[].default vào context cho field required nhưng caller
    # KHÔNG truyền. Dùng cho YAML scenario có credentials hardcoded (test mode
    # Sup Agent paste YAML, không gửi context riêng).
    # Security: secret field type=secret KHÔNG nên có default (yaml_normalizer
    # _check_security_secrets sẽ raise validation error nếu paste vào YAML).
    if spec.inputs:
        ctx_with_defaults = dict(req.context or {})
        for inp in spec.inputs:
            if inp.source != "context":
                continue
            if inp.name not in ctx_with_defaults or ctx_with_defaults[inp.name] in (None, ""):
                if inp.default is not None:
                    ctx_with_defaults[inp.name] = inp.default
        req.context = ctx_with_defaults

    # Resolve ask_missing_inputs: explicit user choice > auto-enable cho query mode.
    # Mode `query`: LLM gen YAML thường declare credentials inputs với source=context;
    # user gõ NL không khai báo trước → fallback ask_user runtime để worker hỏi
    # qua SSE thay vì reject 422.
    if req.ask_missing_inputs is not None:
        auto_ask = bool(req.ask_missing_inputs)
    else:
        auto_ask = generated_from_query

    if auto_ask:
        spec, converted = _convert_missing_required_to_ask(spec, req.context)
        if converted:
            _log.info(
                "[%s] Auto-converted missing required inputs %s → ask_user "
                "(generated_from_query=%s)",
                req.scenario, converted, generated_from_query,
            )

    try:
        scenario_service.validate_context(spec, req.context)
    except ContextValidationError as e:
        raise HTTPException(422, detail=str(e))

    max_steps = max(MIN_STEPS, min(req.max_steps, MAX_STEPS_CAP))
    session_id = str(uuid.uuid4())

    scenario_config = {
        "scenario": req.scenario,
        "context": req.context,
        "goal": req.goal,
        "url": req.url,
        "max_steps": max_steps,
        "spec_snapshot": spec.model_dump(mode="json"),
        # Audit trail: full flow từ Sup Agent gửi → API decision → final YAML.
        # Worker sẽ ghi thành request_flow.json + upload MinIO cùng result.json.
        # Sup Agent / Hiệp dùng để debug sau khi session done.
        "request_flow": _build_request_flow(req, x_user_id, request_source,
                                            gen_model_used, gen_tokens_in,
                                            gen_tokens_out),
    }

    # Callback mode: lưu callback config vào scenario_config để worker đọc
    mode = "sse"
    if req.callback_url:
        scenario_config["callback_url"] = req.callback_url
        if req.callback_secret:
            scenario_config["callback_secret"] = req.callback_secret
        mode = "callback"

    # Auto-fill name nếu user không truyền — fallback "<scenario>" để UI luôn có label.
    session_name = (req.name or "").strip()[:120] if req.name else ""

    # Task tracking — Sup Agent gửi task_id để group iterations. Nếu rỗng,
    # API auto-gen UUID (đảm bảo mọi session đều có task_id, đơn giản hoá
    # query phía admin). Limit 128 char (đã validate ở Pydantic Field).
    task_id = (req.task_id or "").strip() or f"t-{uuid.uuid4().hex[:12]}"

    # INCR atomic counter để gán iteration number. Race safe khi 2 POST
    # cùng task_id đến đồng thời — mỗi POST nhận unique iteration.
    iteration = await _next_iteration(redis, task_id)

    # Auto-cancel iterations cũ non-terminal nếu user opt-in (default true).
    # Pattern: user sửa YAML → tạo session mới → iteration cũ tự bị kill.
    # cancel_prev_iterations=false dùng cho A/B benchmark (chạy song song).
    cancelled_prev_count = 0
    if req.cancel_prev_iterations:
        cancelled_prev_count = await _cancel_prev_iterations(
            redis, task_id, new_session_id=session_id,
        )

    await session_store.create_async(
        redis,
        session_id=session_id,
        scenario=req.scenario,
        max_steps=max_steps,
        scenario_config=scenario_config,
        name=session_name,
        user_id=(x_user_id or "").strip(),
        task_id=task_id,
        iteration=iteration,
    )
    q_pos = await job_queue.push_job(redis, session_id)

    # Trả YAML cho mọi mode có scenario_yaml (paste / promote / gen) — Sup
    # Agent cần xác nhận YAML THỰC TẾ đang chạy (debug + log).
    # Mode scenario_id_lookup: không có YAML inline → None.
    response_yaml = req.scenario_yaml if req.scenario_yaml else None

    return SessionCreatedResponse(
        session_id=session_id,
        status="queued",
        stream_url=f"/v1/sessions/{session_id}/stream",
        mode=mode,
        created_at="",   # filled by session_store; return minimal info
        queue_position=q_pos,
        scenario_yaml=response_yaml,
        scenario_id=req.scenario if response_yaml else None,
        generated_from_query=generated_from_query,
        model_used=gen_model_used or None,
        tokens_in=gen_tokens_in or None,
        tokens_out=gen_tokens_out or None,
        task_id=task_id,
        iteration=iteration,
        cancelled_prev_count=cancelled_prev_count,
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
        name=sess.get("name") or None,
        current_step=int(sess.get("current_step", 0)),
        max_steps=int(sess.get("max_steps", 0)),
        created_at=sess.get("created_at", ""),
        assigned_worker=sess.get("assigned_worker", ""),
        ask_deadline_at=sess.get("ask_deadline_at") or None,
        error_msg=sess.get("error_msg") or None,
        finished_at=sess.get("finished_at") or None,
        task_id=sess.get("task_id") or None,
        iteration=int(sess["iteration"]) if sess.get("iteration") else None,
    )
