"""
AI Form Filler Tool — FastAPI wrapper for an orchestrator agent

One-file service providing:
  - POST /fill       → navigate, (optional) login, auto-discover fields, fill+submit
  - POST /discover   → return discovered fields + inferred canonical keys (no submission)
  - GET  /health     → liveness check

Auth: set env API_KEY and include header `X-API-Key: <key>` on requests.

Run locally:
  pip install fastapi uvicorn playwright rapidfuzz python-dotenv "pydantic[email]" requests
  playwright install
  uvicorn app:app --host 0.0.0.0 --port 8000

Docker (optional):
  - See the Dockerfile snippet at the end of this file.

Notes:
  - This is headless by default. Set headless=False per request to visualize.
  - Files can be provided via base64 payloads.
  - Respect website ToS; do not attempt CAPTCHA/2FA circumvention.
"""
from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from rapidfuzz import fuzz

# -----------------------------
# Security helpers
# -----------------------------

def require_api_key(x_api_key: Optional[str]):
    expected = os.getenv("API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")

# -----------------------------
# Pydantic models
# -----------------------------

class Login(BaseModel):
    url: Optional[HttpUrl] = None
    username_selector: str
    password_selector: str
    submit_selector: str
    username: Optional[str] = None
    password: Optional[str] = None

class FileItem(BaseModel):
    # Provide files via base64 if needed (e.g., resume uploads)
    field_hint: Optional[str] = Field(
        default=None,
        description="Human description of what this file is (e.g., 'resume', 'id_document').",
    )
    filename: str
    content_b64: str

class SubmitHints(BaseModel):
    selector: Optional[str] = Field(
        default=None, description="CSS selector for the submit/continue button"
    )
    wait_selector: Optional[str] = Field(
        default=None, description="CSS selector expected to appear after submit"
    )

class FillOptions(BaseModel):
    headless: bool = True
    timeout_ms: int = Field(default=15000, ge=1000, le=120000)
    screenshot: bool = True
    locale: Optional[str] = None
    # Set to true to keep browser open for debugging (ignored in server mode)
    keep_open_debug: bool = False
    # Optional slow motion (milliseconds) to watch actions more clearly
    slow_mo_ms: int = 0

class FillRequest(BaseModel):
    url: HttpUrl
    data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value data to map into the form (e.g., first_name, email, phone, etc.)",
    )
    login: Optional[Login] = None
    files: List[FileItem] = Field(default_factory=list)
    submit: Optional[SubmitHints] = None
    options: FillOptions = Field(default_factory=FillOptions)

class FieldMatch(BaseModel):
    frame: str
    tag: str
    type: str
    label_text: str
    canonical_key: Optional[str]
    value_used: Optional[str]
    selector_preview: str
    filled: bool

class FillResponse(BaseModel):
    status: str
    url: str
    matched: List[FieldMatch]
    errors: List[str] = Field(default_factory=list)
    screenshot_path: Optional[str] = None
    notes: Optional[str] = None

class DiscoverResponse(BaseModel):
    url: str
    discovered: List[FieldMatch]

# -----------------------------
# Canonical keys and heuristics
# -----------------------------

CANONICAL_KEYS: Dict[str, List[str]] = {
    "first_name": ["first name", "given name", "forename", "fname"],
    "last_name": ["last name", "surname", "family name", "lname"],
    "email": ["email", "e-mail", "mail"],
    "phone": ["phone", "telephone", "mobile", "cell"],
    "company": ["company", "organization", "organisation", "employer"],
    "address_line1": ["address", "street", "address line 1", "addr1"],
    "city": ["city", "town"],
    "state": ["state", "region", "province", "county"],
    "postal_code": ["zip", "zipcode", "postal", "postcode"],
    "country": ["country", "nation"],
    "dob": ["date of birth", "birthday", "birth date", "dob"],
    # Booleans / agreements
    "accept_tos": ["terms", "tos", "agree", "agreement", "privacy"],
}

ALIASES: Dict[str, List[str]] = {
    "postal_code": ["zip", "zipcode", "post_code"],
    "address_line1": ["address1", "street", "line1"],
}

PHONE_RE = re.compile(r"[^\d+]")

# -----------------------------
# Core logic
# -----------------------------

