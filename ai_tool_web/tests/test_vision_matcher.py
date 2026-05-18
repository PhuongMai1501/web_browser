"""
Unit tests cho vision_matcher + FlowRuntime.find_ref_with_vision integration.

Cover:
- vision_matcher.find_ref_by_image: happy path, NOT_FOUND, hallucinated ref,
  network fail, OpenAI API fail.
- FlowRuntime.find_ref_with_vision:
  * text match success → vision không gọi
  * text fail + image_hint + cap OK → vision gọi → return ref
  * text fail + no image_hint → None (no vision call)
  * cap exceeded → None
  * no api_key → None
  * no run_dir → None
  * vision returns None → counter still incremented

Chạy:
  cd ai_tool_web
  python tests/test_vision_matcher.py
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Path setup
_THIS = Path(__file__).resolve().parent
_ROOT = _THIS.parent
_LLM = _ROOT.parent / "LLM_base"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_LLM))


class _FakeBrowser:
    """In-memory browser stub — copy from test_flow_v2."""

    def __init__(self):
        self.url = "https://example.com"
        self.snapshot_text = ""

    def take_snapshot(self):
        return self.snapshot_text

    def take_screenshot(self, save_path, full_page=False):
        # Tạo file empty để satisfy path-exists check downstream
        Path(save_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return None, save_path

    def get_current_url(self):
        return self.url

    def wait_ms(self, ms):
        pass


# Install fake browser_adapter trước khi import LLM_base modules
_fake_browser = _FakeBrowser()
_stub = types.ModuleType("browser_adapter")
for name in dir(_fake_browser):
    if not name.startswith("_"):
        setattr(_stub, name, getattr(_fake_browser, name))
sys.modules["browser_adapter"] = _stub


from scenarios.flow_models import TargetSpec  # noqa: E402
from scenarios.flow_runner import FlowRuntime  # noqa: E402
from services.vision_matcher import (  # noqa: E402
    _build_prompt,
    find_ref_by_image,
)


SAMPLE_SNAPSHOT = """
button "Tải về" [ref=e10]
button "Tải về" [ref=e25]
link "Trang chủ" [ref=e1]
""".strip()


# ── vision_matcher pure function tests ──────────────────────────────────────

class TestVisionMatcher(unittest.TestCase):
    def test_build_prompt_includes_snapshot(self):
        prompt = _build_prompt("button [ref=e1]", "click tải về")
        self.assertIn("button [ref=e1]", prompt)
        self.assertIn("click tải về", prompt)
        self.assertIn("NOT_FOUND", prompt)

    def test_build_prompt_truncates_long_snapshot(self):
        big = "x" * 20000
        prompt = _build_prompt(big, "")
        # Snapshot bị cắt 8000 chars
        self.assertLess(len(prompt), 9000)

    def test_no_api_key_returns_none(self):
        result = find_ref_by_image(
            api_key="",
            current_screenshot_path="/tmp/x.png",
            hint_image_url="https://cdn/h.png",
            snapshot_text=SAMPLE_SNAPSHOT,
        )
        self.assertIsNone(result)

    @patch("services.vision_matcher._fetch_image_b64")
    @patch("services.vision_matcher.OpenAI")
    def test_happy_path(self, mock_openai_cls, mock_fetch):
        mock_fetch.return_value = "fakeb64"
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="e10"))]
        )
        mock_openai_cls.return_value = mock_client

        result = find_ref_by_image(
            api_key="sk-test",
            current_screenshot_path="/tmp/x.png",
            hint_image_url="https://cdn/h.png",
            snapshot_text=SAMPLE_SNAPSHOT,
        )
        self.assertEqual(result, "e10")

    @patch("services.vision_matcher._fetch_image_b64")
    @patch("services.vision_matcher.OpenAI")
    def test_not_found_response(self, mock_openai_cls, mock_fetch):
        mock_fetch.return_value = "fakeb64"
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="NOT_FOUND"))]
        )
        mock_openai_cls.return_value = mock_client

        result = find_ref_by_image(
            api_key="sk-test",
            current_screenshot_path="/tmp/x.png",
            hint_image_url="https://cdn/h.png",
            snapshot_text=SAMPLE_SNAPSHOT,
        )
        self.assertIsNone(result)

    @patch("services.vision_matcher._fetch_image_b64")
    @patch("services.vision_matcher.OpenAI")
    def test_hallucinated_ref(self, mock_openai_cls, mock_fetch):
        """LLM trả ref không có trong snapshot → trả None."""
        mock_fetch.return_value = "fakeb64"
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="e999"))]
        )
        mock_openai_cls.return_value = mock_client

        result = find_ref_by_image(
            api_key="sk-test",
            current_screenshot_path="/tmp/x.png",
            hint_image_url="https://cdn/h.png",
            snapshot_text=SAMPLE_SNAPSHOT,
        )
        self.assertIsNone(result)

    @patch("services.vision_matcher._fetch_image_b64")
    def test_load_image_fail_returns_none(self, mock_fetch):
        mock_fetch.side_effect = Exception("network fail")
        result = find_ref_by_image(
            api_key="sk-test",
            current_screenshot_path="/tmp/x.png",
            hint_image_url="https://cdn/h.png",
            snapshot_text=SAMPLE_SNAPSHOT,
        )
        self.assertIsNone(result)

    @patch("services.vision_matcher._fetch_image_b64")
    @patch("services.vision_matcher.OpenAI")
    def test_api_call_fail_returns_none(self, mock_openai_cls, mock_fetch):
        mock_fetch.return_value = "fakeb64"
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("rate limit")
        mock_openai_cls.return_value = mock_client

        result = find_ref_by_image(
            api_key="sk-test",
            current_screenshot_path="/tmp/x.png",
            hint_image_url="https://cdn/h.png",
            snapshot_text=SAMPLE_SNAPSHOT,
        )
        self.assertIsNone(result)


# ── FlowRuntime.find_ref_with_vision tests ──────────────────────────────────

class TestFlowRuntimeFindRefWithVision(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.run_dir = Path(self.tmpdir.name)
        self.fake_browser = _FakeBrowser()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_rt(
        self, *, api_key="sk-test", cap=5, run_dir=None,
    ) -> FlowRuntime:
        return FlowRuntime(
            browser=self.fake_browser, spec=None, context={},
            session_id="sess-1",
            run_dir=run_dir if run_dir is not None else self.run_dir,
            api_key=api_key,
            vision_calls_cap=cap,
        )

    def test_text_match_success_no_vision(self):
        rt = self._make_rt()
        target = TargetSpec(
            role="button", text_any=["Tải về"],
            image_hint="https://cdn/foo.png",  # vẫn có nhưng không cần dùng
        )
        with patch(
            "services.vision_matcher.find_ref_by_image"
        ) as mock_vision:
            ref = rt.find_ref_with_vision(target, SAMPLE_SNAPSHOT)
        self.assertEqual(ref, "e10")  # match nth=0
        self.assertEqual(rt.vision_calls_used, 0)
        # Vision không được gọi
        self.assertEqual(mock_vision.call_count, 0)

    def test_text_fail_no_image_hint_no_vision(self):
        rt = self._make_rt()
        target = TargetSpec(role="button", text_any=["NotInSnapshot"])
        with patch("services.vision_matcher.find_ref_by_image") as mock_v:
            ref = rt.find_ref_with_vision(target, SAMPLE_SNAPSHOT)
        self.assertIsNone(ref)
        self.assertEqual(rt.vision_calls_used, 0)
        self.assertEqual(mock_v.call_count, 0)

    def test_text_fail_with_image_hint_calls_vision(self):
        rt = self._make_rt()
        target = TargetSpec(
            role="button",
            text_any=["NotInSnapshot"],
            image_hint="https://cdn/h.png",
            image_hint_desc="download tab",
        )
        with patch("services.vision_matcher.find_ref_by_image",
                   return_value="e25") as mock_v:
            ref = rt.find_ref_with_vision(target, SAMPLE_SNAPSHOT)
        self.assertEqual(ref, "e25")
        self.assertEqual(rt.vision_calls_used, 1)
        self.assertEqual(mock_v.call_count, 1)
        kwargs = mock_v.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "sk-test")
        self.assertEqual(kwargs["hint_image_url"], "https://cdn/h.png")
        self.assertEqual(kwargs["description"], "download tab")

    def test_cap_exceeded_no_vision(self):
        rt = self._make_rt(cap=2)
        rt.vision_calls_used = 2  # đã dùng hết
        target = TargetSpec(
            role="button", text_any=["NotInSnapshot"],
            image_hint="https://cdn/h.png",
        )
        with patch("services.vision_matcher.find_ref_by_image") as mock_v:
            ref = rt.find_ref_with_vision(target, SAMPLE_SNAPSHOT)
        self.assertIsNone(ref)
        self.assertEqual(rt.vision_calls_used, 2)  # không tăng
        self.assertEqual(mock_v.call_count, 0)

    def test_no_api_key_no_vision(self):
        rt = self._make_rt(api_key="")
        target = TargetSpec(
            role="button", text_any=["NotInSnapshot"],
            image_hint="https://cdn/h.png",
        )
        with patch("services.vision_matcher.find_ref_by_image") as mock_v:
            ref = rt.find_ref_with_vision(target, SAMPLE_SNAPSHOT)
        self.assertIsNone(ref)
        self.assertEqual(mock_v.call_count, 0)

    def test_no_run_dir_no_vision(self):
        rt = self._make_rt(run_dir=None)
        rt.run_dir = None
        target = TargetSpec(
            role="button", text_any=["NotInSnapshot"],
            image_hint="https://cdn/h.png",
        )
        with patch("services.vision_matcher.find_ref_by_image") as mock_v:
            ref = rt.find_ref_with_vision(target, SAMPLE_SNAPSHOT)
        self.assertIsNone(ref)
        self.assertEqual(mock_v.call_count, 0)

    def test_vision_returns_none_counter_still_incremented(self):
        """Vision call dù fail (NOT_FOUND/hallucinated/network) cũng tốn cost
        → counter phải tăng để cap chính xác."""
        rt = self._make_rt(cap=3)
        target = TargetSpec(
            role="button", text_any=["NotInSnapshot"],
            image_hint="https://cdn/h.png",
        )
        with patch(
            "services.vision_matcher.find_ref_by_image", return_value=None,
        ):
            ref = rt.find_ref_with_vision(target, SAMPLE_SNAPSHOT)
        self.assertIsNone(ref)
        self.assertEqual(rt.vision_calls_used, 1)

    def test_vision_match_recorded_in_telemetry(self):
        rt = self._make_rt()
        rt.step_count = 7
        target = TargetSpec(
            role="button", text_any=["NotInSnapshot"],
            image_hint="https://cdn/h.png",
        )
        with patch("services.vision_matcher.find_ref_by_image",
                   return_value="e10"):
            rt.find_ref_with_vision(target, SAMPLE_SNAPSHOT)
        self.assertEqual(len(rt.vision_matches), 1)
        match = rt.vision_matches[0]
        self.assertEqual(match["step_index"], 7)
        self.assertEqual(match["hint_url"], "https://cdn/h.png")
        self.assertEqual(match["ref_returned"], "e10")
        self.assertTrue(match["screenshot_path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
