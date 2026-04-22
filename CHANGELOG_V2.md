# Changelog Scenario v2 — Sprint 1

> Sprint 1 của plan trong [PLAN_SCENARIO_V2.md](PLAN_SCENARIO_V2.md).
> Mục tiêu: scenario khai báo step-by-step bằng YAML chạy được, 0 LLM, 0 hook.

## TL;DR

- Thêm field `mode: flow | agent | hybrid` vào `ScenarioSpec`. Default `agent` → v1 + `chang_login` không đổi.
- Thêm 6 action chuẩn (declarative): `goto`, `wait_for`, `fill`, `click`, `ask_user`, `if_visible`.
- Flow runner mới dispatch theo `mode`; spec khai báo `steps + success + failure + inputs`.
- Snapshot matcher không cần CSS — hỗ trợ `text_any` / `label_any` / `placeholder_any` / `role` / `nth`.
- Admin API tự động validate action names + input references.
- Builtin YAML `login_basic.yaml` ship sẵn để test thật.
- 18 unit test xanh.

## Defaults đã chốt (các câu hỏi mở trong PLAN_SCENARIO_V2.md §5)

| # | Câu hỏi | Chốt |
|---|---------|------|
| 1 | Default `mode` | `agent` (back-compat) |
| 2 | `chang_login` giữ hooks? | Giữ nguyên mode=agent + hooks — không port sang flow ở Sprint 1 |
| 3 | Matcher engine | Parse accessibility snapshot thuần Python (đủ text/label/placeholder/role) |
| 4 | Action naming | `snake_case` |
| 5 | Secret handling | Reuse pattern `_SECRET_FIELD_NAMES`; `InputField.type=secret` → mask thành `***` trong log/SSE |

---

## Files tạo mới

| Path | Vai trò |
|------|---------|
| [LLM_base/scenarios/flow_models.py](LLM_base/scenarios/flow_models.py) | Pydantic: `InputField`, `TargetSpec`, `FlowStep`, `SuccessRule`, `FailureRule`, `Condition` |
| [LLM_base/scenarios/action_registry.py](LLM_base/scenarios/action_registry.py) | `ACTION_REGISTRY` + `@action` decorator + `ActionResult` |
| [LLM_base/scenarios/snapshot_query.py](LLM_base/scenarios/snapshot_query.py) | Parse snapshot + `find_ref`/`find_refs` theo `TargetSpec` |
| [LLM_base/scenarios/flow_runner.py](LLM_base/scenarios/flow_runner.py) | `run_flow()` generator — thực thi steps, xử lý ask_user, check success/failure |
| [LLM_base/scenarios/actions/__init__.py](LLM_base/scenarios/actions/__init__.py) | Import tất cả action để register |
| [LLM_base/scenarios/actions/goto.py](LLM_base/scenarios/actions/goto.py) | open_url + wait load |
| [LLM_base/scenarios/actions/wait_for.py](LLM_base/scenarios/actions/wait_for.py) | Poll snapshot đến khi target xuất hiện / timeout |
| [LLM_base/scenarios/actions/fill.py](LLM_base/scenarios/actions/fill.py) | type_text + mask secret |
| [LLM_base/scenarios/actions/click.py](LLM_base/scenarios/actions/click.py) | click_element + invalidate snapshot |
| [LLM_base/scenarios/actions/ask_user.py](LLM_base/scenarios/actions/ask_user.py) | Yield `is_blocked=True` StepRecord, nhận answer qua `gen.send()` |
| [LLM_base/scenarios/actions/if_visible.py](LLM_base/scenarios/actions/if_visible.py) | Branch then/else theo visibility |
| [LLM_base/scenarios/builtin/login_basic.yaml](LLM_base/scenarios/builtin/login_basic.yaml) | Builtin flow mẫu: email + password + OTP optional |
| [ai_tool_web/tests/test_flow_v2.py](ai_tool_web/tests/test_flow_v2.py) | 18 unit test cho parser/models/runner/validator |

## Files sửa đổi

| Path | Thay đổi |
|------|----------|
| [LLM_base/scenarios/spec.py](LLM_base/scenarios/spec.py) | Thêm `mode`, `inputs`, `steps`, `success`, `failure`. `context_schema` giữ cho back-compat |
| [LLM_base/scenarios/generic_runner.py](LLM_base/scenarios/generic_runner.py) | Dispatch theo `mode`. Flow branch: set_allowed_domains, open start_url, gọi `run_flow` |
| [ai_tool_web/services/scenario_service.py](ai_tool_web/services/scenario_service.py) | Validator: hook names + action names + input references + step shape theo action |

