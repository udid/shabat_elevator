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

WAKE_LOCK_MOCK = """(mode) => {
    const test = window.wakeTest = {requests: 0, releases: 0, locks: [], visibility: 'visible'};
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => test.visibility});
    test.setVisibility = async (value) => {
        test.visibility = value;
        if (value === 'hidden') await Promise.all(test.locks.map(lock => lock.release()));
        document.dispatchEvent(new Event('visibilitychange'));
    };
    const createLock = () => {
        const lock = new EventTarget();
        lock.released = false;
        lock.release = async () => {
            if (lock.released) return;
            lock.released = true;
            test.releases++;
            lock.dispatchEvent(new Event('release'));
        };
        test.locks.push(lock);
        return lock;
    };
    if (mode === 'insecure') Object.defineProperty(window, 'isSecureContext', {value: false});
    Object.defineProperty(navigator, 'wakeLock', {configurable: true, value: mode === 'unsupported' ? undefined : {
        request: async (type) => {
            if (type !== 'screen') throw new Error('Unexpected wake-lock type');
            test.requests++;
            if (mode === 'denied') throw new DOMException('Denied', 'NotAllowedError');
            if (mode === 'pending') return new Promise(resolve => {test.resolve = () => resolve(createLock());});
            return createLock();
        }
    }});
}"""


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ARTIFACTS.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME, headless=True)

        def prepare(width=1440, height=1000, *, active=False, offline=False, live_state=None, calendar=None, wake_mode="granted"):
            context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
            context.add_init_script(f"({WAKE_LOCK_MOCK})({json.dumps(wake_mode)})")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc) if active else datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
            page.clock.install(time=now)
            if offline:
                page.route("https://www.hebcal.com/**", lambda route: route.abort())
                page.route("https://api.open-meteo.com/**", lambda route: route.abort())
            else:
                page.route("https://www.hebcal.com/**", lambda route: route.fulfill(json=calendar if calendar is not None else CALENDAR))
                page.route("https://api.open-meteo.com/**", lambda route: route.fulfill(json=WEATHER))
            if live_state is not None:
                config = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))
                config["sourceMode"] = "live"
                page.route("**/config.json", lambda route: route.fulfill(json=config))
                page.route("**/api/state", lambda route: route.fulfill(json=live_state))
            page.goto(BASE_URL, wait_until="networkidle")
            expect(page.locator("#floor-select option")).to_have_count(15)
            return context, page

        def toggle_demo(page):
            page.locator("#settings-button").click()
            expect(page.locator("#settings-button")).to_have_attribute("aria-expanded", "true")
            expect(page.locator("#demo-button")).to_be_in_viewport(ratio=1)
            page.locator("#demo-button").click()
            expect(page.locator("#settings-panel")).not_to_be_visible()
            expect(page.locator("#settings-button")).to_be_focused()

        context, page = prepare()
        expect(page.locator("#floor-select")).to_have_value("")
        expect(page.locator("#countdown-label")).to_have_text("מחוץ לשעות הפעילות")
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        expect(page.locator("#arrival-note")).to_contain_text("הפעילות הבאה")
        page.locator("#floor-select").select_option("4")
        toggle_demo(page)
        expect(page.locator("#countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
        expect(page.locator("#mode-tag")).to_have_text("הדגמה")
        expect(page.locator("#mode-copy")).to_have_text("הנתונים אינם מהמעלית")
        expect(page.locator("#arrival-note")).not_to_be_visible()
        expect(page.locator("#last-arrival-label")).to_contain_text("מדומה")
        expect(page.locator("#demo-button")).to_have_attribute("aria-pressed", "true")
        toggle_demo(page)
        expect(page.locator("#demo-button")).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#countdown-label")).to_have_text("מחוץ לשעות הפעילות")
        toggle_demo(page)
        page.screenshot(path=str(ARTIFACTS / "desktop-demo.png"), full_page=True)
        page.reload(wait_until="networkidle")
        expect(page.locator("#floor-select")).to_have_value("4")
        expect(page.locator("#countdown-label")).to_have_text("מחוץ לשעות הפעילות")
        page.locator("#floor-select").select_option("")
        page.reload(wait_until="networkidle")
        expect(page.locator("#floor-select")).to_have_value("")
        context.close()
        print("PASS: no default floor, explicit demo, saved selection and reset")

        for name, width, height in (("phone", 390, 844), ("small-phone", 320, 640), ("phone-short", 375, 667), ("tablet", 768, 1024), ("laptop", 1366, 768), ("desktop-short", 1280, 600), ("phone-landscape", 844, 390)):
            context, page = prepare(width, height, active=True)
            expect(page.locator("#floor-select")).to_have_value("")
            expect(page.locator("#countdown-label")).to_have_text("בחרו קומה להצגת התחזית")
            expect(page.locator("#countdown")).not_to_be_visible()
            expect(page.locator("#holiday-label")).not_to_be_visible()
            page.locator("#floor-select").select_option("-1")
            expect(page.locator("#floor-select")).to_have_value("-1")
            expect(page.locator("#countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Horizontal overflow on {name}"
            assert page.evaluate("document.documentElement.scrollHeight <= innerHeight"), f"Vertical overflow on {name}"
            for selector in ("#floor-select", "#countdown", "#current-floor", "#last-arrival", "#cycle-duration", "#mode-tag", "#mode-copy", "#even-route", "#odd-route", "#candle-time", "#havdalah-time", "#parasha-name", "#parasha-detail", "#hebrew-date", "#gregorian-date", "#wall-clock", "#weather-temperature", "#fullscreen-button", "#settings-button"):
                expect(page.locator(selector)).to_be_in_viewport(ratio=1)
            clipped = page.evaluate("""() => [...document.querySelectorAll('.elevator-card,.info-card,.route-panel')].filter(e => e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1).map(e => e.className)""")
            assert not clipped, f"Clipped card content on {name}: {clipped}"
            page.locator("#about-button").click()
            expect(page.locator("#about-dialog")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#about-dialog")).not_to_be_visible()
            expect(page.locator("#about-button")).to_be_focused()
            page.locator("#settings-button").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#settings-panel")).to_be_in_viewport(ratio=1)
            expect(page.locator("#wake-button")).to_be_in_viewport(ratio=1)
            expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "true")
            page.keyboard.press("Tab")
            expect(page.locator("#demo-button")).to_be_focused()
            page.keyboard.press("Tab")
            expect(page.locator("#wake-button")).to_be_focused()
            page.keyboard.press("Escape")
            expect(page.locator("#settings-button")).to_be_focused()
            expect(page.locator("#settings-panel")).not_to_be_visible()
            page.locator("#settings-button").click()
            page.locator("#weather-temperature").click()
            expect(page.locator("#settings-panel")).not_to_be_visible()
            expect(page.locator("#settings-button")).to_have_attribute("aria-expanded", "false")
            toggle_demo(page)
            expect(page.locator("#demo-button")).to_have_attribute("aria-pressed", "true")
            toggle_demo(page)
            expect(page.locator("#countdown")).to_be_visible()
            page.screenshot(path=str(ARTIFACTS / f"{name}.png"), full_page=True)
            context.close()
            context, page = prepare(width, height, offline=True)
            expect(page.locator("#settings-button")).to_be_visible()
            for demo in (False, True):
                if demo:
                    toggle_demo(page)
                    page.locator("#floor-select").select_option("7")
                assert page.evaluate("document.documentElement.scrollHeight <= innerHeight && document.documentElement.scrollWidth <= innerWidth"), f"Overflow on {name}, offline, demo={demo}"
                clipped = page.evaluate("""() => [...document.querySelectorAll('.elevator-card,.info-card,.route-panel')].filter(e => e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1).map(e => e.className)""")
                assert not clipped, f"Clipped offline content on {name}: {clipped}"
            context.close()
        print("PASS: all dashboard data visible without scrolling on phones, tablet and desktops; dialog keyboard behavior")

        context, page = prepare(offline=True, wake_mode="denied")
        expect(page.locator("#countdown-label")).to_have_text("זמני הפעילות אינם זמינים")
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#weather-description")).to_contain_text("אינו זמין")
        toggle_demo(page)
        page.locator("#floor-select").select_option("7")
        expect(page.locator("#countdown")).to_have_text("בקומה שלכם")
        assert page.evaluate("wakeTest.requests") == 1
        page.locator("#settings-button").click()
        expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "false")
        page.locator("#wake-button").click()
        expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#toast")).to_contain_text("לא אישר")
        assert page.evaluate("wakeTest.requests") == 2
        context.close()
        print("PASS: service failures and denied screen wake lock do not break the display")

        context, page = prepare()
        assert page.evaluate("wakeTest.requests") == 1
        expect(page.locator("#wake-label")).to_have_text("המסך נשאר דולק")
        expect(page.locator("#settings-panel")).not_to_be_visible()
        page.locator("#settings-button").click()
        page.locator("#wake-button").click()
        expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "false")
        assert page.evaluate("wakeTest.releases") == 1
        page.evaluate("async () => { await wakeTest.setVisibility('hidden'); await wakeTest.setVisibility('visible'); }")
        assert page.evaluate("wakeTest.requests") == 1
        page.locator("#wake-button").click()
        expect(page.locator("#wake-label")).to_have_text("המסך נשאר דולק")
        assert page.evaluate("wakeTest.requests") == 2
        page.evaluate("wakeTest.setVisibility('hidden')")
        expect(page.locator("#wake-label")).to_have_text("ממתין לחזרת האתר למסך")
        expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "true")
        page.evaluate("wakeTest.setVisibility('visible')")
        expect(page.locator("#wake-label")).to_have_text("המסך נשאר דולק")
        assert page.evaluate("wakeTest.requests") == 3
        page.evaluate("wakeTest.locks.at(-1).release()")
        expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#toast")).to_contain_text("שחרר")
        assert page.evaluate("wakeTest.requests") == 3
        context.close()

        for wake_mode in ("unsupported", "insecure"):
            context, page = prepare(wake_mode=wake_mode)
            page.locator("#settings-button").click()
            expect(page.locator("#wake-button")).to_be_disabled()
            expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "false")
            expect(page.locator("#wake-label")).to_contain_text("אינה נתמכת")
            assert page.evaluate("wakeTest.requests") == 0
            expect(page.locator("#toast")).not_to_be_visible()
            context.close()

        context, page = prepare(wake_mode="pending")
        expect(page.locator("#wake-label")).to_have_text("מבקש להשאיר מסך דולק…")
        page.locator("#settings-button").click()
        page.locator("#wake-button").click()
        page.evaluate("wakeTest.resolve()")
        expect(page.locator("#wake-button")).to_have_attribute("aria-pressed", "false")
        assert page.evaluate("wakeTest.releases") == 1
        context.close()
        print("PASS: wake lock defaults on, honors manual off, renews on return and handles denial, release, unsupported browsers and cancellation")

        live = {"mode": "live", "sourceConnected": True, "lastArrivalAt": "2026-10-09T14:57:00Z",
                "cycleSeconds": 570, "lastSeenAt": "2026-10-09T14:59:58Z", "measurementStatus": "tracking"}
        context, page = prepare(active=True, live_state=live)
        page.locator("#floor-select").select_option("6")
        expect(page.locator("#mode-tag")).to_have_text("חיישן מחובר")
        expect(page.locator("#mode-copy")).to_contain_text("17:59:58")
        expect(page.locator("#last-arrival")).to_have_text("17:57:00")
        expect(page.locator("#countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
        original_anchor = page.locator("#last-arrival").inner_text()
        toggle_demo(page)
        expect(page.locator("#mode-tag")).to_have_text("הדגמה")
        expect(page.locator("#last-arrival-label")).to_contain_text("מדומה")
        expect(page.locator("#countdown")).to_be_visible()
        toggle_demo(page)
        expect(page.locator("#mode-tag")).to_have_text("חיישן מחובר")
        expect(page.locator("#last-arrival")).to_have_text(original_anchor)
        live["sourceConnected"] = False
        live["measurementStatus"] = "stale"
        page.clock.run_for(5500)
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        expect(page.locator("#mode-tag")).to_have_text("חיישן מנותק")
        expect(page.locator("#last-arrival")).to_have_text(original_anchor)
        toggle_demo(page)
        expect(page.locator("#countdown")).to_be_visible()
        expect(page.locator("#mode-tag")).to_have_text("הדגמה")
        toggle_demo(page)
        expect(page.locator("#mode-tag")).to_have_text("חיישן מנותק")
        expect(page.locator("#countdown")).not_to_be_visible()
        live.update(sourceConnected=True, measurementStatus="tracking", lastSeenAt="2026-10-09T15:00:08Z",
                    lastArrivalAt="2026-10-09T14:40:00Z")
        page.clock.run_for(5500)
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#countdown-label")).to_contain_text("לסנכרון")
        live.update(lastArrivalAt="2026-10-08T14:40:00Z", lastSeenAt="2026-10-09T15:00:13Z")
        page.clock.run_for(5500)
        expect(page.locator("#last-arrival")).to_contain_text("8.10")
        expect(page.locator("#mode-copy")).to_contain_text("18:00:13")
        expect(page.locator("#countdown")).not_to_be_visible()
        live.update(lastArrivalAt=None, cycleSeconds=None, measurementStatus="waiting", lastSeenAt="2026-10-09T15:00:19Z")
        page.clock.run_for(5500)
        expect(page.locator("#countdown-label")).to_have_text("ממתינים למדידת מחזור")
        live.update(lastArrivalAt="2026-10-09T14:59:00Z", cycleSeconds=570, measurementStatus="tracking", lastSeenAt="2026-10-09T15:00:24Z")
        page.clock.run_for(5500)
        expect(page.locator("#countdown")).to_be_visible()
        expect(page.locator("#position-readout")).to_be_visible()
        expect(page.locator("#arrival-note")).not_to_be_visible()
        context.close()
        print("PASS: disconnection, old observations, waiting and recovery retain distinct arrival and heartbeat timestamps")

        holidays = {"items": CALENDAR["items"] + [
            {"category": "holiday", "date": "2026-10-09", "hebrew": "שמיני עצרת", "yomtov": True},
            {"category": "holiday", "date": "2026-10-10", "hebrew": "שמחת תורה", "yomtov": True},
        ]}
        context, page = prepare(320, 640, active=True, calendar=holidays)
        expect(page.locator("#holiday-label")).to_have_text("שמיני עצרת · שמחת תורה")
        expect(page.locator("#parasha-detail")).to_contain_text("10.10")
        expect(page.locator("#candle-time")).to_have_text("17:55")
        expect(page.locator("#havdalah-time")).to_have_text("18:51")
        expect(page.locator("#schedule-note")).to_contain_text("16:55")
        expect(page.locator("#schedule-note")).to_contain_text("19:51")
        expect(page.locator("#holiday-label")).to_be_in_viewport(ratio=1)
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
        context.close()
        print("PASS: holidays, parasha date and the separate Shabbat/elevator activity times stay visible")
        browser.close()
    assert not errors, errors
    print("PASS: no browser JavaScript errors")


if __name__ == "__main__":
    main()
