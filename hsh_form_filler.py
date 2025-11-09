from playwright.sync_api import sync_playwright, TimeoutError

# === CONFIGURATION ===
URL = "https://sanfrancisco.form.us.empro.verintcloudservices.com/form/auto/hsh_shelter_reservation"

DATA = {
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
# =======================


def fill_form(page):
    d = DATA
    print("Navigating to form...")
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")

    # ---- Fill Text Inputs ----
    page.fill("#dform_widget_txt_c_forename", d["first_name"])
    page.fill("#dform_widget_txt_c_surname", d["last_name"])
    page.select_option("#dform_widget_sel_BirthMonth", d["birth_month"])
    page.select_option("#dform_widget_sel_BirthDay", d["birth_day"])
    page.fill("#dform_widget_txt_BirthYear", d["birth_year"])
    page.fill("#dform_widget_txt_individual_email_address_1", d["email"])
    page.fill("#dform_widget_tel_c_telephone", d["phone"])
    page.fill("#dform_widget_txt_Extension", d["extension"])
    page.select_option("#dform_widget_sel_PhoneOwner", d["phone_owner"])
    page.fill("#dform_widget_txt_PhoneOwnerName", d["phone_owner_name"])
    page.fill("#dform_widget_txt_AltEmail", d["alt_email"])
    page.fill("#dform_widget_txt_AlternatePhone", d["alt_phone"])
    page.select_option("#dform_widget_sel_AltPhoneOwner", d["alt_phone_owner"])
    page.fill("#dform_widget_txt_PhoneOwnerName", d["alt_phone_owner_name"])

    # ---- Bed Preference ----
    if d["bed_preference"].lower() == "male":
        page.check("#dform_widget_rad_BedPreference1")
    else:
        # fallback to second radio
        try:
            page.check("#dform_widget_rad_BedPreference2")
        except Exception:
            page.locator("input[name='rad_BedPreference']").nth(1).check()

    # ---- Shelters ----
    for shelter in d["shelters"]:
        try:
            page.check(f"input[value='{shelter}']")
        except Exception:
            print(f"Could not check shelter: {shelter}")

    # ---- Notes ----
    try:
        page.fill("textarea", d["notes"])
    except Exception:
        print("Notes textarea not found; skipping")

    page.screenshot(path="filled_form.png", full_page=True)
    print("✅ Form filled (screenshot saved).")

    # ---- Submit ----
    print("Submitting form...")
    page.click("#dform_widget_button_but_WFR1W8U0")

    # ---- Wait for confirmation message ----
    try:
        confirmation = page.wait_for_selector(
            ".dform_confirmation, .dform_message, text=Thank, text=success, text=submitted",
            timeout=10000
        )
        text = confirmation.inner_text()
        print(f"🎉 Submission successful! Message found:\n{text}")
        return True, text
    except TimeoutError:
        # maybe form shows a modal or different element
        try:
            html = page.content()
            if "Thank" in html or "success" in html.lower() or "submitted" in html.lower():
                print("✅ Submission seems successful (text found in HTML).")
                return True, "Success message detected in HTML."
        except Exception:
            pass

        print("⚠️ No visible confirmation message detected within 10s.")
        page.screenshot(path="after_submit.png", full_page=True)
        return False, "No confirmation message detected."


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context()
        page = context.new_page()

        success, message = fill_form(page)
        print(f"\nResult → success={success}, message={message}")

        # keep open for a few seconds to observe
        page.wait_for_timeout(5000)
        browser.close()


if __name__ == "__main__":
    main()
