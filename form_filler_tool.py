# form_filler_tool.py
from __future__ import annotations

import re
from typing import Any, Dict, List
from playwright.sync_api import sync_playwright, TimeoutError

URL = "https://sanfrancisco.form.us.empro.verintcloudservices.com/form/auto/hsh_shelter_reservation"

# ---------- small helpers ----------
def _try_fill_visible(page, selector: str, value: str) -> bool:
    loc = page.locator(selector)
    if loc.count() == 0:
        return False
    try:
        if loc.first.is_visible():
            loc.first.fill(value or "")
            return True
    except Exception:
        pass
    return False

def _try_select(page, selector: str, value: str) -> bool:
    loc = page.locator(selector)
    if loc.count() == 0:
        return False
    try:
        try:
            loc.first.select_option(value=value)
            return True
        except Exception:
            loc.first.select_option(label=value)
            return True
    except Exception:
        return False

def _try_check(page, selector: str) -> bool:
    loc = page.locator(selector)
    if loc.count() == 0:
        return False
    try:
        if not loc.first.is_checked():
            loc.first.check()
        return True
    except Exception:
        return False

# ---------- confirmation parsing (fallback) ----------
CONF_SR_RE   = re.compile(r"your\s+service\s+request\s+number\s+is:\s*(\d+)", re.I)
CONF_WLID_RE = re.compile(r"your\s+waitlist\s+id\s+is:\s*([A-Z0-9]+)", re.I)

def _parse_confirmation_text(full_text: str) -> Dict[str, str]:
    """Fallback parser from page text."""
    out: Dict[str, str] = {}
    if not full_text:
        return out
    sr = CONF_SR_RE.search(full_text)
    if sr:
        out["service_request_number"] = sr.group(1)
    wl = CONF_WLID_RE.search(full_text)
    if wl:
        out["waitlist_id"] = wl.group(1)
    idx = full_text.lower().find("request details")
    if idx != -1:
        details = full_text[idx:].strip()
        out["request_details_text"] = re.sub(r"[ \t]+", " ", details)
    return out

# Exact DOM extraction using data-mapfrom
MAPFROM_SELECTORS = {
    "service_request_number": "dform_caseid",
    "waitlist_id": "txt_identifier",
    "first_name_confirm": "txt_c_forename",
    "shelters_confirm": "mchk_SheltersPreference",
}

def _extract_mapfrom(page) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, val in MAPFROM_SELECTORS.items():
        try:
            loc = page.locator(f"[data-mapfrom='{val}']")
            if loc.count() > 0:
                txt = loc.first.inner_text().strip()
                if txt:
                    out[key] = txt
        except Exception:
            pass
    return out

