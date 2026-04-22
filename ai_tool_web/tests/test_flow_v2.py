"""
Unit tests cho scenario v2: snapshot_query, flow_models, flow_runner.

Chạy:
  cd ai_tool_web && python -m pytest tests/test_flow_v2.py -v
  (hoặc chạy `python tests/test_flow_v2.py` cho mode standalone)
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path

# Path setup giống worker/browser_worker.py
_THIS = Path(__file__).resolve().parent
_ROOT = _THIS.parent                # ai_tool_web/
_LLM = _ROOT.parent / "LLM_base"
sys.path.insert(0, str(_ROOT))
sys.path.append(str(_LLM))


# ── Stub browser_adapter TRƯỚC khi import flow_runner ─────────────────────────

class _FakeBrowser:
    """In-memory browser: url + body_text + snapshot có thể set tay."""

    def __init__(self):
        self.url = "https://example.com/login"
        self.body_text = "Please login"
        self.snapshot = ""
        self.calls: list = []

    # Browser adapter API mà các action dùng
    def get_current_url(self):
        return self.url

    def get_page_title(self):
        return ""

    def take_snapshot(self):
        return self.snapshot

    def page_contains_any(self, texts):
        body = self.body_text.lower()
        return any(t.lower() in body for t in texts)

    def open_url(self, url):
        self.calls.append(("open_url", url))
        self.url = url

    def wait_ms(self, ms):
        self.calls.append(("wait_ms", ms))

    def click_element(self, ref):
        self.calls.append(("click", ref))

    def type_text(self, ref, text):
        self.calls.append(("type", ref, text))

    def set_allowed_domains(self, domains):
        self.calls.append(("set_allowed_domains", list(domains)))

    def reset_allowed_domains(self):
        self.calls.append(("reset_allowed_domains",))


def _install_fake_browser(fake: _FakeBrowser) -> None:
    stub = types.ModuleType("browser_adapter")
    for name in dir(fake):
        if not name.startswith("_"):
            setattr(stub, name, getattr(fake, name))
    sys.modules["browser_adapter"] = stub


# Dùng fake mặc định để import ban đầu không crash
_install_fake_browser(_FakeBrowser())

import scenarios.actions  # noqa: E402  (register actions)
from scenarios.action_registry import ACTION_REGISTRY  # noqa: E402
from scenarios.flow_models import (  # noqa: E402
    Condition,
    FailureRule,
    FlowStep,
    InputField,
    SuccessRule,
    TargetSpec,
)
from scenarios.flow_runner import run_flow  # noqa: E402
from scenarios.snapshot_query import find_ref, find_refs, parse_snapshot  # noqa: E402
from scenarios.spec import ScenarioSpec  # noqa: E402


SAMPLE_SNAPSHOT = """
  textbox [ref=e1] "Email address"
  textbox [ref=e2] "Password" (placeholder=nhap mat khau)
  button [ref=e3] "Login"
  button [ref=e4] (icon=key, aria-label=Đăng nhập Microsoft Azure)
  link [ref=e5] "Quên mật khẩu?"
  textbox [ref=e6] (placeholder=Mã OTP)
