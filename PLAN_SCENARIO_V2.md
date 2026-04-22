# Plan Scenario v2 — Hợp nhất `scenari_ref.md` & `SCENARIOS.md`

> So sánh 2 tài liệu và đề xuất 1 plan triển khai thực tế.
> Mục tiêu cuối: PM/QA tự định nghĩa flow login / form / search **không cần code Python**.

---

## 1. TL;DR

| Khía cạnh | Đã có (v1) | Đề xuất (v2) | Khoảng cách |
|-----------|-------------|--------------|-------------|
| Spec storage | Redis + YAML seed + admin API | Giữ nguyên | ✅ 0 |
| Scenario definition | `goal: str` + hooks | `steps: [FlowStep]` khai báo từng bước | 🔴 Lớn |
| Runner | 1 path: hook + LLM autonomous | 3 mode: `flow` / `agent` / `hybrid` | 🟠 Vừa |
| Input model | `{"required":["email"]}` | `InputField(type, required, secret, source)` | 🟡 Nhỏ |
| Element targeting | LLM nhìn snapshot | `text_any` / `label_any` / `css` | 🔴 Lớn (cần action engine) |
| Success detection | Hook Python (DOM check) | `success.any_of` khai báo | 🟡 Nhỏ |
| Hook system | Pre/post/final | **Giữ nguyên**, vai trò giảm | ✅ 0 |

**Kết luận:** v1 đã lo xong *tầng hạ tầng* (storage/API/hooks). v2 cần xây *tầng ngôn ngữ khai báo* (flow spec + action engine + targeting) — đây là phần tốn công chính.

---

## 2. So sánh 2 tài liệu

### `SCENARIOS.md` — trạng thái hiện tại (v1, đã ship)

Mô tả hệ thống đã refactor từ hardcode sang:
- `ScenarioSpec` khai báo trong Redis, seed từ YAML.
- Python hook (pre_check / post_step / final_capture) cho logic đặc thù.
- Admin REST API + auth token.
- Generic runner dùng hook + `run_agent_autonomous` (LLM tự quyết định).

→ **Đã giải quyết**: "thêm scenario mới không cần sửa 3 chỗ code".
→ **Chưa giải quyết**: flow nghiệp vụ (login → fill email → fill pwd → OTP → success) vẫn cần LLM đoán, hoặc hook Python cho case phức tạp. PM/QA vẫn bị giới hạn.

### `scenari_ref.md` — định hướng v2

Tách 3 tầng rõ ràng:
1. **Flow spec** (JSON/YAML) — ngôn ngữ khai báo cho product.
2. **Flow runner** — engine chạy tuần tự action chuẩn.
3. **Hook** — chỉ dùng cho anti-bot / iframe / edge cases.

3 mode vận hành:
- `flow` — chạy step-by-step cứng.
- `agent` — LLM tự quyết (v1 hiện tại).
- `hybrid` — flow trước, agent fallback.

→ Giải quyết cốt lõi: PM tự viết flow phổ biến mà không cần LLM đoán hay dev viết Python.

### Điểm trùng khớp (đã thống nhất)

- Spec lưu Redis + seed YAML ✅
- Admin CRUD API ✅
- Hook system cho case đặc biệt ✅
- Snapshot spec tại enqueue ✅

### Điểm bổ sung (chưa có trong v1)

| scenari_ref.md yêu cầu | v1 hiện tại | Cần làm |
|-----------------------|--------------|---------|
| `mode: flow \| agent \| hybrid` | Không có field, ngầm là "agent" | Thêm `mode` vào `ScenarioSpec` |
| `inputs: [InputField]` với type/secret/source | `context_schema: {"required":[...]}` | Upgrade schema |
| `steps: [FlowStep]` action-based | Không có | Tạo `FlowStep`, `TargetSpec`, `ACTION_REGISTRY` |
| `success: SuccessRule` declarative | Hook Python | Thêm `SuccessRule`, checker |
| Target matching `text_any`/`label_any` | LLM đọc snapshot | Viết matcher trên snapshot (reuse agent-browser) |
| `flow_runner.py` | Chỉ có generic_runner (agent mode) | Tạo mới + route theo `mode` |
| Action engine (goto/fill/click/wait_for/ask_user/if_visible) | Không có | Tạo `actions/` package |

---

## 3. Gap phân tích chi tiết

### 3.1. Spec model

