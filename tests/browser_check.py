"""Optional deterministic UI checks: run with a local server on port 8000."""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.getenv("TEST_URL", "http://127.0.0.1:8000")
CHROME = os.getenv("BROWSER_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
ARTIFACTS = ROOT / ".artifacts"
CALENDAR = {"items": [
    {"category": "candles", "date": "2026-10-09T17:55:00+03:00", "hebrew": "הדלקת נרות"},
    {"category": "parashat", "date": "2026-10-10", "hebrew": "פרשת בראשית"},
    {"category": "havdalah", "date": "2026-10-10T18:51:00+03:00", "hebrew": "הבדלה"},
]}
WEATHER = {
    "current": {"temperature_2m": 25, "weather_code": 1, "time": "2026-10-09T18:00"},
    "daily": {"time": [f"2026-10-{n:02}" for n in range(4, 12)],
              "temperature_2m_min": [20] * 8, "temperature_2m_max": [28] * 8, "weather_code": [1] * 8},
}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ARTIFACTS.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME, headless=True)

        def prepare(width=1440, height=1000, *, active=False, offline=False, live_state=None):
            context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc) if active else datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
            page.clock.install(time=now)
            if offline:
                page.route("https://www.hebcal.com/**", lambda route: route.abort())
                page.route("https://api.open-meteo.com/**", lambda route: route.abort())
            else:
                page.route("https://www.hebcal.com/**", lambda route: route.fulfill(json=CALENDAR))
                page.route("https://api.open-meteo.com/**", lambda route: route.fulfill(json=WEATHER))
            if live_state is not None:
                config = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))
                config["sourceMode"] = "live"
                page.route("**/config.json", lambda route: route.fulfill(json=config))
                page.route("**/api/state", lambda route: route.fulfill(json=live_state))
            page.goto(BASE_URL, wait_until="networkidle")
            expect(page.locator("#floor-select option")).to_have_count(15)
            return context, page

        context, page = prepare()
        expect(page.locator("#floor-select")).to_have_value("")
        expect(page.locator("#activity-label")).to_have_text("מחוץ לשעות הפעילות")
        expect(page.locator("#countdown")).to_have_text("--:--")
        page.locator("#floor-select").select_option("4")
        page.locator("#demo-button").click()
        expect(page.locator("#countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
        expect(page.locator("#activity-label")).to_have_text("תצוגת הדגמה")
        expect(page.locator("#mode-copy")).to_contain_text("אינה משקפת")
        page.screenshot(path=str(ARTIFACTS / "desktop-demo.png"), full_page=True)
        page.reload(wait_until="networkidle")
        expect(page.locator("#floor-select")).to_have_value("4")
        expect(page.locator("#activity-label")).to_have_text("מחוץ לשעות הפעילות")
        page.locator("#floor-select").select_option("")
        page.reload(wait_until="networkidle")
        expect(page.locator("#floor-select")).to_have_value("")
        context.close()
        print("PASS: no default floor, explicit demo, saved selection and reset")

        for name, width, height in (("phone", 390, 844), ("small-phone", 320, 640), ("tablet", 768, 1024)):
            context, page = prepare(width, height, active=True)
            expect(page.locator("#floor-select")).to_have_value("")
            expect(page.locator("#activity-label")).to_have_text("בחלון פעילות שבת")
            expect(page.locator("#countdown")).to_have_text("--:--")
            page.locator("#floor-select").select_option("-1")
            expect(page.locator("#countdown-label")).to_contain_text("-1")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Horizontal overflow on {name}"
            page.screenshot(path=str(ARTIFACTS / f"{name}.png"), full_page=True)
            context.close()
        print("PASS: scheduled simulation and no horizontal overflow at 320, 390, 768 pixels")

        context, page = prepare(offline=True)
        expect(page.locator("#activity-label")).to_have_text("זמני הפעילות לא זמינים")
        expect(page.locator("#weather-description")).to_contain_text("אינו זמין")
        page.locator("#demo-button").click()
        page.locator("#floor-select").select_option("7")
        expect(page.locator("#countdown")).to_have_text("בקומה שלכם")
        page.evaluate("Object.defineProperty(navigator, 'wakeLock', {value: {request: async () => {throw new Error('Denied')}}})")
        page.locator("#wake-button").click()
        expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#toast")).to_contain_text("לא אישר")
        context.close()
        print("PASS: service failures and denied screen wake lock do not break the display")

        live = {"mode": "live", "sourceConnected": True, "lastArrivalAt": "2026-10-09T14:57:00Z",
                "cycleSeconds": 570, "lastSeenAt": "2026-10-09T14:59:58Z", "measurementStatus": "tracking"}
        context, page = prepare(active=True, live_state=live)
        page.locator("#floor-select").select_option("6")
        expect(page.locator("#mode-tag")).to_have_text("חיישן קומה 7")
        expect(page.locator("#countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
        original_anchor = page.locator("#last-arrival").inner_text()
        live["sourceConnected"] = False
        live["measurementStatus"] = "stale"
        page.clock.run_for(5500)
        expect(page.locator("#countdown")).to_have_text("--:--")
        expect(page.locator("#last-arrival")).to_have_text(original_anchor)
        live.update(sourceConnected=True, measurementStatus="tracking", lastSeenAt="2026-10-09T15:00:08Z",
                    lastArrivalAt="2026-10-09T14:40:00Z")
        page.clock.run_for(5500)
        expect(page.locator("#countdown")).to_have_text("--:--")
        expect(page.locator("#countdown-label")).to_contain_text("לסנכרון")
        context.close()
        print("PASS: disconnected and expired real observations hide ETA without inventing arrivals")
        browser.close()
    assert not errors, errors
    print("PASS: no browser JavaScript errors")


if __name__ == "__main__":
    main()
