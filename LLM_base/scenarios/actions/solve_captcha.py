"""Action: solve_captcha — đọc CAPTCHA bằng vision LLM (OCR), điền, submit, retry.

Pipeline mỗi lần thử:
    [chụp viewport] → [tự crop ảnh captcha] → [OCR GPT-4o đọc mã, GIỮ hoa/thường]
      → [điền bằng eval_js value-setter + dispatch input/change — Angular/React-safe]
      → [submit] → [verify] → nếu sai: [reroll 'Thay đổi'] → lặp tối đa max_attempts.

Vì sao điền bằng eval_js thay vì chỉ CDP type_text: site SPA (Angular/React) chỉ
nhận giá trị khi có event `input`/`change`; set `.value` trần không cập nhật model
→ submit gửi mã rỗng. eval_js dùng native value setter + dispatch events nên framework
nhận đúng, và giữ nguyên HOA/thường (captcha phân biệt hoa/thường).

Verify TỔNG QUÁT (dùng lại mọi site):
- `_DEFAULT_FAIL_PATTERNS` (lỗi captcha phổ biến VN+EN) dựng sẵn; `verify_fail_any`
  của scenario chỉ BỔ SUNG.
- `verify_success_any` (optional): nếu khai báo → THẨM QUYỀN, chỉ True khi success
  text xuất hiện. Dùng khi cần chắc chắn (site hiện kết quả rõ ràng).
- Không success_any: submit xong KHÔNG thấy lỗi = đúng.

Kết quả action nhồi chuỗi chẩn đoán (OCR code, fill/submit status, verdict) vào
`reason`/`error` để log session đọc được mà không cần log container.

YAML tối thiểu:

```yaml
- action: solve_captcha
  fill_target: { role: textbox, text_any: ["Nhập mã xác minh"] }
  verify_success_any: ["CHI TIẾT HÓA ĐƠN"]   # optional, tăng độ chắc
  max_attempts: 4
  vision_model: gpt-4o
```
Fill + submit + locate ảnh captcha đều có built-in JS robust (không cần khai báo).
Override khi cần: `submit_eval_js`, `reload_eval_js`, `crop_selector`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import tempfile

from ..action_registry import ActionResult, action
from ..snapshot_query import describe_target, find_ref

_log = logging.getLogger(__name__)

# Chờ sau submit để trang xử lý + render kết quả / toast lỗi (SPA cần thời gian).
_VERIFY_WAIT_MS = 2500
# Chờ sau khi reroll captcha để ảnh mới load.
_RELOAD_WAIT_MS = 1000
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)

# Default fail patterns — lỗi captcha PHỔ BIẾN (VN+EN). page_contains_any đã
# case + diacritic-insensitive. Scenario THÊM (merge) qua verify_fail_any.
_DEFAULT_FAIL_PATTERNS = (
    "mã xác minh không chính xác", "mã xác minh không đúng",
    "mã xác nhận không chính xác", "mã xác nhận không đúng",
    "mã captcha không đúng", "mã bảo mật không đúng",
    "sai mã xác minh", "nhập sai mã", "vui lòng nhập đúng mã",
    "invalid captcha", "incorrect captcha", "wrong captcha",
    "captcha is incorrect", "invalid verification code",
    "incorrect verification code", "verification code is incorrect",
)

# Keyword nhận diện ô input captcha (placeholder/aria/name/id chứa các từ này).
_CAPTCHA_INPUT_KW = "['xác minh','xac minh','captcha','mã','minh','code']"

# JS: tìm ô input captcha (theo keyword), set value + dispatch events SPA-safe.
# __CODE__ thay bằng JSON string literal của mã (giữ nguyên hoa/thường).
_FILL_JS = """
(function(){
  var v=__CODE__;
  function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
  var kw=__KW__;
  var ins=Array.prototype.slice.call(document.querySelectorAll('input')).filter(function(i){
    var t=(i.type||'text').toLowerCase();
    return ['text','tel','number',''].indexOf(t)!==-1 && !i.disabled && vis(i);
  });
  function score(i){
    var s=((i.placeholder||'')+' '+(i.getAttribute('aria-label')||'')+' '+(i.name||'')+' '+(i.id||'')).toLowerCase();
    for(var k=0;k<kw.length;k++) if(s.indexOf(kw[k])!==-1) return 1;
    return 0;
  }
  ins.sort(function(a,b){return score(b)-score(a);});
  var inp=ins[0];
  if(!inp) return 'NOINPUT';
  inp.focus();
  var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  setter.call(inp,v);
  inp.dispatchEvent(new Event('input',{bubbles:true}));
  inp.dispatchEvent(new Event('change',{bubbles:true}));
  inp.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true}));
  return 'FILLED('+(inp.placeholder||inp.name||inp.id||'?')+')='+inp.value;
})()
""".replace("__KW__", _CAPTCHA_INPUT_KW)

# JS: tìm + click nút search (nút rỗng/ngắn-text SAU ô captcha, hoặc submit/“Tìm”).
_SUBMIT_JS = """
(function(){
  function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
  var kw=__KW__;
  var ins=Array.prototype.slice.call(document.querySelectorAll('input')).filter(vis);
  var inp=ins.filter(function(i){
    var s=((i.placeholder||'')+(i.getAttribute('aria-label')||'')+(i.name||'')+(i.id||'')).toLowerCase();
    for(var k=0;k<kw.length;k++) if(s.indexOf(kw[k])!==-1) return true;
    return false;
  })[0]||ins[0];
  if(!inp) return 'NOINPUT';
  var bs=Array.prototype.slice.call(document.querySelectorAll('button,input[type=submit],a[role=button]')).filter(function(b){return !b.disabled&&vis(b);});
  var after=bs.filter(function(b){return inp.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING;});
  var pick=after.filter(function(b){var t=(b.innerText||b.textContent||'').trim();return t.length<=2||/tìm|search|tra c|xác nh/i.test(t);})[0];
  if(!pick) pick=document.querySelector('button[type=submit]:not([disabled])');
  if(!pick && after.length) pick=after[0];
  if(!pick) return 'NOBTN';
  pick.click();
  return 'CLICKED('+((pick.innerText||pick.textContent||'').trim().slice(0,14)||'icon')+')';
})()
""".replace("__KW__", _CAPTCHA_INPUT_KW)

# JS: định vị ảnh captcha → bbox. Ứng viên: img/canvas/svg + div có background-image
# (nhiều site render captcha bằng background). Anchor: ô input captcha, hoặc text
# "Thay đổi" (link reload luôn cạnh captcha). Chọn ứng viên kích-thước-captcha gần
# anchor nhất.
_LOCATE_CAPTCHA_JS = """
(function(){
  function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
  var kw=__KW__;
  var ins=Array.prototype.slice.call(document.querySelectorAll('input')).filter(vis);
  var inp=ins.filter(function(i){
    var s=((i.placeholder||'')+(i.getAttribute('aria-label')||'')+(i.name||'')+(i.id||'')).toLowerCase();
    for(var k=0;k<kw.length;k++) if(s.indexOf(kw[k])!==-1) return true;
    return false;
  })[0];
  var anchorEl=inp;
  if(!anchorEl){
    anchorEl=Array.prototype.slice.call(document.querySelectorAll('a,p,span,div,button')).filter(function(e){
      var t=(e.textContent||'').trim().toLowerCase();
      return t==='thay đổi'||t==='đổi mã'||t==='refresh'||t==='làm mới';
    })[0];
  }
  var a=anchorEl?anchorEl.getBoundingClientRect():null;
  var cs=[];
  Array.prototype.slice.call(document.querySelectorAll('img,canvas,svg')).forEach(function(e){cs.push(e);});
  Array.prototype.slice.call(document.querySelectorAll('div,span,a,button')).forEach(function(e){
    try{var bg=getComputedStyle(e).backgroundImage;if(bg&&bg!=='none'&&bg.indexOf('url')!==-1)cs.push(e);}catch(_){}
  });
  cs=cs.filter(function(im){var r=im.getBoundingClientRect();return r.width>=30&&r.width<=520&&r.height>=12&&r.height<=200&&vis(im);});
  if(!cs.length) return 'NONE';
  var best=cs[0];
  if(a){var bd=1e9;cs.forEach(function(im){var r=im.getBoundingClientRect();
    var d=Math.abs((r.top+r.bottom)/2-(a.top+a.bottom)/2)+Math.abs(r.left-a.left);
    if(d<bd){bd=d;best=im;}});}
  var r=best.getBoundingClientRect();
  return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height,dpr:window.devicePixelRatio||1});
})()
""".replace("__KW__", _CAPTCHA_INPUT_KW)


@action("solve_captcha")
def run_solve_captcha(rt, step) -> ActionResult:
    api_key = getattr(rt, "api_key", "") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return ActionResult(
            ok=False, action_type="solve_captcha",
            error="OPENAI_API_KEY chưa cấu hình — không gọi được vision OCR",
        )

    from services.captcha_solver import read_captcha_text

    max_attempts = max(1, int(step.max_attempts or 4))
    fail_any = _DEFAULT_FAIL_PATTERNS + tuple(step.verify_fail_any or [])
    success_any = tuple(step.verify_success_any or [])
    # Cost optimization: thử model RẺ (gpt-4o-mini) cho `cheap_attempts` lần đầu,
    # fail thì escalate sang model MẠNH (gpt-4o). Mỗi lần là captcha mới (reroll).
    strong_model = step.vision_model or os.getenv("CAPTCHA_MODEL", "gpt-4o")
    cheap_model = step.vision_model_cheap or ""
    cheap_n = step.cheap_attempts if step.cheap_attempts is not None else 2
    url_before = _safe_url(rt.browser)
    last_code = ""
    diag: list[str] = []   # chẩn đoán nhồi vào result để log session đọc được

    # ── MANUAL MODE ──────────────────────────────────────────────────────────
    # Resume từ ask_user: nếu context[field] đã có mã user nhập → dùng luôn, bỏ
    # qua OCR. (Step solve_captcha đặt SAU step hỏi sẽ rơi vào nhánh này.)
    if step.field and str(rt.context.get(step.field, "")).strip():
        code = str(rt.context[step.field]).strip()
        rt.context[step.field] = ""   # consume — tránh dùng lại lần sau
        # Kiểm tra captcha có ĐỔI giữa lúc hỏi user và lúc submit không (race).
        changed = ""
        ask_hash = rt.context.pop("__captcha_ask_hash", "")
        if step.captcha_src_selector and ask_hash:
            cur, _ = _extract_img_src(rt, step.captcha_src_selector, 1)
            cur_hash = _file_hash(cur) if cur else ""
            if cur_hash and cur_hash != ask_hash:
                changed = f" CAPTCHA_CHANGED(ask={ask_hash},now={cur_hash})"
            elif cur_hash:
                changed = " CAPTCHA_SAME"
        fill_out, submit_out = _do_fill_submit(rt, step, code)
        _wait(rt, _VERIFY_WAIT_MS)
        rt.last_snapshot = ""
        verdict = _verify(rt, fail_any, success_any)
        d = f"manual:code={code} | fill={_short(fill_out)} | submit={_short(submit_out)} | verdict={verdict}{changed}"
        if fill_out.startswith("NOINPUT"):
            return ActionResult(
                ok=False, action_type="solve_captcha", text_typed=code,
                error="Manual fill NOINPUT — không thấy ô nhập. [" + d + "]",
                url_before=url_before,
            )
        if verdict is False:
            return ActionResult(
                ok=False, action_type="solve_captcha", text_typed=code,
                error="Mã user nhập vẫn sai / không ra kết quả. [" + d + "]",
                url_before=url_before,
            )
        return ActionResult(
            ok=True, action_type="solve_captcha", text_typed=code,
            url_before=url_before, url_after=_safe_url(rt.browser),
            reason="Captcha (user nhập tay) hợp lệ [" + d + "]",
        )

    # manual_only: step chỉ để tiêu thụ mã user (sau ask_user). Không có mã →
    # no-op (path auto-success đã xong ở step trước, step này bị bỏ qua logic).
    if step.manual_only:
        return ActionResult(
            ok=True, action_type="solve_captcha",
            reason="manual_only: chưa có mã user nhập → bỏ qua",
            url_before=url_before,
        )

    for attempt in range(1, max_attempts + 1):
        # 1+2. Lấy ảnh captcha để OCR theo thứ tự ưu tiên:
        #   (a) captcha_src_selector → trích THẲNG src <img> (data URI base64) →
        #       ảnh gốc sạch nhất, KHÔNG cần screenshot/crop.
        #   (b) screenshot + crop_selector / auto-locate.
        #   (c) full viewport.
        ocr_path = ""
        crop_tag = ""
        acq = ""   # chẩn đoán cách lấy ảnh
        # (a) Trích THẲNG src <img> (data URI) — ảnh gốc sạch nhất.
        if step.captcha_src_selector:
            sp, acq = _extract_img_src(rt, step.captcha_src_selector, attempt)
            if sp:
                ocr_path = sp
                crop_tag = "src"
        if not ocr_path:
            shot_path = _capture(rt, attempt)
            if not shot_path:
                return ActionResult(
                    ok=False, action_type="solve_captcha",
                    error="Không lấy được ảnh captcha (src fail + screenshot fail). "
                    + acq + " | " + " | ".join(diag), url_before=url_before,
                )
            ocr_path = shot_path
            crop_tag = "full"
            # (b) Crop bbox — ưu tiên crop_selector, rồi captcha_src_selector (crop
            #     chính ảnh đó), rồi auto-locate. Chỉ cần toạ độ nhỏ, không lo cắt chuỗi.
            box = None
            crop_sel = step.crop_selector or step.captcha_src_selector
            if crop_sel:
                box = _bbox_from_js(rt, _selector_bbox_js(crop_sel))
                acq = (acq + f"|cropsel={'box' if box else 'nobox'}").lstrip("|")
            if box is None:
                box = _bbox_from_js(rt, _LOCATE_CAPTCHA_JS)
                if box:
                    acq = (acq + "|autoloc=box").lstrip("|")
            if box:
                cropped = _crop_bbox(shot_path, box, attempt)
                if cropped:
                    ocr_path = cropped
                    crop_tag = "crop"

        # 3. OCR (giữ hoa/thường). Model: rẻ trước, escalate khi quá cheap_n lần.
        #    Khi KHÔNG crop được → gửi kèm ảnh hint khoanh đỏ (nếu có).
        use_model = cheap_model if (cheap_model and attempt <= cheap_n) else strong_model
        hint_url = step.captcha_image_hint if crop_tag == "full" else ""
        code = read_captcha_text(
            api_key=api_key, image_path=ocr_path,
            model=use_model, hint=step.note or "",
            hint_image_url=hint_url or "",
        )
        diag = [f"a{attempt}:ocr={code or '∅'}({crop_tag}{'+hint' if hint_url else ''},{use_model.split('-')[-1]})"]
        if acq:
            diag.append(f"acq[{acq}]")
        if not code:
            if not _reroll(rt, step, attempt, max_attempts):
                break
            continue
        last_code = code

        # 3b. Chống stale: captcha có thể đã refresh trong lúc gọi OCR (2-5s).
        #     Re-extract ngay trước submit; nếu ảnh đổi → OCR lại ảnh MỚI để
        #     submit đúng captcha hiện tại (không gửi mã của captcha cũ).
        if step.captcha_src_selector and crop_tag == "src":
            fresh, _fi = _extract_img_src(rt, step.captcha_src_selector, attempt)
            if fresh and _file_hash(fresh) != _file_hash(ocr_path):
                code2 = read_captcha_text(
                    api_key=api_key, image_path=fresh,
                    model=use_model, hint=step.note or "",
                )
                diag.append(f"recap({code}->{code2 or '∅'})")
                if code2:
                    code = code2
                    last_code = code2

        # 4+5. Điền (SPA-safe) + submit.
        fill_out, submit_out = _do_fill_submit(rt, step, code)
        diag.append(f"fill={_short(fill_out)}")
        if fill_out.startswith("NOINPUT"):
            return ActionResult(
                ok=False, action_type="solve_captcha", text_typed=code,
                error="Không tìm thấy ô nhập mã captcha (fill NOINPUT). "
                + " | ".join(diag), url_before=url_before,
            )
        diag.append(f"submit={_short(submit_out)}")
        _wait(rt, _VERIFY_WAIT_MS)
        rt.last_snapshot = ""

        # 6. Verify.
        verdict = _verify(rt, fail_any, success_any)
        diag.append(f"verdict={verdict}")
        if verdict is True:
            return ActionResult(
                ok=True, action_type="solve_captcha", text_typed=code,
                url_before=url_before, url_after=_safe_url(rt.browser),
                reason=(step.note or "Captcha hợp lệ")
                + f" (lần {attempt}/{max_attempts}) [{' | '.join(diag)}]",
            )

        _log.info("[%s] solve_captcha %s → reroll", rt.session_id, " ".join(diag))
        if not _reroll(rt, step, attempt, max_attempts):
            break

    # Hết lượt OCR. Nếu khai báo `field` → fallback HỎI USER nhập tay (human-in-
    # the-loop): pause flow, user nhìn captcha trên màn hình gõ mã. Answer ghi
    # vào context[field]; step kế tiếp (fill value_from=field + submit) xử lý.
    if step.field:
        prompt = step.prompt or (
            f"OCR đọc captcha thất bại sau {max_attempts} lần "
            f"(mã đoán cuối: '{last_code or '∅'}'). Vui lòng nhìn ảnh captcha "
            "và nhập mã (PHÂN BIỆT hoa/thường):"
        )
        # Trích ảnh captcha HIỆN TẠI ngay trước khi hỏi → user thấy đúng captcha
        # sẽ được submit (không stale do reroll trước đó). Captcha không TTL nên
        # ảnh này khớp với cái step manual sẽ điền vào.
        ask_img = ""
        if step.captcha_src_selector:
            ask_img, _ = _extract_img_src(rt, step.captcha_src_selector, 0)
            # Lưu hash để step manual kiểm tra captcha có đổi trong lúc user gõ.
            rt.context["__captcha_ask_hash"] = _file_hash(ask_img)
        if not ask_img:
            ask_img = _capture(rt, 0)   # fallback: screenshot tươi
        return ActionResult(
            ok=True, action_type="solve_captcha",
            ask_user=True, ask_field=step.field, ask_prompt=prompt,
            ask_image_path=ask_img,
            reason=f"Captcha OCR fail {max_attempts} lần → hỏi user. [{' | '.join(diag)}]",
            url_before=url_before,
        )

    return ActionResult(
        ok=False, action_type="solve_captcha", text_typed=last_code,
        url_before=url_before,
        error=(
            f"Captcha fail sau {max_attempts} lần. Mã cuối='{last_code or '∅'}'. "
            f"[{' | '.join(diag)}]. Nếu fill/submit báo NOINPUT/NOBTN → DOM khác "
            "dự đoán (chỉnh selector/JS). Nếu OCR sai liên tục → kiểm tra crop / "
            "đổi vision_model=gpt-4o, hoặc thêm `field` để hỏi user nhập tay."
        ),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _do_fill_submit(rt, step, code: str):
    """Điền mã (SPA-safe) + submit. Trả (fill_out, submit_out).

    Fill: CDP type_text (best-effort) + eval_js value-setter + dispatch events
    (Angular/React nhận đúng, giữ hoa/thường). Submit: submit_eval_js override
    hoặc built-in robust JS.
    """
    snap = _snapshot(rt)
    if step.fill_target is not None:
        fref = find_ref(snap, step.fill_target)
        if fref:
            try:
                rt.browser.type_text(fref, code)
            except Exception:
                pass
    fill_out = _eval(rt, _FILL_JS.replace("__CODE__", json.dumps(code)))
    submit_out = _eval(rt, step.submit_eval_js or _SUBMIT_JS)
    return fill_out, submit_out


def _extract_img_src(rt, selector: str, attempt: int):
    """Trích src của <img> captcha → (path PNG, info chẩn đoán).

    Nếu src là data URI base64 → decode trực tiếp (ảnh gốc sạch, cách tốt nhất).
    Nếu src là URL http(s) → fetch (qua proxy env). Fail → ("", info).
    `info` cho biết eval trả gì (để debug khi log: NONE / data:image len=.. / fetch).
    """
    js = (
        "(function(){var e=document.querySelector(" + json.dumps(selector) + ");"
        "return e?(e.currentSrc||e.src||e.getAttribute('src')||'NOSRC'):'NOEL';})()"
    )
    src = _eval(rt, js)
    if not src or src.startswith(("NOEL", "NOSRC", "ERR")):
        return "", f"src:{_short(src) or 'empty'}"

    kind = src[:22]
    out_dir = rt.run_dir if getattr(rt, "run_dir", None) is not None else None
    out_path = (
        str(out_dir / f"captcha_src_{rt.step_count:02d}_{attempt}.png")
        if out_dir else
        os.path.join(tempfile.gettempdir(), f"captcha_src_{attempt}.png")
    )

    try:
        if src.startswith("data:image"):
            b64 = src.split(",", 1)[1]
            data = base64.b64decode(b64)
            info = f"data,len={len(src)},bytes={len(data)}"
        elif src.startswith(("http://", "https://")):
            import requests
            r = requests.get(src, timeout=15)
            r.raise_for_status()
            data = r.content
            info = f"http,bytes={len(data)}"
        else:
            return "", f"src:unknown({kind})"
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path, f"src:ok({info})"
    except Exception as e:
        _log.info("solve_captcha: extract img src fail: %s", e)
        return "", f"src:fail({kind},{type(e).__name__})"


def _capture(rt, attempt: int) -> str:
    save_path = None
    if getattr(rt, "run_dir", None) is not None:
        save_path = str(rt.run_dir / f"captcha_{rt.step_count:02d}_{attempt}.png")
    try:
        _, path = rt.browser.take_screenshot(save_path=save_path, full_page=False)
        return path
    except TypeError:
        try:
            _, path = rt.browser.take_screenshot(save_path=save_path)
            return path
        except Exception as e:
            _log.warning("solve_captcha: take_screenshot fail: %s", e)
            return ""
    except Exception as e:
        _log.warning("solve_captcha: take_screenshot fail: %s", e)
        return ""


def _selector_bbox_js(selector: str) -> str:
    return (
        "(function(){var el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return 'NONE';var r=el.getBoundingClientRect();"
        "return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height,"
        "dpr:window.devicePixelRatio||1});})()"
    )


def _bbox_from_js(rt, js: str):
    """Chạy JS trả bbox JSON → dict, hoặc None."""
    raw = _eval(rt, js)   # đã gỡ JSON quote → raw là chuỗi JSON {...}
    if not raw or raw.startswith(("NONE", "ERR")):
        return None
    m = _JSON_OBJ_RE.search(raw)
    if not m:
        return None
    try:
        box = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not box.get("w") or not box.get("h"):
        return None
    return box


def _crop_bbox(shot_path: str, box: dict, attempt: int) -> str:
    """Crop ảnh theo bbox (CSS px × dpr). Trả path crop hoặc ""."""
    try:
        from PIL import Image
    except Exception:
        return ""
    dpr = float(box.get("dpr") or 1)
    pad = 5 * dpr
    left = max(0, int(box["x"] * dpr - pad))
    top = max(0, int(box["y"] * dpr - pad))
    right = int((box["x"] + box["w"]) * dpr + pad)
    bottom = int((box["y"] + box["h"]) * dpr + pad)
    out_path = shot_path.replace(".png", f"_crop{attempt}.png")
    try:
        with Image.open(shot_path) as im:
            right = min(right, im.width)
            bottom = min(bottom, im.height)
            if right <= left or bottom <= top:
                return ""
            crop = im.crop((left, top, right, bottom))
            # Phóng to để GPT-4o phán đoán CHIỀU CAO tương đối (hoa/thường) tốt
            # hơn. Target chiều cao ~160px, cap 4x, LANCZOS giữ nét.
            h = crop.height or 1
            factor = min(4.0, max(1.0, 160.0 / h))
            if factor > 1.05:
                crop = crop.resize(
                    (int(crop.width * factor), int(crop.height * factor)),
                    Image.LANCZOS,
                )
            crop.save(out_path)
        return out_path
    except Exception as e:
        _log.info("solve_captcha: crop fail (%s)", e)
        return ""


def _verify(rt, fail_any: tuple, success_any: tuple):
    """True=đúng, False=sai, None=không xác định.

    - fail text xuất hiện → False.
    - success_any khai báo → THẨM QUYỀN: chỉ True khi success text xuất hiện.
    - không success_any → vắng lỗi = True.
    """
    page_has = getattr(rt.browser, "page_contains_any", None)
    if page_has is None:
        return None
    try:
        if fail_any and page_has(tuple(fail_any)):
            return False
        if success_any:
            return True if page_has(tuple(success_any)) else False
        return True
    except Exception as e:
        _log.info("solve_captcha: page_contains_any fail: %s", e)
        return None


def _reroll(rt, step, attempt: int, max_attempts: int) -> bool:
    """Lấy captcha mới. reload_eval_js → else reload_target → else built-in 'Thay đổi'."""
    if attempt >= max_attempts:
        return False
    js = step.reload_eval_js or _RELOAD_JS
    if step.reload_eval_js or step.reload_target is None:
        out = _eval(rt, js)
        _log.info("[%s] solve_captcha reload → %r", rt.session_id, _short(out))
        if out.startswith(("NORELOAD", "ERR")):
            # built-in không thấy 'Thay đổi' và không có reload_target → dừng.
            if step.reload_target is None:
                return False
        else:
            _wait(rt, _RELOAD_WAIT_MS)
            rt.last_snapshot = ""
            return True
    # reload_target (accessibility) fallback
    snap = _snapshot(rt, fresh=True)
    ref = find_ref(snap, step.reload_target)
    if ref is None:
        return False
    try:
        rt.browser.click_element(ref)
        _wait(rt, _RELOAD_WAIT_MS)
        rt.last_snapshot = ""
        return True
    except Exception:
        return False


# JS reroll built-in: click phần tử có text đúng "Thay đổi" (paragraph onclick-only).
_RELOAD_JS = (
    "(function(){var es=Array.prototype.slice.call("
    "document.querySelectorAll('a,p,span,div,button'));"
    "var el=es.filter(function(e){return (e.innerText||e.textContent||'').trim()==='Thay đổi';})[0];"
    "if(!el)return 'NORELOAD';el.click();return 'RELOADED';})()"
)


def _eval(rt, js: str) -> str:
    """Chạy JS, trả giá trị string ĐÃ gỡ lớp JSON quote.

    agent-browser eval bọc giá trị string trong JSON quotes (vd trả về
    '"data:image/png;base64,.."' hoặc '"CLICKED"'). Gỡ 1 lớp để code dùng trực
    tiếp (startswith, decode...). Nếu không bọc thì giữ nguyên.
    """
    try:
        out = rt.browser.eval_js(js)
    except Exception as e:
        return f"ERR:{type(e).__name__}"
    out = (out or "").strip()
    if len(out) >= 2 and out[0] == '"' and out[-1] == '"':
        try:
            return json.loads(out)
        except Exception:
            return out[1:-1]
    return out


def _short(s: str) -> str:
    return (s or "")[:50]


def _file_hash(path: str) -> str:
    """sha1 ngắn của file ảnh — để so captcha có đổi không."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:10]
    except Exception:
        return ""


def _snapshot(rt, fresh: bool = False) -> str:
    if not fresh and getattr(rt, "last_snapshot", ""):
        return rt.last_snapshot
    try:
        snap = rt.browser.take_snapshot() or ""
    except Exception:
        snap = ""
    rt.last_snapshot = snap
    return snap


def _wait(rt, ms: int) -> None:
    try:
        rt.browser.wait_ms(ms)
    except Exception:
        pass


def _safe_url(browser) -> str:
    try:
        return browser.get_current_url()
    except Exception:
        return ""