# ---------- main tool ----------
def fill_hsh_form(
    data: Dict[str, Any],
    *,
    headless: bool = True,
    slow_mo_ms: int = 0,
    wait_timeout_ms: int = 45_000,  # generous default
) -> Dict[str, Any]:
    """
    Fill & submit the SF HSH shelter reservation form.

    Returns:
      {
        success, message, logs,
        screenshot_before, screenshot_after,
        confirmation: {
          service_request_number?, waitlist_id?, first_name_confirm?, shelters_confirm?,
          request_details_text?
        }
      }
    """
    screenshot_before = "filled_form.png"
    screenshot_after = "after_submit.png"
    logs: List[str] = []

    def safe_goto(page, url: str, attempts: int = 2):
        # Robust navigation: try; if slow/blank, reload once.
        for i in range(attempts):
            try:
                page.goto(url, wait_until="load")
                page.wait_for_load_state("networkidle", timeout=wait_timeout_ms)
                return
            except TimeoutError:
                if i == attempts - 1:
                    raise
                logs.append("Goto timed out; reloading once…")
                try:
                    page.reload(wait_until="load")
                    page.wait_for_load_state("networkidle", timeout=wait_timeout_ms)
                    return
                except Exception:
                    pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(wait_timeout_ms)
        page.set_default_navigation_timeout(wait_timeout_ms)

        try:
            # Navigate (robust)
            safe_goto(page, URL)

            # Some Verint forms show a "Start" step; click if present
            try:
                start = page.get_by_role("button", name=re.compile(r"^start$", re.I))
                if start.count() > 0 and start.first.is_visible():
                    start.first.click()
                    page.wait_for_load_state("networkidle", timeout=wait_timeout_ms)
            except Exception:
                pass

            # Ensure first field is visible before filling
            page.wait_for_selector("#dform_widget_txt_c_forename", timeout=wait_timeout_ms)

            # ----- Participant info -----
            _try_fill_visible(page, "#dform_widget_txt_c_forename", data.get("first_name", ""))
            _try_fill_visible(page, "#dform_widget_txt_c_surname",  data.get("last_name", ""))

            _try_select(page, "#dform_widget_sel_BirthMonth", data.get("birth_month", "01"))
            _try_select(page, "#dform_widget_sel_BirthDay",   data.get("birth_day", "01"))
            _try_fill_visible(page, "#dform_widget_txt_BirthYear", data.get("birth_year", ""))

            _try_fill_visible(page, "#dform_widget_txt_individual_email_address_1", data.get("email", ""))
            _try_fill_visible(page, "#dform_widget_tel_c_telephone",                data.get("phone", ""))
            if data.get("extension"):
                _try_fill_visible(page, "#dform_widget_txt_Extension", data["extension"])

            # ----- Phone owner + conditional name -----
            _try_select(page, "#dform_widget_sel_PhoneOwner", data.get("phone_owner", "Self"))
            try:
                owner_name = data.get("phone_owner_name", "")
                owner_name_loc = page.locator("#dform_widget_txt_PhoneOwnerName")
                if owner_name and owner_name_loc.count() > 0 and owner_name_loc.first.is_visible():
                    owner_name_loc.first.fill(owner_name)
            except Exception:
                logs.append("Owner name field skipped (hidden or not present).")

            # ----- Alternate contact -----
            if data.get("alt_email"):
                _try_fill_visible(page, "#dform_widget_txt_AltEmail", data["alt_email"])
            if data.get("alt_phone"):
                _try_fill_visible(page, "#dform_widget_txt_AlternatePhone", data["alt_phone"])
            _try_select(page, "#dform_widget_sel_AltPhoneOwner", data.get("alt_phone_owner", "N/A"))
            try:
                alt_owner_name = data.get("alt_phone_owner_name", "")
                alt_owner_loc = page.locator("#dform_widget_txt_PhoneOwnerName")
                if alt_owner_name:
                    if alt_owner_loc.count() > 1 and alt_owner_loc.nth(1).is_visible():
                        alt_owner_loc.nth(1).fill(alt_owner_name)
                    elif alt_owner_loc.count() == 1 and alt_owner_loc.first.is_visible():
                        alt_owner_loc.first.fill(alt_owner_name)
            except Exception:
                pass

            # ----- Bed preference -----
            bed = (data.get("bed_preference") or "").lower()
            if bed == "male":
                if not _try_check(page, "#dform_widget_rad_BedPreference1"):
                    page.locator("input[name='rad_BedPreference']").first.check()
            elif bed == "female":
                if not _try_check(page, "#dform_widget_rad_BedPreference2"):
                    page.locator("input[name='rad_BedPreference']").nth(1).check()

            # ----- Shelters -----
            for val in data.get("shelters", []):
                if not _try_check(page, f"input[value='{val}']"):
                    logs.append(f"Could not check shelter: {val}")

            # ----- Notes -----
            try:
                if data.get("notes"):
                    page.locator("textarea").first.fill(data["notes"])
            except Exception:
                pass

            # Pre-submit screenshot
            try:
                page.screenshot(path=screenshot_before, full_page=True)
            except Exception:
                pass

            # Submit
            page.click("#dform_widget_button_but_WFR1W8U0")

            # ----- Success detection (fixed) -----
            success = False
            message = ""

            # 1) try known CSS confirmation containers
            try:
                css_targets = "#dform_success_message, .dform_confirmation, .dform_message"
                conf = page.locator(css_targets).first
                conf.wait_for(state="visible", timeout=15_000)
                message = conf.inner_text()
                success = True
            except Exception:
                # 2) fallback: look for common success words via text engine (separately)
                for txt in [r"\bthank\b", r"\bsubmitted\b", r"\bsuccess\b"]:
                    try:
                        conf = page.get_by_text(re.compile(txt, re.I)).first
                        conf.wait_for(state="visible", timeout=8_000)
                        message = conf.inner_text()
                        success = True
                        break
                    except Exception:
                        pass

            # 3) last resort: scan raw HTML for keywords
            if not success:
                try:
                    html = page.content().lower()
                    if any(w in html for w in ["thank", "submitted", "success"]):
                        success = True
                        message = "Success keywords detected in page HTML."
                except Exception:
                    pass

            # ----- Extract confirmation values -----
            confirmation: Dict[str, str] = {}
            # 1) exact DOM values via data-mapfrom
            try:
                confirmation = _extract_mapfrom(page)
            except Exception:
                confirmation = {}

            # 2) fallback from page text
            try:
                full_text = page.inner_text("body")
                parsed = _parse_confirmation_text(full_text)
                for k, v in parsed.items():
                    confirmation.setdefault(k, v)
            except Exception:
                pass

            # Final screenshot
            try:
                page.screenshot(path=screenshot_after, full_page=True)
            except Exception:
                pass

            return {
                "success": success,
                "message": message,
                "logs": logs,
                "screenshot_before": screenshot_before,
                "screenshot_after": screenshot_after,
                "confirmation": confirmation,
            }

        except Exception as e:
            try:
                page.screenshot(path=screenshot_after, full_page=True)
            except Exception:
                pass
            return {
                "success": False,
                "message": f"Unhandled error during fill/submit: {e}",
                "logs": logs,
                "screenshot_before": screenshot_before,
                "screenshot_after": screenshot_after,
                "confirmation": {},
            }
        finally:
            ctx.close()
            browser.close()

# Optional manual test
if __name__ == "__main__":
    sample = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "birth_month": "07",
        "birth_day": "15",
        "birth_year": "1985",
        "email": "ada@example.com",
        "phone": "+14155550117",
        "extension": "123",
        "phone_owner": "Self",
        "phone_owner_name": "",
        "alt_email": "alt@example.com",
        "alt_phone": "+14155550118",
        "alt_phone_owner": "N/A",
        "alt_phone_owner_name": "",
        "bed_preference": "female",
        "shelters": ["MSC_south", "next_door", "sanctuary"],
        "notes": "No stairs, please."
    }
    print(fill_hsh_form(sample, headless=False, slow_mo_ms=250))