# Site-specific helpers (Verint Cloud forms)
VERINT_HOSTS = ["verintcloudservices.com", "empro.verintcloudservices.com"]

def _is_verint(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc.lower()
        return any(h in netloc for h in VERINT_HOSTS)
    except Exception:
        return False

def _verint_start_flow(page, timeout_ms: int, logs: List[str], create_password: bool = False):
    """
    Best-effort: click Start if present. Some Verint flows show a password panel;
    this specific HSH form typically does not, but we handle it if it appears.
    """
    try:
        start_btn = page.get_by_role("button", name=re.compile(r"^start$", re.I))
        if start_btn.count() > 0 and start_btn.first.is_visible():
            start_btn.first.click()
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass
    except Exception as e:
        logs.append(f"Start button not found/failed: {e}")

    # Password panel handler (rare on this form, safe no-op otherwise)
    try:
        if page.get_by_text(re.compile(r"Please enter a password", re.I)).count() > 0:
            pwd = "A1" + str(int(time.time())) if create_password else "A1temporary"
            try:
                page.get_by_label(re.compile(r"^password$", re.I)).fill(pwd)
            except Exception:
                page.locator("input[type='password']").first.fill(pwd)
            try:
                page.get_by_label(re.compile(r"confirm password", re.I)).fill(pwd)
            except Exception:
                page.locator("input[type='password']").nth(1).fill(pwd)
            try:
                page.get_by_role("button", name=re.compile(r"save", re.I)).click()
            except Exception:
                page.locator("button:has-text('Save')").first.click()
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass
    except Exception as e:
        logs.append(f"Password step handling error: {e}")

def _candidate_text(page_el) -> str:
    txts: List[str] = []
    # associated <label for="id">
    try:
        lab = page_el.evaluate(
            """
        el => {
            const id = el.id;
            const lab = id ? document.querySelector(`label[for="${id}"]`) : null;
            return lab ? lab.innerText.trim() : null;
        }
        """
        )
        if lab:
            txts.append(lab)
    except Exception:
        pass

    for attr in ["aria-label", "aria-labelledby", "name", "id", "placeholder", "autocomplete"]:
        try:
            v = page_el.get_attribute(attr)
            if v:
                txts.append(v)
        except Exception:
            pass

    try:
        near = page_el.evaluate(
            """
        el => {
            let t = "";
            const prev = el.previousElementSibling;
            if (prev) t += (prev.innerText || "").trim();
            const p = el.parentElement;
            if (p) t += " " + ((p.innerText || "").trim());
            return t.slice(0, 300);
        }
        """
        )
        if near:
            txts.append(near)
    except Exception:
        pass

    return " ".join([t for t in txts if t]).strip()

def _guess_canonical_key(text: str) -> Optional[str]:
    t = (text or "").lower()
    if "email" in t:
        return "email"
    if re.search(r"\b(zip|postal|postcode)\b", t):
        return "postal_code"
    if re.search(r"\b(first|given|forename)\b", t):
        return "first_name"
    if re.search(r"\b(last|surname|family)\b", t):
        return "last_name"
    if re.search(r"\b(phone|mobile|cell|tel)\b", t):
        return "phone"
    if re.search(r"\b(company|organization|organisation|employer)\b", t):
        return "company"
    if re.search(r"\b(city|town)\b", t):
        return "city"
    if re.search(r"\b(state|region|province|county)\b", t):
        return "state"
    if re.search(r"\b(country|nation)\b", t):
        return "country"
    if re.search(r"\b(date of birth|birthday|dob)\b", t):
        return "dob"
    if re.search(r"\b(address|street)\b", t):
        return "address_line1"
    if re.search(r"\b(terms|privacy|agree|accept)\b", t):
        return "accept_tos"

    best = None
    best_score = -1
    for canon, syns in CANONICAL_KEYS.items():
        for s in [canon] + syns:
            score = fuzz.partial_ratio(t, s)
            if score > best_score:
                best_score, best = score, canon
    return best if best_score >= 65 else None

def _best_data_value(canon_key: str, data: Dict[str, Any]) -> Any:
    if canon_key in data:
        return data[canon_key]
    for a in ALIASES.get(canon_key, []):
        if a in data:
            return data[a]
    return None

def _normalize_value(key: str, value: Any) -> Any:
    if key == "phone" and isinstance(value, str):
        digits = PHONE_RE.sub("", value)
        return digits if digits.startswith("+") else ("+" + digits if digits else digits)
    return value

def _login_if_needed(page, login: Optional[Login], timeout_ms: int, logs: List[str]):
    if not login:
        return
    page.goto(str(login.url), wait_until="domcontentloaded")
    page.fill(login.username_selector, login.username or os.getenv("LOGIN_USERNAME", ""))
    page.fill(login.password_selector, login.password or os.getenv("LOGIN_PASSWORD", ""))
    page.click(login.submit_selector)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeout:
        logs.append("Post-login network idle not reached; continuing.")

def _discover_fields(page) -> List[Tuple[Any, Any, Dict[str, str]]]:
    frames = [page, *page.frames]
    discovered: List[Tuple[Any, Any, Dict[str, str]]] = []
    for f in frames:
        loc = f.locator("input, textarea, select").filter(has_not=f.locator("[type='hidden']"))
        count = 0
        try:
            count = loc.count()
        except Exception:
            pass
        for i in range(count):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue
            try:
                tag = el.evaluate("e=>e.tagName.toLowerCase()")
            except Exception:
                tag = ""
            typ = el.get_attribute("type") or ""
            meta = {
                "frame": f.name or "main",
                "tag": tag,
                "type": typ,
                "text": _candidate_text(el),
                "selector_preview": el.evaluate("e=>e.outerHTML.slice(0,200)") or "",
            }
            discovered.append((f, el, meta))
    return discovered

def _fill_discovered(page, discovered, data: Dict[str, Any], timeout_ms: int, logs: List[str]):
    matched: List[FieldMatch] = []
    for f, el, meta in discovered:
        canon = _guess_canonical_key(meta.get("text", ""))
        value = _best_data_value(canon, data) if canon else None
        filled = False
        value_used: Optional[str] = None
        try:
            if canon and value not in (None, ""):
                value = _normalize_value(canon, value)
                tag = meta.get("tag", "")
                typ = (meta.get("type", "") or "").lower()
                if tag == "select":
                    try:
                        el.select_option(label=str(value))
                    except Exception:
                        el.select_option(value=str(value))
                elif typ in ("checkbox", "radio"):
                    should = bool(value) if isinstance(value, bool) else True
                    if should:
                        el.check()
                    else:
                        el.uncheck()
                else:
                    el.fill(str(value))
                filled = True
                value_used = str(value)
        except Exception as e:
            logs.append(f"Fill error: {e}")
        matched.append(
            FieldMatch(
                frame=meta.get("frame", "main"),
                tag=meta.get("tag", ""),
                type=meta.get("type", ""),
                label_text=meta.get("text", ""),
                canonical_key=canon,
                value_used=value_used,
                selector_preview=meta.get("selector_preview", ""),
                filled=filled,
            )
        )
    return matched

def _submit_if_possible(page, submit: Optional[SubmitHints], timeout_ms: int, logs: List[str]):
    selectors = []
    if submit and submit.selector:
        selectors.append(submit.selector)
    selectors += [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "[role='button']:has-text('Submit')",
    ]
    for s in selectors:
        try:
            btn = page.locator(s)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                if submit and submit.wait_selector:
                    page.wait_for_selector(submit.wait_selector, timeout=timeout_ms)
                else:
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                return True
        except Exception as e:
            logs.append(f"Submit attempt failed for selector {s}: {e}")
    return False

# -----------------------------
# HSH Shelter Reservation — page-specific helpers
# -----------------------------

MONTHS = {m.lower(): i for i, m in enumerate([
    "", "January","February","March","April","May","June","July",
    "August","September","October","November","December"
])}

def _select_option_by_label_or_value(el, value: str):
    try:
        el.select_option(label=str(value)); return
    except Exception:
        pass
    try:
        el.select_option(value=str(value)); return
    except Exception:
        pass
    # last resort: click an <option> whose text contains the value
    try:
        opts = el.locator("option")
        for i in range(opts.count()):
            t = (opts.nth(i).inner_text() or "").strip()
            if str(value).lower() in t.lower():
                opts.nth(i).click(); return
    except Exception:
        pass

def _fill_hsh_specific(page, data: Dict[str, Any], timeout_ms: int, logs: List[str]):
    """
    Handles fields visible in the HSH screenshots:
      - Birthdate (3 selects: month/day/year)
      - Phone Owner / Alternate Phone Owner (selects)
      - Bed Preference (Male/Female radios)
      - Shelters You're Willing to Stay At (3 checkboxes)
      - "Anything we need to know?" textarea
    """

    # --- Birthdate (from `dob` = YYYY-MM-DD or birth_month/day/year) ---
    dob = data.get("dob") or data.get("date_of_birth")
    bmon = data.get("birth_month"); bday = data.get("birth_day"); byear = data.get("birth_year")
    if isinstance(dob, str) and len(dob.split("-")) == 3:
        byear, bmon, bday = dob.split("-")
        try:
            bmon = int(bmon)
        except Exception:
            pass
    try:
        if page.get_by_text(re.compile(r"Birthdate", re.I)).count() > 0:
            # Heuristic: the first 3 visible selects near the birthdate area are month/day/year
            selects = page.locator("select")
            visible = [selects.nth(i) for i in range(selects.count()) if selects.nth(i).is_visible()]
            if len(visible) >= 3 and (bmon or bday or byear):
                if bmon:
                    mv = bmon
                    if isinstance(bmon, str) and not bmon.isdigit():
                        mv = MONTHS.get(bmon.lower(), bmon)
                    _select_option_by_label_or_value(visible[0], str(mv))
                if bday:
                    _select_option_by_label_or_value(visible[1], str(bday))
                if byear:
                    _select_option_by_label_or_value(visible[2], str(byear))
    except Exception as e:
        logs.append(f"Birthdate fill error: {e}")

    # --- Phone Owner / Alternate Phone Owner ---
    for key, label_rx in [("phone_owner", r"^Phone Owner$"),
                          ("alt_phone_owner", r"Alternate Phone Owner")]:
        val = data.get(key)
        if not val:
            continue
        try:
            sel = page.get_by_label(re.compile(label_rx, re.I))
            if sel.count() > 0:
                _select_option_by_label_or_value(sel.first, str(val))
        except Exception as e:
            logs.append(f"{key} select error: {e}")

    # --- Bed Preference (Male/Female radios) ---
    bed = (data.get("bed_preference") or "").lower()
    if bed in ("male", "female"):
        try:
            page.get_by_label(f"{bed.capitalize()} Bed").check()
        except Exception as e:
            logs.append(f"Bed preference error: {e}")

    # --- Shelters You're Willing to Stay At (checkboxes) ---
    shelters = data.get("shelters") or []
    if isinstance(shelters, str):
        shelters = [s.strip() for s in shelters.split(",") if s.strip()]
    for s in shelters:
        try:
            page.get_by_label(re.compile(re.escape(s), re.I)).check()
        except Exception:
            # fallback: try any checkbox near text
            try:
                box = page.locator("input[type='checkbox']").filter(has_text=s)
                if box.count() > 0:
                    box.first.check()
            except Exception as e:
                logs.append(f"Shelter checkbox '{s}' error: {e}")

    # --- Notes ("Anything we need to know?") ---
    if data.get("notes"):
        try:
            page.get_by_label(re.compile(r"Anything we need to know\?", re.I)).fill(str(data["notes"]))
        except Exception:
            pass

# -----------------------------
# FastAPI app
# -----------------------------

app = FastAPI(title="AI Form Filler Tool", version="0.1.0")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/discover", response_model=DiscoverResponse)
def discover(req: FillRequest, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    logs: List[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=req.options.headless, slow_mo=req.options.slow_mo_ms)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(str(req.url), wait_until="domcontentloaded")
        if req.login:
            _login_if_needed(page, req.login, req.options.timeout_ms, logs)
            page.goto(str(req.url), wait_until="domcontentloaded")
        # Verint-specific start flow (reveals fields)
        if _is_verint(str(req.url)):
            _verint_start_flow(page, req.options.timeout_ms, logs)
        disc = _discover_fields(page)
        matched = _fill_discovered(page, disc, {}, req.options.timeout_ms, logs)
        ctx.close(); browser.close()
    return DiscoverResponse(url=str(req.url), discovered=matched)

@app.post("/fill", response_model=FillResponse)
def fill(req: FillRequest, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    logs: List[str] = []
    screenshot_path: Optional[str] = None

    # Prepare temp files from base64 list if provided
    temp_files: List[Tuple[str, str]] = []  # (path, hint)
    for f in req.files:
        path = f"/tmp/{int(time.time()*1000)}_{f.filename}"
        with open(path, "wb") as fp:
            fp.write(base64.b64decode(f.content_b64))
        temp_files.append((path, f.field_hint or f.filename))
        # Allow mapping like data['resume_path'] = path when hint matches
        hint = (f.field_hint or "").lower()
        if hint in ("resume", "cv", "file", "attachment") and "resume_path" not in req.data:
            req.data["resume_path"] = path

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=req.options.headless, slow_mo=req.options.slow_mo_ms)
        ctx = browser.new_context()
        page = ctx.new_page()

        page.goto(str(req.url), wait_until="domcontentloaded")
        if req.login:
            _login_if_needed(page, req.login, req.options.timeout_ms, logs)
            page.goto(str(req.url), wait_until="domcontentloaded")

        # ✅ Verint 'Start' screen (needed to reveal the form)
        if _is_verint(str(req.url)):
            _verint_start_flow(page, req.options.timeout_ms, logs)

        # Discover all inputs/controls now that the form is visible
        disc = _discover_fields(page)

        # HSH-specific handling (birthdate selects, owners, bed, shelters, notes)
        _fill_hsh_specific(page, req.data, req.options.timeout_ms, logs)

        # Generic AI-ish auto-fill for remaining fields
        matched = _fill_discovered(page, disc, req.data, req.options.timeout_ms, logs)

        # Try to submit
        submitted = _submit_if_possible(page, req.submit, req.options.timeout_ms, logs)

        # Basic error detection
        try:
            err_count = page.locator("[aria-invalid='true'], .error, .invalid, [role='alert']").count()
            if err_count:
                logs.append(f"Validation hints detected: {err_count}")
        except Exception:
            pass

        if req.options.screenshot:
            screenshot_path = f"/tmp/fill_{int(time.time())}.png"
            try:
                page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None

        ctx.close(); browser.close()

    status = "submitted" if submitted else "filled_no_submit"
    return FillResponse(
        status=status,
        url=str(req.url),
        matched=matched,
        errors=logs,
        screenshot_path=screenshot_path,
        notes="Note: Submission is best-effort and may be blocked by CAPTCHAs/2FA/WAF.",
    )

# -----------------------------
# Example client usage (Python), for the orchestrator agent
# -----------------------------
EXAMPLE_CLIENT = r"""
import requests

API_KEY = "changeme"
URL = "http://localhost:8000/fill"

payload = {
  "url": "https://example.com/form",
  "data": {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "+1 415 555 0117",
    "address_line1": "10 Downing St",
    "city": "London",
    "state": "London",
    "postal_code": "SW1A 2AA",
    "country": "United Kingdom",
    "dob": "1815-12-10"
  },
  "submit": {"selector": null, "wait_selector": "text=Thanks"},
  "options": {"headless": true, "screenshot": true}
}

r = requests.post(URL, json=payload, headers={"X-API-Key": API_KEY})
print(r.status_code)
print(r.json())
"""

# -----------------------------
# Dockerfile (save as Dockerfile if you want a container)
# -----------------------------
DOCKERFILE = r"""
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# Install python deps
RUN pip install --no-cache-dir fastapi uvicorn rapidfuzz python-dotenv "pydantic[email]" requests

# App
WORKDIR /app
COPY app.py /app/app.py

# Expose port
EXPOSE 8000

# Start
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "8000"]
"""

# If you want to write these example files to disk for convenience when running this file as a script:
if __name__ == "__main__":
    with open("app.py", "w", encoding="utf-8") as f:
        f.write(open(__file__, "r", encoding="utf-8").read())
    with open("client_example.py", "w", encoding="utf-8") as f:
        f.write(EXAMPLE_CLIENT)
    with open("Dockerfile", "w", encoding="utf-8") as f:
        f.write(DOCKERFILE)
    print("Wrote app.py, client_example.py, Dockerfile")
