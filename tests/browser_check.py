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
    const test = window.wakeTest = {mode, requests: 0, releases: 0, locks: [], visibility: 'visible', hiddenRequests: 0};
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
            if (test.visibility !== 'visible') test.hiddenRequests++;
            if (test.mode === 'denied') throw new DOMException('Denied', 'NotAllowedError');
            if (test.mode === 'pending') return new Promise((resolve, reject) => {
                test.resolve = () => resolve(createLock());
                test.reject = () => reject(new DOMException('No longer active', 'NotAllowedError'));
            });
            const lock = createLock();
            if (test.mode === 'released') await lock.release();
            return lock;
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
            config = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))
            config["sourceMode"] = "live" if live_state is not None else "simulation"
            config["liveApiUrl"] = ""
            page.route("**/config.json", lambda route: route.fulfill(json=config))
            if live_state is not None:
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

        def expect_connection(page, status, label):
            expect(page.locator("#detector-connection")).to_have_attribute("data-status", status)
            expect(page.locator("#detector-connection-label")).to_have_text(label)
            expect(page.locator("#detector-connection")).to_be_visible()

        def expect_wake_dialog(page, *, unsupported=False):
            dialog = page.locator("#wake-dialog")
            expect(dialog).to_be_visible()
            assert dialog.evaluate("e => e instanceof HTMLDialogElement && e.open && e.matches(':modal')")
            expect(dialog.get_by_role("heading", name="שמירת המסך דולק אינה פעילה", exact=True)).to_be_visible()
            for instruction in ("חברו את הטלפון למטען", "כבו מצב חיסכון בסוללה", "השאירו את האתר פתוח בחזית"):
                expect(dialog.get_by_text(instruction, exact=False)).to_be_in_viewport(ratio=1)
            expect(page.locator("#wake-dialog-close")).to_have_text("הבנתי")
            expect(page.locator("#wake-dialog-close")).to_be_in_viewport(ratio=1)
            expect(dialog).to_be_in_viewport(ratio=1)
            assert dialog.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), "Wake guidance has horizontal overflow"
            if unsupported:
                expect(page.locator("#wake-dialog-description")).to_contain_text("אינה נתמכת")
                expect(page.locator("#wake-dialog-description")).not_to_contain_text("ינסה שוב")
                expect(page.locator("#wake-dialog-retry")).not_to_be_visible()
            else:
                expect(page.locator("#wake-dialog-retry")).to_be_visible()
            expect(page.locator("#toast")).not_to_be_visible()

        context, page = prepare()
        expect_connection(page, "disabled", "גלאי לא מוגדר")
        expect(page.locator("#floor-select")).to_have_value("")
        expect(page.locator("#last-update-field")).not_to_be_visible()
        expect(page.locator("#last-update")).to_have_text("—")
        expect(page.locator("#countdown-label")).to_have_text("מחוץ לשעות הפעילות")
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        expect(page.locator("#arrival-note")).to_contain_text("הפעילות הבאה")
        page.locator("#floor-select").select_option("4")
        toggle_demo(page)
        expect(page.locator("#countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
        expect(page.locator("#mode-tag")).to_have_text("הדגמה")
        expect(page.locator("#mode-copy")).to_have_text("הנתונים אינם מהמעלית")
        expect(page.locator("#last-update-field")).not_to_be_visible()
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
            for selector in ("#floor-select", "#countdown", "#current-floor", "#last-arrival", "#cycle-duration", "#mode-tag", "#mode-copy", "#candle-time", "#havdalah-time", "#parasha-name", "#parasha-detail", "#hebrew-date", "#gregorian-date", "#wall-clock", "#weather-temperature", "#fullscreen-button", "#settings-button", "#detector-connection"):
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
            expect(page.locator("#wake-button")).to_have_count(0)
            expect(page.locator("#wake-status")).to_be_in_viewport(ratio=1)
            expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
            for selector in ("#route-heading", "#even-route", "#odd-route", ".route-caption"):
                expect(page.locator(selector)).to_be_in_viewport(ratio=1)
            assert page.locator("#even-route .stop-node").evaluate_all("nodes => nodes.map(node => Number(node.dataset.floor))") == [0, 12, 10, 8, 6, 4, 2, -1]
            assert page.locator("#odd-route .stop-node").evaluate_all("nodes => nodes.map(node => Number(node.dataset.floor))") == [0, 11, 9, 7, 5, 3, 1, -1]
            expect(page.locator('.route-panel .stop-node.selected')).to_have_count(2)
            expect(page.locator('#even-route .stop-node.double')).to_have_attribute('data-floor', '0')
            expect(page.locator('#even-route .stop-node.double small')).to_have_text('×2')
            assert page.locator('#settings-panel').evaluate('e => e.scrollHeight <= e.clientHeight + 1 && e.scrollWidth <= e.clientWidth + 1'), f"Settings overflow on {name}"
            page.screenshot(path=str(ARTIFACTS / f"settings-route-{name}.png"), full_page=True)
            page.keyboard.press("Tab")
            expect(page.locator("#demo-button")).to_be_focused()
            page.keyboard.press("Tab")
            expect(page.locator("#floor-select")).to_be_focused()
            expect(page.locator("#settings-panel")).not_to_be_visible()
            page.locator("#settings-button").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#settings-panel")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#settings-button")).to_be_focused()
            expect(page.locator("#settings-panel")).not_to_be_visible()
            page.locator("#settings-button").click()
            page.locator(".site-header").click(position={"x": 1, "y": 1})
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

        context, page = prepare(320, 640, offline=True, wake_mode="denied")
        expect(page.locator("#countdown-label")).to_have_text("זמני הפעילות אינם זמינים")
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#weather-description")).to_contain_text("אינו זמין")
        expect_wake_dialog(page)
        page.clock.run_for(5_000)
        expect_wake_dialog(page)
        assert page.evaluate("wakeTest.requests") == 1
        page.screenshot(path=str(ARTIFACTS / "wake-warning-small-phone.png"), full_page=True)
        page.locator("#wake-dialog-close").click()
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        toggle_demo(page)
        page.locator("#floor-select").select_option("7")
        expect(page.locator("#countdown")).to_have_text("בקומה שלכם")
        assert page.evaluate("wakeTest.requests") == 1
        page.locator("#settings-button").click()
        expect(page.locator("#wake-button")).to_have_count(0)
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "retry")
        expect(page.locator("#wake-label")).to_have_text("המסך עלול להיכבות")
        expect(page.locator("#wake-status")).to_be_in_viewport(ratio=1)
        assert page.locator('#settings-panel').evaluate('e => e.scrollHeight <= e.clientHeight + 1 && e.scrollWidth <= e.clientWidth + 1'), "Denied wake-lock status overflows small-phone settings"
        expect(page.locator("#toast")).not_to_be_visible()
        assert page.evaluate("wakeTest.requests") == 1
        page.clock.run_for(30_000)
        assert page.evaluate("wakeTest.requests") == 2
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "retry")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        expect(page.locator("#toast")).not_to_be_visible()
        page.clock.run_for(30_000)
        assert page.evaluate("wakeTest.requests") == 3
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        expect(page.locator("#toast")).not_to_be_visible()
        page.evaluate("wakeTest.mode = 'granted'")
        page.clock.run_for(30_000)
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        assert page.evaluate("wakeTest.requests") == 4
        assert page.evaluate("wakeTest.hiddenRequests") == 0
        context.close()
        print("PASS: persistent wake guidance fits small phones, explains recovery, and stays dismissed during bounded retries")

        context, page = prepare(844, 390, wake_mode="denied")
        expect_wake_dialog(page)
        page.screenshot(path=str(ARTIFACTS / "wake-warning-landscape.png"), full_page=True)
        page.locator("#wake-dialog-close").click()
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        context.close()

        context, page = prepare()
        assert page.evaluate("wakeTest.requests") == 1
        expect(page.locator("#wake-button")).to_have_count(0)
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        expect(page.locator("#wake-label")).to_have_text("המסך נשאר דולק")
        expect(page.locator("#settings-panel")).not_to_be_visible()
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        page.locator("#settings-button").click()
        page.locator("#demo-button").focus()
        page.evaluate("wakeTest.locks.at(-1).release()")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "retry")
        expect_wake_dialog(page)
        expect(page.locator("#settings-panel")).not_to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        expect(page.locator("#settings-button")).to_be_focused()
        assert page.evaluate("wakeTest.releases") == 1
        page.clock.run_for(1_000)
        assert page.evaluate("wakeTest.requests") == 1
        page.clock.run_for(30_000)
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        assert page.evaluate("wakeTest.requests") == 2
        # Old sentinel events must not clear a newer active lock.
        page.evaluate("wakeTest.locks[0].dispatchEvent(new Event('release'))")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        page.evaluate("wakeTest.setVisibility('hidden')")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "hidden")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        expect(page.locator("#wake-label")).to_have_text("ממתין לחזרת האתר למסך")
        page.clock.run_for(90_000)
        assert page.evaluate("wakeTest.requests") == 2
        page.evaluate("wakeTest.setVisibility('visible')")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        assert page.evaluate("wakeTest.requests") == 3
        # A new outage after recovery opens guidance again; success closes it.
        page.evaluate("wakeTest.locks.at(-1).release()")
        expect_wake_dialog(page)
        page.clock.run_for(31_000)
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        assert page.evaluate("wakeTest.requests") == 4
        # Hiding during the retry delay cancels it; return retries immediately.
        page.evaluate("wakeTest.locks.at(-1).release()")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "retry")
        expect_wake_dialog(page)
        page.locator("#wake-dialog-close").click()
        page.evaluate("wakeTest.setVisibility('hidden')")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        page.clock.run_for(90_000)
        assert page.evaluate("wakeTest.requests") == 4
        page.evaluate("wakeTest.setVisibility('visible')")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        assert page.evaluate("wakeTest.requests") == 5
        assert page.evaluate("wakeTest.hiddenRequests") == 0
        context.close()

        for wake_mode in ("unsupported", "insecure"):
            context, page = prepare(wake_mode=wake_mode)
            expect_wake_dialog(page, unsupported=True)
            page.locator("#wake-dialog-close").click()
            expect(page.locator("#wake-dialog")).not_to_be_visible()
            page.locator("#settings-button").click()
            expect(page.locator("#wake-button")).to_have_count(0)
            expect(page.locator("#wake-status")).to_have_attribute("data-status", "unsupported")
            expect(page.locator("#wake-label")).to_contain_text("אינה נתמכת")
            page.evaluate("async () => { await wakeTest.setVisibility('hidden'); await wakeTest.setVisibility('visible'); }")
            page.clock.run_for(90_000)
            assert page.evaluate("wakeTest.requests") == 0
            expect(page.locator("#wake-dialog")).not_to_be_visible()
            expect(page.locator("#toast")).not_to_be_visible()
            context.close()

        context, page = prepare(wake_mode="pending")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "pending")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        expect(page.locator("#wake-label")).to_have_text("מבקש להשאיר מסך דולק…")
        page.evaluate("wakeTest.setVisibility('hidden')")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "hidden")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        page.evaluate("wakeTest.resolve()")
        assert page.evaluate("wakeTest.releases") == 1
        assert page.evaluate("wakeTest.locks.every(lock => lock.released)")
        page.clock.run_for(90_000)
        assert page.evaluate("wakeTest.requests") == 1
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        expect(page.locator("#toast")).not_to_be_visible()
        page.evaluate("wakeTest.mode = 'granted'")
        page.evaluate("wakeTest.setVisibility('visible')")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        assert page.evaluate("wakeTest.requests") == 2
        assert page.evaluate("wakeTest.hiddenRequests") == 0
        context.close()

        context, page = prepare(wake_mode="pending")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        page.evaluate("async () => { await wakeTest.setVisibility('hidden'); await wakeTest.setVisibility('visible'); }")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "pending")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        assert page.evaluate("wakeTest.requests") == 1
        page.evaluate("wakeTest.reject()")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "retry")
        expect_wake_dialog(page)
        page.evaluate("wakeTest.mode = 'granted'")
        page.clock.run_for(1_000)
        assert page.evaluate("wakeTest.requests") == 1
        page.clock.run_for(30_000)
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        assert page.evaluate("wakeTest.requests") == 2
        assert page.evaluate("wakeTest.hiddenRequests") == 0
        context.close()

        context, page = prepare(wake_mode="released")
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "retry")
        expect_wake_dialog(page)
        assert page.evaluate("wakeTest.requests") == 1
        assert page.evaluate("wakeTest.releases") == 1
        page.evaluate("wakeTest.mode = 'granted'")
        page.clock.run_for(1_000)
        assert page.evaluate("wakeTest.requests") == 1
        page.clock.run_for(30_000)
        expect(page.locator("#wake-status")).to_have_attribute("data-status", "active")
        expect(page.locator("#wake-dialog")).not_to_be_visible()
        assert page.evaluate("wakeTest.requests") == 2
        context.close()
        print("PASS: automatic wake lock has no toggle, retries system releases, pauses while hidden, and handles late grants, pending rejection and released sentinels")

        live = {"mode": "live", "sourceConnected": True, "lastArrivalAt": "2026-10-09T14:57:00Z",
                "cycleSeconds": 570, "lastSeenAt": "2026-10-09T14:59:58Z", "measurementStatus": "tracking"}
        context, page = prepare(320, 640, active=True, live_state=live)
        expect_connection(page, "connected", "מחובר לגלאי")
        page.locator("#floor-select").select_option("6")
        expect(page.locator("#mode-banner")).not_to_be_visible()
        expect(page.locator("#last-update")).to_have_text("17:59:58")
        expect(page.locator("#last-update-field")).not_to_be_visible()
        expect(page.locator("#cycle-duration")).to_be_in_viewport(ratio=1)
        page.locator("#settings-button").click()
        expect(page.locator("#settings-panel #last-update-field")).to_be_in_viewport(ratio=1)
        page.screenshot(path=str(ARTIFACTS / "small-phone-settings-live.png"), full_page=True)
        page.keyboard.press("Escape")
        expect(page.locator("#last-update-field")).not_to_be_visible()
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight && document.documentElement.scrollWidth <= innerWidth"), "Live measurements overflow on small phone"
        expect(page.locator("#last-arrival")).to_have_text("17:57:00")
        expect(page.locator("#countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
        original_anchor = page.locator("#last-arrival").inner_text()
        toggle_demo(page)
        expect(page.locator("#mode-tag")).to_have_text("הדגמה")
        expect(page.locator("#last-update-field")).not_to_be_visible()
        expect_connection(page, "connected", "מחובר לגלאי")
        expect(page.locator("#last-arrival-label")).to_contain_text("מדומה")
        expect(page.locator("#countdown")).to_be_visible()
        toggle_demo(page)
        expect(page.locator("#mode-banner")).not_to_be_visible()
        expect(page.locator("#last-update")).to_have_text("17:59:58")
        expect(page.locator("#last-arrival")).to_have_text(original_anchor)
        live["sourceConnected"] = False
        live["measurementStatus"] = "stale"
        page.clock.run_for(30_500)
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        expect(page.locator("#mode-banner")).not_to_be_visible()
        expect(page.locator("#last-update")).to_have_text("17:59:58")
        expect_connection(page, "disconnected", "הגלאי אינו מדווח")
        expect(page.locator("#last-arrival")).to_have_text(original_anchor)
        toggle_demo(page)
        expect(page.locator("#countdown")).to_be_visible()
        expect(page.locator("#mode-tag")).to_have_text("הדגמה")
        expect_connection(page, "disconnected", "הגלאי אינו מדווח")
        toggle_demo(page)
        expect(page.locator("#mode-banner")).not_to_be_visible()
        expect(page.locator("#countdown")).not_to_be_visible()
        live.update(sourceConnected=True, measurementStatus="tracking", lastSeenAt="2026-10-09T15:00:58Z",
                    lastArrivalAt="2026-10-09T14:40:00Z", cycleSource="measured")
        page.clock.run_for(30_500)
        expect_connection(page, "connected", "מחובר לגלאי")
        expect(page.locator("#countdown")).to_be_visible()
        expect(page.locator("#countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
        expect(page.locator("#position-readout")).to_be_visible()
        expect(page.locator("#countdown-label")).to_have_text("זמן משוער להגעה")
        expect(page.locator("#cycle-label")).to_have_text("מחזור חציוני")
        expect(page.locator("#last-arrival")).to_have_text("17:40:00")
        live.update(lastArrivalAt="2026-10-08T14:40:00Z", lastSeenAt="2026-10-09T15:01:28Z",
                    measurementStatus="uncertain")
        page.clock.run_for(30_500)
        expect(page.locator("#last-arrival")).to_have_text("—")
        expect(page.locator("#last-update")).to_have_text("18:01:28")
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        expect(page.locator("#countdown-label")).to_have_text("ממתינים למדידת מחזור")
        live.update(lastArrivalAt=None, cycleSeconds=None, measurementStatus="waiting", lastSeenAt="2026-10-09T15:01:58Z")
        page.clock.run_for(30_500)
        expect(page.locator("#countdown-label")).to_have_text("ממתינים למדידת מחזור")
        expect_connection(page, "connected", "מחובר לגלאי")
        live.update(lastArrivalAt="2026-10-09T14:59:00Z", cycleSeconds=570, measurementStatus="tracking", lastSeenAt="2026-10-09T15:02:28Z")
        page.clock.run_for(30_500)
        expect(page.locator("#countdown")).to_be_visible()
        expect(page.locator("#position-readout")).to_be_visible()
        expect(page.locator("#arrival-note")).not_to_be_visible()
        page.screenshot(path=str(ARTIFACTS / "small-phone-live.png"), full_page=True)
        context.set_offline(True)
        expect_connection(page, "unavailable", "אין חיבור לגלאי")
        expect(page.locator("#last-update")).to_have_text("18:02:28")
        expect(page.locator("#mode-banner")).not_to_be_visible()
        page.screenshot(path=str(ARTIFACTS / "small-phone-live-offline.png"), full_page=True)
        context.close()
        print("PASS: missed detections keep forecasting from the real timestamp; disconnection, uncertain state and waiting suppress forecasts")

        configured = {"mode": "live", "sourceConnected": True, "anchorKind": "departure",
                      "lastDepartureAt": None, "lastArrivalAt": None,
                      "lastSeenAt": "2026-10-09T14:59:58Z", "cycleSeconds": 558,
                      "cycleSource": "configured", "latestCycleSeconds": None,
                      "measurementStatus": "waiting"}
        context, page = prepare(320, 640, active=True, live_state=configured)
        page.locator("#floor-select").select_option("6")
        expect_connection(page, "connected", "מחובר לגלאי")
        expect(page.locator("#cycle-label")).to_have_text("מחזור לפי כיול")
        expect(page.locator("#cycle-duration")).to_have_text("09:18")
        expect(page.locator("#countdown-label")).to_have_text("ממתינים לזיהוי עזיבה בקומה 7")
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        # A candidate/heartbeat alone does not publish an anchor or enable a forecast.
        configured["lastSeenAt"] = "2026-10-09T15:00:28Z"
        page.clock.run_for(30_500)
        expect(page.locator("#countdown")).not_to_be_visible()
        configured.update(lastDepartureAt="2026-10-09T15:00:40Z", measurementStatus="tracking",
                          lastSeenAt="2026-10-09T15:00:58Z")
        page.clock.run_for(30_500)
        expect(page.locator("#countdown")).to_be_visible()
        expect(page.locator("#position-readout")).to_be_visible()
        expect(page.locator("#last-arrival")).to_have_text("18:00:40")
        expect(page.locator("#cycle-label")).to_have_text("מחזור לפי כיול")
        expect(page.locator("#cycle-duration")).to_have_text("09:18")
        page.screenshot(path=str(ARTIFACTS / "small-phone-configured-cycle.png"), full_page=True)
        # After reconnect, even a saved anchor plus the configured cycle is insufficient.
        configured.update(measurementStatus="waiting", lastSeenAt="2026-10-09T15:01:28Z")
        page.clock.run_for(30_500)
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        expect(page.locator("#cycle-duration")).to_have_text("09:18")
        context.close()
        print("PASS: configured cycle is displayed while waiting; only a confirmed departure enables forecasts")

        expiring = {"mode": "live", "sourceConnected": True, "anchorKind": "departure",
                    "lastDepartureAt": "2026-10-09T13:00:20Z", "lastArrivalAt": None,
                    "lastSeenAt": "2026-10-09T14:59:58Z", "cycleSeconds": 560,
                    "cycleSource": "configured", "measurementStatus": "tracking"}
        context, page = prepare(320, 640, active=True, live_state=expiring)
        page.locator("#floor-select").select_option("6")
        expect(page.locator("#countdown")).to_be_visible()
        # Cross the two-hour boundary before the next 30-second API poll.
        page.clock.pause_at(datetime(2026, 10, 9, 15, 0, 20, tzinfo=timezone.utc))
        page.locator("#floor-select").select_option("5")
        expect_connection(page, "connected", "מחובר לגלאי")
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        expect(page.locator("#countdown-label")).to_contain_text("לסנכרון")
        expect(page.locator("#last-arrival")).to_have_text("16:00:20")
        page.clock.run_for(1_000)
        expect(page.locator("#last-arrival")).to_have_text("—")
        expect(page.locator("#countdown-label")).to_have_text("ממתינים לזיהוי עזיבה בקומה 7")
        expect(page.locator("#cycle-duration")).to_have_text("09:20")
        detected = page.evaluate("new Date().toISOString()")
        expiring.update(lastDepartureAt=detected, lastSeenAt=detected)
        page.clock.run_for(30_500)
        expect(page.locator("#countdown")).to_be_visible()
        expect(page.locator("#position-readout")).to_be_visible()
        context.close()
        print("PASS: two-hour detection expiry removes the old timestamp between polls; a new detection restores forecasting")

        cached = {"mode": "live", "sourceConnected": True, "anchorKind": "departure",
                  "lastDepartureAt": "2026-10-09T13:00:20Z", "lastArrivalAt": None,
                  "lastSeenAt": "2026-10-09T14:59:58Z", "cycleSeconds": 558,
                  "cycleSource": "configured", "measurementStatus": "tracking"}
        context, page = prepare(320, 640, active=True, live_state=cached)
        page.locator("#floor-select").select_option("6")
        context.set_offline(True)
        expect_connection(page, "unavailable", "אין חיבור לגלאי")
        page.clock.pause_at(datetime(2026, 10, 9, 15, 0, 20, tzinfo=timezone.utc))
        page.locator("#floor-select").select_option("5")
        expect(page.locator("#last-arrival")).to_have_text("16:00:20")
        page.clock.run_for(1_000)
        expect(page.locator("#last-arrival")).to_have_text("—")
        expect(page.locator("#countdown")).not_to_be_visible()
        expect(page.locator("#position-readout")).not_to_be_visible()
        expect(page.locator("#cycle-duration")).to_have_text("09:18")
        expect(page.locator("#last-update")).to_have_text("17:59:58")
        context.close()
        print("PASS: cached offline detections older than two hours are hidden without clearing heartbeat or configured cycle")

        monitor = {"mode": "live", "sourceConnected": True, "monitorOnly": True,
                   "lastSeenAt": "2026-10-09T14:59:58Z", "measurementStatus": "waiting"}
        context, page = prepare(320, 640, active=True, live_state=monitor)
        expect(page.locator("#mode-tag")).to_have_text("מצב בדיקה")
        expect_connection(page, "connected", "מחובר לגלאי")
        expect(page.locator("#detector-connection")).to_be_in_viewport(ratio=1)
        expect(page.locator("#last-update")).to_have_text("17:59:58")
        page.route("**/api/state", lambda route: route.fulfill(status=503, body="Unavailable"))
        page.clock.run_for(30_500)
        expect_connection(page, "unavailable", "אין חיבור לגלאי")
        expect(page.locator("#last-update")).to_have_text("17:59:58")
        page.unroute("**/api/state")
        page.route("**/api/state", lambda route: route.fulfill(json=monitor))
        monitor["lastSeenAt"] = "2026-10-09T14:58:00Z"
        page.clock.run_for(30_500)
        expect_connection(page, "disconnected", "הגלאי אינו מדווח")
        monitor["lastSeenAt"] = page.evaluate("new Date().toISOString()")
        page.clock.run_for(30_500)
        expect_connection(page, "connected", "מחובר לגלאי")
        context.set_offline(True)
        expect_connection(page, "unavailable", "אין חיבור לגלאי")
        monitor["lastSeenAt"] = page.evaluate("new Date().toISOString()")
        context.set_offline(False)
        expect_connection(page, "connected", "מחובר לגלאי")
        context.close()
        print("PASS: detector connection stays independent of monitor mode and distinguishes API failure, stale heartbeat and browser offline/recovery")

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