**Không đổi:** [worker/job_handler.py](ai_tool_web/worker/job_handler.py), [api/routes/sessions.py](ai_tool_web/api/routes/sessions.py), [api/routes/scenarios.py](ai_tool_web/api/routes/scenarios.py), [api/app.py](ai_tool_web/api/app.py). Lý do: dispatch v1↔v2 hoàn toàn nằm trong `generic_runner`.

---

## Cấu trúc thư mục sau Sprint 1

```
deploy_server/
├── LLM_base/scenarios/
│   ├── spec.py                    ← mở rộng (mode/inputs/steps/success/failure)
│   ├── flow_models.py             ← MỚI (models v2)
│   ├── action_registry.py         ← MỚI
│   ├── snapshot_query.py          ← MỚI
│   ├── flow_runner.py             ← MỚI
│   ├── generic_runner.py          ← dispatch theo mode
│   ├── hooks_registry.py          ← v1 giữ nguyên
│   ├── actions/                   ← MỚI
│   │   ├── __init__.py
│   │   ├── goto.py
│   │   ├── wait_for.py
│   │   ├── fill.py
│   │   ├── click.py
│   │   ├── ask_user.py
│   │   └── if_visible.py
│   ├── hooks/                     ← v1 giữ nguyên
│   │   └── chang_login_hooks.py
│   └── builtin/
│       ├── chang_login.yaml       ← mode=agent (v1)
│       ├── custom.yaml            ← mode=agent (v1)
│       └── login_basic.yaml       ← MỚI mode=flow
├── ai_tool_web/
│   ├── services/scenario_service.py  ← validator mở rộng
│   └── tests/test_flow_v2.py         ← MỚI
```

---

## ScenarioSpec v2 — schema

```python
class ScenarioSpec(BaseModel):
    id: str
    display_name: str
    description: str = ""
    enabled: bool = True
    builtin: bool = False
    version: int = 1

    mode: Literal["flow", "agent", "hybrid"] = "agent"  # NEW

    start_url: str | None = None
    goal: str = ""
    max_steps_default: int = 20
    allowed_domains: list[str] = []

    inputs: list[InputField] = []                       # NEW (ưu tiên)
    steps: list[FlowStep] = []                          # NEW — bắt buộc khi mode=flow
    success: SuccessRule | None = None                  # NEW
    failure: FailureRule | None = None                  # NEW

    context_schema: dict = {}                           # back-compat
    system_prompt_extra: str = ""
    hooks: ScenarioHooks = ...
```

---

## Action reference (Sprint 1)

| Action | Field bắt buộc | Mô tả |
|--------|----------------|-------|
| `goto` | `url` | Mở URL, wait 1.5s |
| `wait_for` | `target` hoặc `timeout_ms` | Poll snapshot đến khi target xuất hiện, hoặc sleep |
| `fill` | `target`, (`value` hoặc `value_from`) | type_text; tự mask nếu InputField.type=secret |
| `click` | `target` | click_element; invalidate snapshot |
| `ask_user` | `field`, `prompt` | Yield ask event, chờ `gen.send(answer)` |
| `if_visible` | `target`, (`then` hoặc `else`) | Branch theo find_ref(target) |

### Target syntax

```yaml
target:
  role: textbox          # button | textbox | link | ...
  text_any: ["Đăng nhập", "Login"]
  label_any: ["Email", "Username"]
  placeholder_any: ["Nhập email"]
  css: "input[name='email']"   # escape hatch
  nth: 0
```

Matcher diacritic-insensitive — `"quen mat khau"` match `"Quên mật khẩu?"`.

### Success/Failure rule

```yaml
success:
  any_of:
    - url_contains: /dashboard
    - text_any: ["Welcome", "Hi!"]
    - element_visible:
        role: link
        text_any: ["Logout"]
  all_of: []   # optional, tất cả phải pass

failure:
  any_of:
    - text_any: ["Invalid password"]
  code: AUTH_FAILED
  message: "Sai mật khẩu"
```

---

## Ví dụ YAML đầy đủ

[LLM_base/scenarios/builtin/login_basic.yaml](LLM_base/scenarios/builtin/login_basic.yaml):

