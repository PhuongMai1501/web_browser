"""
Test — Inputs validator (G5 runtime context validation)
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

_LLM_BASE = Path(__file__).parent.parent.parent / "LLM_base"
if str(_LLM_BASE) not in sys.path:
    sys.path.append(str(_LLM_BASE))

from scenarios.spec import ScenarioSpec  # noqa: E402
from services.inputs_validator import (  # noqa: E402
    InputValidationError,
    validate_inputs,
)


_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def _check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        _FAIL.append((label, detail))
        print(f"  [FAIL] {label}{(' -> ' + detail) if detail else ''}")


def _spec(inputs: list[dict]) -> ScenarioSpec:
    return ScenarioSpec(
        id="t", display_name="t", inputs=inputs
    )


def test_required_missing():
    print("=== 1. REQUIRED MISSING ===")
    spec = _spec([
        {"name": "keyword", "type": "string", "required": True, "source": "context"}
    ])
    try:
        validate_inputs(spec, {})
        _check("raise khi thiếu required", False)
    except InputValidationError as e:
        _check("raise khi thiếu required", True)
        _check("error mention field name",
               any(x["field"] == "keyword" for x in e.errors))


def test_required_empty_string():
    print("\n=== 2. REQUIRED EMPTY STRING ===")
    spec = _spec([
        {"name": "keyword", "type": "string", "required": True, "source": "context"}
    ])
    try:
        validate_inputs(spec, {"keyword": ""})
        _check("raise khi empty string", False)
    except InputValidationError:
        _check("raise khi empty string", True)

    try:
        validate_inputs(spec, {"keyword": "   "})
        _check("raise khi whitespace-only", False)
    except InputValidationError:
        _check("raise khi whitespace-only", True)


def test_default_fallback():
    print("\n=== 3. DEFAULT FALLBACK ===")
    spec = _spec([
        {"name": "count", "type": "number", "required": False,
         "source": "context", "default": 10}
    ])
    result = validate_inputs(spec, {})
    _check("default dùng khi value thiếu", result.context["count"] == 10.0)


def test_coerce_string():
    print("\n=== 4. COERCE STRING ===")
    spec = _spec([
        {"name": "q", "type": "string", "required": True, "source": "context"}
    ])
    result = validate_inputs(spec, {"q": "hello"})
    _check("string pass through", result.context["q"] == "hello")

    # int input → str
    result = validate_inputs(spec, {"q": 42})
    _check("int coerced to str", result.context["q"] == "42")


def test_coerce_number():
    print("\n=== 5. COERCE NUMBER ===")
    spec = _spec([
        {"name": "n", "type": "number", "required": True, "source": "context"}
    ])
    result = validate_inputs(spec, {"n": "3.14"})
    _check("string -> number", result.context["n"] == 3.14)

    result = validate_inputs(spec, {"n": 42})
    _check("int -> number", result.context["n"] == 42.0)

    # Invalid string → error
    try:
        validate_inputs(spec, {"n": "abc"})
        _check("reject non-numeric string", False)
    except InputValidationError:
        _check("reject non-numeric string", True)

    # bool should be rejected (ambiguous)
    try:
        validate_inputs(spec, {"n": True})
        _check("reject bool for number", False)
    except InputValidationError:
        _check("reject bool for number", True)


def test_coerce_bool():
    print("\n=== 6. COERCE BOOL ===")
    spec = _spec([
        {"name": "flag", "type": "bool", "required": True, "source": "context"}
    ])
    for truthy in [True, 1, "true", "True", "1", "yes", "y"]:
        r = validate_inputs(spec, {"flag": truthy})
        _check(f"truthy {truthy!r} -> True", r.context["flag"] is True)

    for falsy in [False, 0, "false", "False", "0", "no", "n"]:
        r = validate_inputs(spec, {"flag": falsy})
        _check(f"falsy {falsy!r} -> False", r.context["flag"] is False)


def test_secret_type():
    print("\n=== 7. SECRET TYPE ===")
    spec = _spec([
        {"name": "password", "type": "secret", "required": True, "source": "context"}
    ])
    r = validate_inputs(spec, {"password": "mypass"})
    _check("secret coerced to str", r.context["password"] == "mypass")


def test_ask_user_skipped():
    print("\n=== 8. ASK_USER FIELDS SKIPPED ===")
    spec = _spec([
        {"name": "keyword", "type": "string", "required": True, "source": "context"},
        {"name": "otp", "type": "string", "required": True, "source": "ask_user"},
    ])
    r = validate_inputs(spec, {"keyword": "x"})
    _check("ask_user không trong context", "otp" not in r.context)
    _check("ask_user_fields list",
           r.ask_user_fields == ["otp"])


def test_extra_fields_dropped():
    print("\n=== 9. EXTRA FIELDS DROPPED ===")
    spec = _spec([
        {"name": "a", "type": "string", "required": True, "source": "context"}
    ])
    r = validate_inputs(spec, {"a": "x", "b": "y", "c": "z"})
    _check("chỉ field trong spec được giữ",
           set(r.context.keys()) == {"a"})


def test_optional_missing_no_default():
    print("\n=== 10. OPTIONAL MISSING NO DEFAULT ===")
    spec = _spec([
        {"name": "opt", "type": "string", "required": False, "source": "context"}
    ])
    r = validate_inputs(spec, {})
    _check("optional missing → skip, no key",
           "opt" not in r.context)


def test_none_inputs():
    print("\n=== 11. NONE INPUTS DICT ===")
    spec = _spec([
        {"name": "opt", "type": "string", "required": False, "source": "context"}
    ])
    r = validate_inputs(spec, None)
    _check("None inputs → empty context",
           r.context == {})


def test_no_inputs_in_spec():
    print("\n=== 12. SPEC WITHOUT INPUTS ===")
    spec = ScenarioSpec(id="t", display_name="t")
    r = validate_inputs(spec, {"anything": "ok"})
    _check("no inputs → empty result", r.context == {})


def test_multiple_errors_accumulated():
    print("\n=== 13. MULTIPLE ERRORS ACCUMULATED ===")
    spec = _spec([
        {"name": "a", "type": "string", "required": True, "source": "context"},
        {"name": "b", "type": "number", "required": True, "source": "context"},
        {"name": "c", "type": "bool", "required": True, "source": "context"},
    ])
    try:
        validate_inputs(spec, {"b": "not_a_number", "c": "not_a_bool"})
        _check("raise với multi errors", False)
    except InputValidationError as e:
        _check("raise với multi errors", True)
        _check("có ít nhất 2 errors", len(e.errors) >= 2,
               f"got {len(e.errors)}")


def main():
    groups = [
        test_required_missing,
        test_required_empty_string,
        test_default_fallback,
        test_coerce_string,
        test_coerce_number,
        test_coerce_bool,
        test_secret_type,
        test_ask_user_skipped,
        test_extra_fields_dropped,
        test_optional_missing_no_default,
        test_none_inputs,
        test_no_inputs_in_spec,
        test_multiple_errors_accumulated,
    ]
    for fn in groups:
        try:
            fn()
        except Exception:
            print(f"  [ERROR in {fn.__name__}]")
            traceback.print_exc()
            _FAIL.append((fn.__name__, "uncaught exception"))

    print(f"\n{'='*50}")
    print(f"Total: {len(_PASS)} pass, {len(_FAIL)} fail")
    if _FAIL:
        print("\nFailed:")
        for label, detail in _FAIL:
            print(f"  - {label}: {detail}")
        return 1
    print("\n[ALL PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