"""


# ── snapshot_query ────────────────────────────────────────────────────────────

class TestSnapshotQuery(unittest.TestCase):
    def test_parse_basic(self):
        recs = parse_snapshot(SAMPLE_SNAPSHOT)
        refs = {r.ref for r in recs}
        self.assertEqual(refs, {"e1", "e2", "e3", "e4", "e5", "e6"})

    def test_parse_hints(self):
        recs = {r.ref: r for r in parse_snapshot(SAMPLE_SNAPSHOT)}
        self.assertEqual(recs["e2"].hints.get("placeholder"), "nhap mat khau")
        self.assertEqual(recs["e4"].hints.get("icon"), "key")

    def test_match_label_any(self):
        t = TargetSpec(role="textbox", label_any=["Email"])
        self.assertEqual(find_ref(SAMPLE_SNAPSHOT, t), "e1")

    def test_match_text_any_uses_aria_label(self):
        t = TargetSpec(role="button", text_any=["Azure"])
        self.assertEqual(find_ref(SAMPLE_SNAPSHOT, t), "e4")

    def test_match_placeholder(self):
        t = TargetSpec(placeholder_any=["Mã OTP"])
        self.assertEqual(find_ref(SAMPLE_SNAPSHOT, t), "e6")

    def test_diacritic_insensitive(self):
        t = TargetSpec(text_any=["quen mat khau"])   # bỏ dấu
        self.assertEqual(find_ref(SAMPLE_SNAPSHOT, t), "e5")

    def test_no_match_returns_none(self):
        t = TargetSpec(text_any=["Không có element này"])
        self.assertIsNone(find_ref(SAMPLE_SNAPSHOT, t))

    def test_nth_pick(self):
        snap = '  button [ref=e1] "Go"\n  button [ref=e2] "Go"\n'
        t0 = TargetSpec(role="button", text_any=["Go"], nth=0)
        t1 = TargetSpec(role="button", text_any=["Go"], nth=1)
        self.assertEqual(find_ref(snap, t0), "e1")
        self.assertEqual(find_ref(snap, t1), "e2")

    def test_text_all_AND_logic(self):
        snap = (
            '- link "Thông tư 27/2025/TT-BCA về khí thải công nghiệp" [ref=e1]\n'
            '- link "Thông tư 08/2025/TT-BCA về cấu trúc dữ liệu" [ref=e2]\n'
            '- link "Thông tư 103/2025/TT-BCA về phòng cháy" [ref=e3]\n'
        )
        # Chỉ e1 chứa CẢ "27/2025" VÀ "khí thải"
        t = TargetSpec(role="link", text_all=["27/2025", "khí thải"])
        self.assertEqual(find_ref(snap, t), "e1")

        # Chỉ e2 chứa cả "08/2025" và "cấu trúc"
        t2 = TargetSpec(role="link", text_all=["08/2025", "cấu trúc"])
        self.assertEqual(find_ref(snap, t2), "e2")

        # Không link nào chứa cả "XYZ" và "khí thải"
        t3 = TargetSpec(role="link", text_all=["XYZ", "khí thải"])
        self.assertIsNone(find_ref(snap, t3))

        # Diacritic-insensitive: "khi thai" (no diacritics) match "khí thải"
        t4 = TargetSpec(role="link", text_all=["27/2025", "khi thai"])
        self.assertEqual(find_ref(snap, t4), "e1")


# ── flow_models validation ────────────────────────────────────────────────────

class TestFlowModels(unittest.TestCase):
    def test_target_requires_at_least_one_field(self):
        with self.assertRaises(Exception):
            TargetSpec()

    def test_flow_step_parse(self):
        step = FlowStep(action="fill",
                        target=TargetSpec(role="textbox", label_any=["Email"]),
                        value_from="email")
        self.assertEqual(step.action, "fill")
        self.assertEqual(step.target.label_any, ["Email"])

    def test_if_visible_else_alias(self):
        # 'else' là từ khoá Python → dùng alias
        step = FlowStep(
            action="if_visible",
            target=TargetSpec(text_any=["OTP"]),
            **{"else": [FlowStep(action="goto", url="https://x")]},
        )
        self.assertEqual(len(step.else_), 1)


# ── flow_runner E2E với fake browser ─────────────────────────────────────────

def _login_spec(failure_text: list[str] | None = None) -> ScenarioSpec:
    return ScenarioSpec(
        id="login_test",
        display_name="Login test",
        mode="flow",
        start_url="https://example.com/login",
        allowed_domains=["example.com"],
        inputs=[
            InputField(name="email", type="string", required=True, source="context"),
            InputField(name="password", type="secret", required=True, source="context"),
            InputField(name="otp", type="string", required=False, source="ask_user"),
        ],
        steps=[
            FlowStep(action="wait_for",
                     target=TargetSpec(role="textbox", label_any=["Email"]),
                     timeout_ms=3000, note="wait form"),
            FlowStep(action="fill",
                     target=TargetSpec(role="textbox", label_any=["Email"]),
                     value_from="email"),
            FlowStep(action="fill",
                     target=TargetSpec(role="textbox", label_any=["Password"]),
                     value_from="password"),
            FlowStep(action="click",
                     target=TargetSpec(role="button", text_any=["Login"])),
        ],
        success=SuccessRule(any_of=[
            Condition(url_contains="/dashboard"),
        ]),
        failure=FailureRule(
            any_of=[Condition(text_any=failure_text)] if failure_text else [],
            code="AUTH_FAILED", message="Invalid credentials",
        ),
    )


class TestFlowRunner(unittest.TestCase):
    def _setup_fake(self, snapshot=SAMPLE_SNAPSHOT, body="login"):
        fake = _FakeBrowser()
        fake.snapshot = snapshot
        fake.body_text = body
        _install_fake_browser(fake)
        # flow_runner đã cache import browser_adapter → phải reload module import đó
        # dưới path browser=... nên truyền trực tiếp.
        return fake

    def test_happy_path(self):
        fake = self._setup_fake()
        # Sau khi click, redirect sang /dashboard
        original_click = fake.click_element

        def _click_redirect(ref):
            original_click(ref)
            fake.url = "https://example.com/dashboard"

        fake.click_element = _click_redirect

        spec = _login_spec()
        gen = run_flow(
            spec,
            context={"email": "e@x", "password": "HUNTER2"},
            session_id="t",
            browser=fake,
        )
        records = list(gen)
        # Expect: wait_for, fill email, fill pwd, click, done
        self.assertEqual(len(records), 5)
        self.assertEqual(records[-1].action["action"], "done")
        # Password phải được mask trong record
        pwd_record = records[2]
        self.assertEqual(pwd_record.action.get("text"), "***")

    def test_failure_rule_triggers(self):
        fake = self._setup_fake(body="Sai mật khẩu hoặc email")
        spec = _login_spec(failure_text=["Sai mật khẩu"])
        gen = run_flow(spec,
                       context={"email": "e@x", "password": "WRONG"},
                       session_id="t", browser=fake)
        records = list(gen)
        last = records[-1]
        self.assertTrue(last.error)
        self.assertIn("AUTH_FAILED", last.action["message"])

    def test_ask_user_pause_resume(self):
        fake = self._setup_fake()
        spec = ScenarioSpec(
            id="ask_test", display_name="ask", mode="flow",
            steps=[
                FlowStep(action="ask_user", field="otp", prompt="Nhập OTP"),
            ],
        )
        gen = run_flow(spec, context={}, session_id="t", browser=fake)
        r = next(gen)
        self.assertTrue(r.is_blocked)
        self.assertEqual(r.action.get("ask_type"), "question")
        # Gửi answer → runner hoàn thành (hết steps)
        r2 = gen.send("123456")
        # Có thể là done fallback
        self.assertEqual(r2.action["action"], "done")

    def test_missing_target_fails_step(self):
        fake = self._setup_fake(snapshot="")   # snapshot rỗng
        spec = ScenarioSpec(
            id="x", display_name="x", mode="flow",
            steps=[FlowStep(action="wait_for",
                            target=TargetSpec(role="button", text_any=["Nowhere"]),
                            timeout_ms=500)],
        )
        gen = run_flow(spec, context={}, session_id="t", browser=fake)
        records = list(gen)
        # 1 record fail + 1 done fallback — nhưng flow_runner thoát sớm khi step fail
        self.assertTrue(any(r.error for r in records))


# ── Validator ────────────────────────────────────────────────────────────────

class TestValidator(unittest.TestCase):
    def test_flow_requires_steps(self):
        from services.scenario_service import ScenarioValidationError, validate_spec
        spec = ScenarioSpec(id="x", display_name="x", mode="flow")
        with self.assertRaises(ScenarioValidationError):
            validate_spec(spec)

    def test_unknown_action_rejected(self):
        from services.scenario_service import ScenarioValidationError, validate_spec
        spec = ScenarioSpec(
            id="x", display_name="x", mode="flow",
            steps=[FlowStep(action="doesnt_exist",
                            target=TargetSpec(role="button"))],
        )
        with self.assertRaises(ScenarioValidationError):
            validate_spec(spec)

    def test_value_from_unknown_input_rejected(self):
        from services.scenario_service import ScenarioValidationError, validate_spec
        spec = ScenarioSpec(
            id="x", display_name="x", mode="flow",
            inputs=[InputField(name="email", source="context")],
            steps=[FlowStep(action="fill",
                            target=TargetSpec(role="textbox", label_any=["x"]),
                            value_from="missing_field")],
        )
        with self.assertRaises(ScenarioValidationError):
            validate_spec(spec)


if __name__ == "__main__":
    unittest.main()