```yaml
id: login_basic
display_name: "Login cơ bản (mode=flow)"
mode: flow
start_url: https://fpt.net/login
allowed_domains:
  - fpt.net

inputs:
  - {name: email, type: string, required: true, source: context}
  - {name: password, type: secret, required: true, source: context}
  - {name: otp, type: string, required: false, source: ask_user}

steps:
  - action: wait_for
    target: {role: textbox, label_any: [Email, Tên đăng nhập, Username]}
    timeout_ms: 8000

  - action: fill
    target: {role: textbox, label_any: [Email]}
    value_from: email

  - action: fill
    target: {role: textbox, label_any: [Password, Mật khẩu]}
    value_from: password

  - action: click
    target: {role: button, text_any: [Login, Đăng nhập, Sign in]}

  - action: if_visible
    target: {text_any: [OTP, Mã xác thực]}
    then:
      - action: ask_user
        field: otp
        prompt: "Vui lòng nhập mã OTP"
      - action: fill
        target: {role: textbox, label_any: [OTP, Mã xác thực]}
        value_from: otp
      - action: click
        target: {role: button, text_any: [Verify, Xác nhận]}

success:
  any_of:
    - url_contains: /dashboard
    - text_any: [Welcome, Xin chào]

failure:
  any_of:
    - text_any: ["Sai mật khẩu"]
  code: AUTH_FAILED
  message: "Đăng nhập thất bại"
```

---

## Backward compatibility

- Spec v1 không có `mode` → Pydantic default `"agent"` → route về `run_agent_autonomous` + hooks (y hệt trước).
- `chang_login` + `custom` không bị đụng vào.
- UI/SSE shape không đổi — mọi action v2 được map về `click`/`type`/`wait`/`ask`/`done` trong StepRecord.action.action (xem `flow_runner._translate_action`). Debug field thêm `flow_action` để phân biệt.

---

## API admin cho flow

Endpoints giữ nguyên từ v1:

```bash
# List
curl -H "X-Admin-Token: $ADMIN_TOKEN" http://SERVER:9000/v1/scenarios

# Tạo scenario flow mới
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d @my_flow.json http://SERVER:9000/v1/scenarios

# Validate không tạo
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d @my_flow.json http://SERVER:9000/v1/scenarios/my_flow/dry-run
```

Validator mới sẽ reject (422) trước khi save nếu:
- `mode=flow` nhưng `steps` rỗng
- Action name không có trong `ACTION_REGISTRY`
- `value_from` reference field không tồn tại trong `inputs`
- `ask_user` thiếu `field`, `goto` thiếu `url`, `if_visible` thiếu `target`/branch

---

## Commit

Code Sprint 1 chưa commit. Sau khi review, chạy tương tự PR trước:
```bash
git add LLM_base/scenarios/flow_models.py \
        LLM_base/scenarios/action_registry.py \
        LLM_base/scenarios/snapshot_query.py \
        LLM_base/scenarios/flow_runner.py \
        LLM_base/scenarios/actions/ \
        LLM_base/scenarios/builtin/login_basic.yaml \
        LLM_base/scenarios/spec.py \
        LLM_base/scenarios/generic_runner.py \
        ai_tool_web/services/scenario_service.py \
        ai_tool_web/tests/test_flow_v2.py \
        CHANGELOG_V2.md TEST_V2.md

git -c user.name="PhuongMai1501" -c user.email="hiepqn@fpt.com" commit -m "feat(v2): mode=flow with declarative steps + action engine

Sprint 1 per PLAN_SCENARIO_V2.md. Adds ScenarioSpec.mode routing,
6 built-in actions (goto/wait_for/fill/click/ask_user/if_visible),
snapshot matcher (text_any/label_any/placeholder_any/role),
success/failure rules, login_basic.yaml builtin. Back-compat:
spec cũ default mode='agent' → v1 behavior không đổi.

18 unit tests xanh."
```

---

## Còn gì — Sprint 2 tiếp theo

Từ [PLAN_SCENARIO_V2.md §4](PLAN_SCENARIO_V2.md):
- [ ] Action `assert` + `extract` (Sprint 2)
- [ ] Per-step timeout + retry policy cho `fill`/`click` (hiện chỉ retry count, chưa wrap `wait_for` ngầm)
- [ ] Screenshot per-step khi chạy flow (hiện StepRecord có screenshot_path rỗng)
- [ ] `hybrid` mode runner (Sprint 3)
- [ ] Admin dry-run với context giả → preview bước nào chạy (hiện dry-run chỉ validate spec)
- [ ] Audit log admin (scenario_audit:<id>)