**Hiện tại (spec.py):**
```python
class ScenarioSpec(BaseModel):
    id: str
    goal: str = ""
    context_schema: dict = {}     # chỉ {"required":[...]}
    hooks: ScenarioHooks = ...
```

**Cần thêm:**
```python
class ScenarioSpec(BaseModel):
    # ... như cũ ...
    mode: Literal["flow", "agent", "hybrid"] = "agent"   # default giữ hành vi cũ

    inputs: list[InputField] = []    # thay dần context_schema
    steps: list[FlowStep] = []       # bắt buộc khi mode=flow
    success: SuccessRule | None = None
    failure: FailureRule | None = None

class InputField(BaseModel):
    name: str
    type: Literal["string", "secret", "number", "bool"] = "string"
    required: bool = False
    source: Literal["context", "ask_user"] = "context"
    default: Any | None = None

class TargetSpec(BaseModel):
    text_any: list[str] | None = None
    label_any: list[str] | None = None
    placeholder_any: list[str] | None = None
    role: str | None = None          # button / textbox / link
    css: str | None = None           # escape hatch cho dev
    nth: int = 0                     # nếu nhiều kết quả

class FlowStep(BaseModel):
    action: str                      # "goto" | "fill" | "click" | ...
    target: TargetSpec | None = None
    value_from: str | None = None    # tên field trong inputs
    value: str | None = None         # literal
    field: str | None = None         # dùng với ask_user
    prompt: str | None = None
    url: str | None = None
    timeout_ms: int | None = None
    # cho if_visible:
    then: list["FlowStep"] = []
    else_: list["FlowStep"] = Field(default=[], alias="else")

class SuccessRule(BaseModel):
    any_of: list["Condition"] = []
    all_of: list["Condition"] = []

class Condition(BaseModel):
    url_contains: str | None = None
    text_any: list[str] | None = None
    element_visible: TargetSpec | None = None
```

**Back-compat:** `context_schema` giữ lại deprecated, validator fallback sang nếu `inputs` rỗng. Spec `mode=agent` vẫn chạy hooks + LLM như v1 — không ảnh hưởng `chang_login`.

### 3.2. Action engine

`ACTION_REGISTRY` mới, tách riêng khỏi HOOK_REGISTRY:

```python
# LLM_base/scenarios/action_registry.py
ACTION_REGISTRY: dict[str, Callable] = {}

def action(name: str):
    def deco(fn): ACTION_REGISTRY[name] = fn; return fn
    return deco

# LLM_base/scenarios/actions/goto.py
@action("goto")
def run_goto(rt: FlowRuntime, step: FlowStep) -> ActionResult: ...

# Tương tự: wait_for, fill, click, ask_user, if_visible, extract, assert_
```

Mỗi action nhận `FlowRuntime` (browser, context, step_num, emit helpers) và `FlowStep`, trả `ActionResult(record, ask_user_field, terminate, error)`.

### 3.3. Target matching

Đây là **phần khó nhất**. Hiện `browser_adapter.py` chỉ trả accessibility snapshot (ref=e1, e2...) và chỉ validate khi có ref cụ thể. Chưa có hàm "tìm element theo text/label".

**Đề xuất:** viết `snapshot_query.py` thuần Python parse snapshot text rồi khớp:
- `text_any` → grep text nodes trong snapshot.
- `label_any` → tìm label tag liền kề input.
- `placeholder_any` → parse metadata `placeholder="..."`.
- `role` → filter theo role prefix trong snapshot ("button", "textbox"...).
- `nth` → pick index trong list match.

Trả về ref hoặc raise `TargetNotFound`. Action `fill`/`click` gọi qua adapter với ref tìm được.

Nếu matcher fail với `text_any`/`label_any` → fallback gợi ý dùng `css` hoặc báo lỗi flow để admin fix.

### 3.4. Runner routing

```python
# LLM_base/scenarios/generic_runner.py
def run_scenario(spec, ...):
    if spec.mode == "flow":
        yield from run_flow(spec, ...)       # NEW
    elif spec.mode == "hybrid":
        yield from run_hybrid(spec, ...)     # NEW (Sprint 2)
    else:
        yield from run_agent(spec, ...)      # current path
```

`run_agent` = đúng code generic_runner hiện tại, chỉ rename.

---

## 4. Plan đề xuất (hợp nhất)

### Nguyên tắc

