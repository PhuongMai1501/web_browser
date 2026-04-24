"""
Test — YAML normalizer

Chạy:
  cd deploy_server/ai_tool_web
  python tests/test_yaml_normalizer.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.yaml_normalizer import normalize_yaml  # noqa: E402


_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def _check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        _FAIL.append((label, detail))
        print(f"  [FAIL] {label}{(' -> ' + detail) if detail else ''}")


def test_happy_path():
    print("=== 1. HAPPY PATH ===")
    yaml = """
id: test_scn
display_name: Test Scenario
start_url: https://example.com
allowed_domains: [example.com]
inputs:
  - name: keyword
    type: string
    required: true
    source: context
goal: Tìm {keyword}
"""
    r = normalize_yaml(yaml)
    _check("parse_ok", r.parse_ok)
    _check("validation_ok", r.validation_ok, str(r.errors))
    _check("spec loaded", r.spec is not None and r.spec.id == "test_scn")
    _check("normalized_json present", r.normalized_json is not None)
    _check("normalized has display_name",
           r.normalized_json.get("display_name") == "Test Scenario")
    _check("yaml_hash set", len(r.yaml_hash) == 64)
    _check("no errors", len(r.errors) == 0)


def test_yaml_parse_error():
    print("\n=== 2. YAML PARSE ERROR ===")
    r = normalize_yaml("!!! not valid yaml\n-- broken ---:\n  x: [")
    _check("parse_ok=False", not r.parse_ok)
    _check("validation_ok=False", not r.validation_ok)
    _check("spec is None", r.spec is None)
    _check("errors has yaml field",
           any(e.field == "<yaml>" for e in r.errors))


def test_yaml_not_dict():
    print("\n=== 3. YAML NOT DICT ===")
    r = normalize_yaml("- item1\n- item2\n")
    _check("parse_ok=False khi là list", not r.parse_ok)
    _check("error mention mapping/dict",
           any("mapping" in e.message.lower() or "dict" in e.message.lower()
               for e in r.errors))


def test_pydantic_validation_fail():
    print("\n=== 4. PYDANTIC VALIDATION FAIL ===")
    # Thiếu required field 'id'
    yaml = """
display_name: Missing id
"""
    r = normalize_yaml(yaml)
    _check("parse_ok=True (YAML parse được)", r.parse_ok)
    _check("validation_ok=False (thiếu id)", not r.validation_ok)
    _check("errors không rỗng", len(r.errors) > 0)
    _check("spec là None khi validation fail", r.spec is None)


def test_hook_whitelist():
    print("\n=== 5. HOOK WHITELIST ===")
    # Hook không tồn tại trong HOOK_REGISTRY
    yaml = """
id: bad_hook
display_name: Bad Hook Test
hooks:
  pre_check: nonexistent_hook_xyz
"""
    r = normalize_yaml(yaml)
    _check("parse + pydantic OK", r.parse_ok)
    _check("validation_ok=False do hook invalid", not r.validation_ok)
    _check("error mention hook",
           any("hook" in e.message.lower() for e in r.errors))


def test_credential_protection_secret_default():
    print("\n=== 6. CREDENTIAL PROTECTION — secret default ===")
    yaml = """
id: cred_test
display_name: Cred Test
inputs:
  - name: password
    type: secret
    default: "leaked_password_123"
    source: context
"""
    r = normalize_yaml(yaml)
    _check("parse OK", r.parse_ok)
    _check("validation_ok=False do secret có default",
           not r.validation_ok)
    _check("error mention secret/password",
           any("secret" in e.message.lower() or "password" in e.message.lower()
               for e in r.errors))


def test_credential_protection_by_name():
    print("\n=== 7. CREDENTIAL PROTECTION — name heuristic ===")
    # Field type=string nhưng tên 'api_key' → vẫn bị coi là secret
    yaml = """
id: cred_name_test
display_name: Cred Name Test
inputs:
  - name: api_key
    type: string
    default: "sk-real-key-123"
    source: context
"""
    r = normalize_yaml(yaml)
    _check("secret-like name trigger check", not r.validation_ok)


def test_credential_warning_regex():
    print("\n=== 8. CREDENTIAL WARNING — regex secret detect ===")
    # Tên không giống secret nhưng default LOOK like secret
    yaml = """
id: warn_test
display_name: Warn Test
inputs:
  - name: user_token
    type: string
    default: "AKIAIOSFODNN7EXAMPLE"
    source: context
"""
    r = normalize_yaml(yaml)
    # 'user_token' matches regex "token" → hard error (§3.5 tên có token coi như secret)
    _check("name with 'token' blocked (hard)", not r.validation_ok)


def test_force_id_override():
    print("\n=== 9. FORCE ID OVERRIDE ===")
    yaml = """
id: user_wrote_this
display_name: Override Test
"""
    r = normalize_yaml(yaml, force_id="user_hiepqn_auto_generated")
    _check("id bị override",
           r.spec and r.spec.id == "user_hiepqn_auto_generated")


def test_force_builtin_skips_security():
    print("\n=== 10. FORCE BUILTIN SKIPS SECURITY ===")
    # Builtin scenario với hook name chưa register vẫn OK (trusted source)
    yaml = """
id: builtin_trusted
display_name: Trusted
hooks:
  pre_check: future_hook_not_registered_yet
"""
    r = normalize_yaml(yaml, force_builtin=True)
    _check("builtin bỏ qua hook whitelist", r.validation_ok)
    _check("builtin=True trong spec", r.spec and r.spec.builtin is True)


def test_hash_consistency():
    print("\n=== 11. HASH CONSISTENCY ===")
    yaml = "id: same\ndisplay_name: Same\n"
    r1 = normalize_yaml(yaml)
    r2 = normalize_yaml(yaml)
    _check("same input -> same hash", r1.yaml_hash == r2.yaml_hash)

    r3 = normalize_yaml(yaml + "# tweak\n")
    _check("different input -> different hash", r1.yaml_hash != r3.yaml_hash)


def test_empty_yaml():
    print("\n=== 12. EMPTY YAML ===")
    r = normalize_yaml("")
    _check("empty YAML → parse_ok False", not r.parse_ok)

    r2 = normalize_yaml("   \n\n  ")
    _check("whitespace-only → parse_ok False", not r2.parse_ok)


def main():
    groups = [
        test_happy_path,
        test_yaml_parse_error,
        test_yaml_not_dict,
        test_pydantic_validation_fail,
        test_hook_whitelist,
        test_credential_protection_secret_default,
        test_credential_protection_by_name,
        test_credential_warning_regex,
        test_force_id_override,
        test_force_builtin_skips_security,
        test_hash_consistency,
        test_empty_yaml,
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
