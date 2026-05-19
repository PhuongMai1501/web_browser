"""
scenarios/snapshot_query.py — Matcher tìm ref element trong accessibility snapshot.

Snapshot của agent-browser có format mỗi dòng kiểu:
    button [ref=e11] "Đăng nhập"
    textbox [ref=e7] "Email address"
    link [ref=e5] (href=/auth, icon=key, aria-label=Login)
    generic [ref=e20]

Matcher parse thành record rồi khớp theo text_any / label_any /
placeholder_any / role. Không cần LLM cho case đơn giản.

Role compatibility (Cách 2a — cross-role fallback):
Nhiều site dùng ARIA role khác nhau cho cùng 1 loại UI element:
  - Google/Bing/YouTube search box → combobox (vì có autocomplete dropdown)
  - HTML5 search input semantic   → searchbox
  - Form input thường              → textbox
  - Rich editor (Gmail, Twitter)   → textbox (contenteditable)
  - Button styled as link / vice versa
LLM gen YAML thường đoán `role: textbox` mặc định → fail trên search engines.
Matcher tự fallback sang role compatible khi exact match rỗng (2-pass).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .flow_models import TargetSpec

_log = logging.getLogger(__name__)

# Role nào có thể thay thế role nào khi exact match fail. Pass 2 thử theo
# thứ tự — return ngay khi match đầu tiên có element.
# Lưu ý: role hai chiều — combobox fallback textbox và ngược lại — vì LLM
# có thể đoán nhầm cả 2 chiều.
_ROLE_FALLBACKS: dict[str, list[str]] = {
    "textbox":   ["combobox", "searchbox", "textarea"],
    "combobox":  ["textbox", "searchbox"],
    "searchbox": ["textbox", "combobox"],
    "textarea":  ["textbox"],
    "button":    ["link"],          # link styled as button
    "link":      ["button"],        # button styled as link
}


# Snapshot agent-browser format (accessibility tree, YAML-ish):
#   - button "Tìm kiếm" [ref=e71]
#   - textbox "Tên đăng nhập" [ref=e95]
#   - textbox [ref=e70]: < Tìm Văn bản Pháp luật >
#   - textbox [ref=e73]: QCVN 10:2025/BCA
#   - link "Trang chủ" [ref=e14]
#   - listitem [level=1, ref=e13] clickable [cursor:pointer]
#   - generic [ref=e20]
# Strategy: parse từng thành phần riêng, chấp nhận mọi thứ tự.

# Ref: `[ref=eN]` hoặc lồng trong attr block `[level=1, ref=e13]`
_REF_RE = re.compile(r"\bref=(e\d+)")

# Role: từ đầu dòng (bỏ `- ` leading) tới khi gặp `"`, `[`, hoặc khoảng trắng
_ROLE_RE = re.compile(r"^\s*-?\s*([a-zA-Z][\w-]*)")

# Name/label trong quotes — thường nằm ngay sau role
_QUOTED_RE = re.compile(r'"([^"]*)"')

# Value/placeholder sau `]: ` — dùng `< placeholder >` nếu trong angle brackets
_AFTER_COLON_RE = re.compile(r"\]\s*:\s*(.+?)\s*$")
_ANGLE_RE = re.compile(r"^<\s*(.+?)\s*>$")

# Attr block cũ: `(href=..., icon=..., placeholder=...)`
_PAREN_HINTS_RE = re.compile(r"\(([^)]*)\)")


@dataclass
class ElementRecord:
    ref: str
    role: str
    label: str = ""                              # chuỗi trong "" đầu tiên
    hints: dict[str, str] = field(default_factory=dict)
    raw_line: str = ""


def _strip_diacritics(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def _norm(s: str) -> str:
    """Lowercase + strip diacritics để so sánh không phân biệt dấu."""
    return _strip_diacritics(s).strip()


def _contains_any(haystack: str, needles: Iterable[str]) -> bool:
    if not haystack or not needles:
        return False
    hay_norm = _norm(haystack)
    hay_raw = haystack.lower()
    for n in needles:
        if not n:
            continue
        n_norm = _norm(n)
        if n_norm and n_norm in hay_norm:
            return True
        if n.lower() in hay_raw:
            return True
    return False


def _contains_all(haystack: str, needles: Iterable[str]) -> bool:
    """Haystack phải chứa TẤT CẢ needles (diacritic-insensitive).
    Rỗng haystack/needles → False."""
    if not haystack:
        return False
    needles = [n for n in needles if n]
    if not needles:
        return False
    hay_norm = _norm(haystack)
    hay_raw = haystack.lower()
    for n in needles:
        n_norm = _norm(n)
        if n_norm in hay_norm or n.lower() in hay_raw:
            continue
        return False
    return True


def parse_snapshot(snapshot: str) -> list[ElementRecord]:
    """Parse snapshot text → list ElementRecord. Bỏ qua dòng không có ref.

    Hỗ trợ cả format cũ (legacy synthetic: `button [ref=e1] "Label"`) lẫn
    format agent-browser thực tế (`- button "Label" [ref=e1]`,
    `- textbox [ref=e1]: < placeholder >`).
    """
    records: list[ElementRecord] = []
    for line in (snapshot or "").splitlines():
        ref_m = _REF_RE.search(line)
        if not ref_m:
            continue
        ref = ref_m.group(1)

        role_m = _ROLE_RE.match(line)
        role = role_m.group(1) if role_m else "generic"

        # Label: string trong quotes đầu tiên (không có → ''). Với textbox/searchbox
        # mà agent-browser dùng `: < placeholder >`, lấy phần trong angle brackets
        # làm label luôn — vì user quen coi placeholder-as-label.
        label = ""
        quoted_m = _QUOTED_RE.search(line)
        if quoted_m:
            label = quoted_m.group(1).strip()

        # Value/placeholder sau `]: `
        hints: dict[str, str] = {}
        after_m = _AFTER_COLON_RE.search(line)
        if after_m:
            after = after_m.group(1).strip()
            angle = _ANGLE_RE.match(after)
            if angle:
                # `: < placeholder >` → đây là placeholder
                placeholder = angle.group(1).strip()
                hints["placeholder"] = placeholder
                if not label:
                    label = placeholder
            else:
                # `: value` (input đã có giá trị)
                hints["value"] = after

        # Paren hints cũ (cho format legacy / enriched snapshot)
        for group in _PAREN_HINTS_RE.findall(line):
            for part in group.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    hints[k.strip().lower()] = v.strip()

        records.append(ElementRecord(
            ref=ref, role=role, label=label, hints=hints, raw_line=line,
        ))
    return records


def _element_text(rec: ElementRecord) -> str:
    """Gộp label + text/aria-label/title từ hints thành 1 string để search."""
    parts = [rec.label]
    for k in ("text", "aria-label", "title"):
        if rec.hints.get(k):
            parts.append(rec.hints[k])
    return " | ".join(p for p in parts if p)


def _match_one(rec: ElementRecord, target: TargetSpec) -> bool:
    if target.role and rec.role.lower() != target.role.lower():
        return False
    text_concat = _element_text(rec)
    if target.text_any:
        if not _contains_any(text_concat, target.text_any):
            return False
    if target.text_all:
        if not _contains_all(text_concat, target.text_all):
            return False
    if target.label_any:
        if not _contains_any(rec.label, target.label_any):
            return False
    if target.placeholder_any:
        placeholder = rec.hints.get("placeholder") or rec.hints.get("aria-label") or ""
        if not _contains_any(placeholder, target.placeholder_any):
            return False
    # css không check được ở layer này → ignore (action fill/click fallback
    # trực tiếp xuống browser_adapter.* với ref do user tự cung cấp).
    return True


def find_refs(snapshot: str, target: TargetSpec) -> list[str]:
    """Tìm tất cả ref khớp target. Trả list theo thứ tự xuất hiện trong snapshot.

    2-pass strategy (Cách 2a):
      1. Exact role match (giữ behavior cũ).
      2. Nếu pass 1 rỗng + target.role có fallback → thử lần lượt fallback roles.
         Mỗi fallback dùng chính TargetSpec với role override, tất cả filter
         khác (text_any, label_any, placeholder_any) giữ nguyên.

    Lý do: LLM gen YAML đôi khi đoán role sai (search Google = combobox chứ
    không phải textbox). Fallback tránh fail runtime + ép user phải sửa YAML.
    """
    if target.css:
        # Layer này không xử lý css → trả rỗng, caller có thể raise tuỳ context
        return []
    recs = parse_snapshot(snapshot)

    # Pass 1: exact role match
    exact = [r.ref for r in recs if _match_one(r, target)]
    if exact or not target.role:
        return exact

    # Pass 2: cross-role fallback. Chỉ kick in khi pass 1 rỗng + role có
    # fallback group. Giữ tất cả filter khác.
    fallbacks = _ROLE_FALLBACKS.get(target.role.lower(), [])
    for alt_role in fallbacks:
        target_alt = target.model_copy(update={"role": alt_role})
        refs = [r.ref for r in recs if _match_one(r, target_alt)]
        if refs:
            _log.info(
                "find_refs: cross-role fallback hit — target.role=%s → %s "
                "(matched %d refs)",
                target.role, alt_role, len(refs),
            )
            return refs
    return []


def find_ref(snapshot: str, target: TargetSpec) -> Optional[str]:
    """Tìm ref duy nhất theo target.nth. None nếu không match."""
    refs = find_refs(snapshot, target)
    if not refs:
        return None
    idx = target.nth if 0 <= target.nth < len(refs) else 0
    return refs[idx]


def lookup_element(snapshot: str, ref: str) -> Optional[ElementRecord]:
    """Tìm ElementRecord theo ref trong snapshot. None nếu không có.

    Dùng để extract text/label của element vừa match — UI hiển thị
    "đã click nút X" cho user dễ hiểu thay vì hiển thị ref selector.
    """
    if not ref:
        return None
    for rec in parse_snapshot(snapshot):
        if rec.ref == ref:
            return rec
    return None


def element_label(snapshot: str, ref: str) -> str:
    """Trả label readable của element theo ref.

    Ưu tiên: label > aria-label > text > placeholder. Rỗng nếu không tìm thấy.
    Dùng cho UI hiển thị "Tool-web vừa click nút <label>".
    """
    rec = lookup_element(snapshot, ref)
    if rec is None:
        return ""
    return _element_text(rec)


class TargetNotFound(RuntimeError):
    """Không tìm thấy element theo TargetSpec trong snapshot hiện tại."""


def describe_target(target: TargetSpec) -> str:
    """Mô tả ngắn TargetSpec cho log/lỗi."""
    parts = []
    if target.role: parts.append(f"role={target.role}")
    if target.text_any: parts.append(f"text_any={target.text_any}")
    if target.text_all: parts.append(f"text_all={target.text_all}")
    if target.label_any: parts.append(f"label_any={target.label_any}")
    if target.placeholder_any: parts.append(f"placeholder_any={target.placeholder_any}")
    if target.css: parts.append(f"css={target.css!r}")
    if target.nth: parts.append(f"nth={target.nth}")
    return ", ".join(parts) or "<empty>"