- **Không phá v1.** `mode="agent"` (default nếu spec cũ không set) chạy y hệt hôm nay. `chang_login` + `custom` builtin không cần đổi.
- **Ship from-end incrementally.** Mỗi sprint chạy E2E được 1 scenario mới, không ship half-baked.
- **Dev viết action 1 lần — product dùng muôn đời.** Ưu tiên 5 action phủ 80% use case (goto, wait_for, fill, click, ask_user) trước khi làm `if_visible` / `extract`.
- **Skip UI builder cho đến khi flow runner stable.** YAML/JSON editor qua admin API là đủ cho Sprint 1–2; UI là Sprint 3+.

### Sprint 1 — Flow MVP (1–1.5 tuần)

**Mục tiêu:** chạy được scenario login đơn giản (không OTP) khai báo bằng YAML, 0 hook, 0 LLM.

| # | Task | File |
|---|------|------|
| 1.1 | Thêm `mode`, `inputs`, `steps`, `success` vào `ScenarioSpec`; tạo `InputField`, `TargetSpec`, `FlowStep`, `SuccessRule` | [LLM_base/scenarios/spec.py](LLM_base/scenarios/spec.py), thêm `flow_models.py` |
| 1.2 | Action registry + 5 action: `goto`, `wait_for`, `fill`, `click`, `ask_user` | `LLM_base/scenarios/action_registry.py`, `LLM_base/scenarios/actions/*.py` |
| 1.3 | `snapshot_query.py` — matcher `text_any`, `label_any`, `placeholder_any`, `role` | `LLM_base/scenarios/snapshot_query.py` |
| 1.4 | `flow_runner.py` — chạy steps, emit StepRecord, xử lý ask_user pause/resume, check success | `LLM_base/scenarios/flow_runner.py` |
| 1.5 | Route theo `spec.mode` trong `generic_runner.py` | [LLM_base/scenarios/generic_runner.py](LLM_base/scenarios/generic_runner.py) |
| 1.6 | Validator chặt: hook names + action names + input references + target không rỗng | [ai_tool_web/services/scenario_service.py](ai_tool_web/services/scenario_service.py) tách thêm `scenario_validator.py` |
| 1.7 | Builtin YAML: `login_basic.yaml` (flow) — test bằng trang login public đơn giản | `LLM_base/scenarios/builtin/login_basic.yaml` |
| 1.8 | Unit test: FlowStep parse, target match trên snapshot mẫu, runner happy path + ask_user flow | `ai_tool_web/tests/test_flow_runner.py` |

**Done =** `curl POST /v1/sessions {"scenario":"login_basic","context":{...}}` chạy end-to-end login thật.

### Sprint 2 — Flow thực tế + ổn định (1 tuần)

**Mục tiêu:** hỗ trợ OTP, conditional, retry/timeout.

- `if_visible` action (then/else branch).
- `assert` + `extract` action.
- Timeout per-step + retry 1–2 lần cho `wait_for` / `click`.
- `success.any_of` / `all_of` + `failure.any_of` → trả `failed` event với code/message cụ thể.
- Screenshot theo step (đã có sẵn, chỉ wire vào flow runner).
- `login_with_otp.yaml` builtin — flow mẫu match ví dụ trong `scenari_ref.md`.
- Port thử `chang_login` từ hooks sang flow spec → đánh giá có nên deprecate hook version không (khả năng: vẫn giữ vì Azure Authenticator step phức tạp, dùng `mode=hybrid`).

### Sprint 3 — Hybrid mode + admin UX (1 tuần)

- `run_hybrid`: chạy flow tới 1 checkpoint (`until: {url_contains: ...}`), sau đó chuyển agent autonomous cho đến `done`.
- `POST /v1/scenarios/{id}/dry-run` với context giả → return step-by-step gì sẽ xảy ra (không thực sự chạy browser).
- Preview: `GET /v1/scenarios/{id}/explain` render flow dạng text ("1. Mở X, 2. Điền Y…") cho admin review.
- Audit log admin: ghi ai đổi spec lúc nào (Redis hash `scenario_audit:<id>`).
- Version history (giữ 5 bản cuối).

### Sprint 4 — UI builder + AI-authoring (2 tuần, optional)

- Builder web component (4 panel: Basic / Inputs / Steps / Success) — frontend task riêng.
- LLM draft: "mô tả bằng tiếng Việt" → spec draft → user sửa → save. Gate sau nút confirm, không auto-apply.
- Chỉ làm khi sprint 1–3 ổn định và có demand thật.

---

## 5. Câu hỏi cần chốt trước Sprint 1

Xin user quyết để tránh rework:

1. **Default `mode` khi spec cũ không khai báo?**
   - Option A (an toàn): `agent` — hành vi y hệt hôm nay.
   - Option B: `flow` — buộc admin điền `steps` khi tạo mới.
   - *Đề xuất: A* (back-compat).

2. **`chang_login` giữ hooks hay port sang flow?**
   - Option A: giữ `mode=agent` + hooks như hiện tại (Sprint 2 port thử song song, không ép).
   - Option B: port ngay Sprint 1 làm case validation.
   - *Đề xuất: A* — Azure Authenticator có step "chờ push notification" không hợp với flow cứng, dễ fail E2E test mà không có ROI.

3. **Matcher engine dùng gì?**
   - Option A: parse accessibility snapshot thuần Python (nhẹ, đủ cho text/label).
   - Option B: kêu `agent-browser` thêm CLI command `query --text "..."` (tốt hơn nhưng đổi binary Rust).
   - Option C: chạy JS eval qua `page_contains_any` pattern hiện có + extend.
   - *Đề xuất: A* cho Sprint 1, pilot C cho case khó.

4. **Action name convention?**
   - `snake_case` (`wait_for`, `if_visible`, `ask_user`) — nhất quán Python.
   - `camelCase` (`waitFor`, `ifVisible`, `askUser`) — giống web tooling (Playwright).
   - *Đề xuất: snake_case* — nhất quán với spec YAML và Python hook names đang có.

5. **Secret handling trong action `fill`?**
   - Log step: `text_typed="***"` khi `InputField.type=secret`. Đã có pattern này ở [ai_tool_web/models.py:123](ai_tool_web/models.py#L123) (`_SECRET_FIELD_NAMES`) — reuse.
   - Artifact upload: `spec.type=secret` → không gửi lên CDN.
   - *Đề xuất: reuse logic cũ + mở rộng cho `InputField.type`.*

---

## 6. Rủi ro & cách giảm

| Rủi ro | Mitigation |
|--------|------------|
| Matcher `text_any` fail trên trang dynamic (React render async) | Mỗi action fill/click bọc `wait_for` ngầm 3s; admin có thể thêm `wait_for` tường minh. |
| Flow dài brittle — 1 step fail là toàn flow fail | `failure` rule + `on_error: ask_user` step-level; cho phép admin define fallback. |
| Spec v2 phức tạp → migration đau | `mode` default = `agent` cho spec v1 → không migration nào cần. Chỉ spec mới dùng `mode=flow`. |
| UI builder tốn công → chưa có thì PM/QA vẫn phải viết YAML | Sprint 1–2 ship YAML + docs + example library, UI là Sprint 3+. Admin giỏi YAML vẫn tốt hơn không có gì. |
| Action set không đủ phủ case thực | Mỗi action mới là 1 file Python nhỏ → thêm dễ, review nhanh. |

---

## 7. Metrics — định nghĩa "xong"

- **Sprint 1 done:** 1 scenario login đơn giản chạy bằng YAML, 0 LLM call, pass unit + 1 E2E.
- **Sprint 2 done:** Flow `login_with_otp.yaml` mẫu chạy thật (trên staging site), conditional OTP, screenshot theo step, failure rule rõ ràng.
- **Sprint 3 done:** Admin có thể tạo scenario mới qua Postman mà không mở editor code, dry-run cho preview, audit log.
- **Sprint 4 done:** Non-dev ghé UI tạo 1 flow đơn giản trong <5 phút mà không hỏi dev.

---

## 8. Ước lượng nhân sự

- Sprint 1: 1 BE senior, 1 tuần.
- Sprint 2: 1 BE + 1 QA viết flow mẫu, 1 tuần.
- Sprint 3: 1 BE + 0.3 PM feedback loop, 1 tuần.
- Sprint 4: 1 FE + 1 BE, 2 tuần.

Tổng 5 tuần-người cho đến tầng UI builder. Sprint 1–2 là giá trị lõi (~60% benefit).

---

## 9. Next action

**Chờ user chốt 5 câu hỏi ở §5.** Sau khi có đáp án, mình sẽ:
1. Update `PLAN_SCENARIO_V2.md` fix những chỗ cần thay đổi theo quyết định.
2. Bắt tay viết Sprint 1 (theo plan trên): `flow_models.py` + action registry + 5 action + flow_runner + `login_basic.yaml` + tests.

Không code trước cho đến khi 5 câu trên có answer.
