"""Deterministic browser checks for presence and the unlinked usage dashboard.

Starts its own local static server; all external services and analytics are
intercepted with fixtures. Does not contact production or record test visitors.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from browser_check import CALENDAR, WEATHER, WAKE_LOCK_MOCK

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts"
CHROME = os.getenv("BROWSER_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
NOW = datetime(2026, 10, 25, 12, 30, tzinfo=timezone.utc)


def fixture():
    beginning = NOW.replace(minute=0) - timedelta(hours=23)
    hours = []
    for index in range(24):
        start = beginning + timedelta(hours=index)
        missing = index == 0
        partial = index in (3, 23)
        hours.append({
            "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(),
            "uniqueVisitors": None if missing else 7 + index % 4,
            "peakConcurrent": None if missing else 3 + index % 3,
            "averageConcurrent": None if missing else 1.5 + index % 3 * .3,
            "coverageSeconds": 0 if missing else 1800 if partial else 3600,
            "partial": missing or partial, "limited": index == 4,
        })
    return {
        "available": True, "generatedAt": NOW.isoformat(), "timezone": "Asia/Jerusalem",
        "retentionDays": 90, "activeWindowSeconds": 90, "activeNow": 4,
        "collectionStartedAt": (beginning + timedelta(hours=1)).isoformat(),
        "rangeStart": beginning.isoformat(), "rangeEnd": (beginning + timedelta(hours=24)).isoformat(),
        "hours": hours,
    }


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        try:
            super().do_GET()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Closing a browser context can cancel an in-flight asset/request.
            pass


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ARTIFACTS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "web")))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    config = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))
    config.update(sourceMode="live", liveApiUrl="")
    live = {"mode": "live", "sourceConnected": True, "anchorKind": "departure",
            "lastDepartureAt": None, "lastArrivalAt": None, "lastSeenAt": NOW.isoformat(),
            "cycleSeconds": 558, "cycleSource": "configured", "latestCycleSeconds": None,
            "measurementStatus": "waiting"}
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROME, headless=True)

            def prepare(width=1366, height=900):
                context = browser.new_context(viewport={"width": width, "height": height})
                context.add_init_script(f"({WAKE_LOCK_MOCK})('granted')")
                context.route("**/config.json", lambda route: route.fulfill(json=config))
                context.route("https://www.hebcal.com/**", lambda route: route.fulfill(json=CALENDAR))
                context.route("https://api.open-meteo.com/**", lambda route: route.fulfill(json=WEATHER))
                context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
                return context

            context = prepare()
            visits = []
            page_visits = {}

            def state_response(route):
                visits.append(route.request.url)
                page_visits.setdefault(route.request.frame.page, []).append(route.request.url)
                route.fulfill(json=live)

            context.route("**/api/state*", state_response)
            first = context.new_page()
            first.clock.install(time=NOW)
            first.goto(base, wait_until="networkidle")
            assert visits
            identifier = parse_qs(urlsplit(visits[-1]).query)["visitor"][0]
            assert re.fullmatch("[0-9a-f]{32}", identifier)
            assert first.locator('a[href*="stats"]').count() == 0
            assert first.locator("#active-now").count() == 0

            second = context.new_page()
            second.clock.install(time=NOW)
            second.goto(base, wait_until="networkidle")
            assert parse_qs(urlsplit(visits[-1]).query)["visitor"] == [identifier]
            first.evaluate("wakeTest.setVisibility('hidden')")
            prior = len(page_visits[first])
            first.clock.run_for(30_100)
            assert len(page_visits[first]) > prior
            assert "visitor" not in parse_qs(urlsplit(page_visits[first][-1]).query)
            second.clock.run_for(30_100)
            assert parse_qs(urlsplit(page_visits[second][-1]).query)["visitor"] == [identifier]
            with first.expect_response(re.compile(r"/api/state\?visitor=")):
                first.evaluate("wakeTest.setVisibility('visible')")
            assert parse_qs(urlsplit(page_visits[first][-1]).query)["visitor"] == [identifier]
            context.close()
            print("PASS: visible-only heartbeat, shared browser identity, hidden tab leaves other tab intact, no stats UI on main page")

            # Blocked storage omits presence without losing the elevator state.
            context = prepare()
            context.add_init_script("Storage.prototype.setItem = () => { throw new Error('blocked'); }")
            visits.clear()
            context.route("**/api/state*", state_response)
            page = context.new_page()
            page.clock.install(time=NOW)
            page.goto(base, wait_until="networkidle")
            expect(page.locator("#detector-connection")).to_have_attribute("data-status", "connected")
            assert "visitor" not in parse_qs(urlsplit(visits[-1]).query)
            context.close()
            print("PASS: unavailable browser storage preserves elevator operation")

            for width, height in ((1366, 900), (390, 844), (320, 640)):
                context = prepare(width, height)
                requests = []
                unexpected_presence = []
                mode = {"fail": False}

                def analytics_response(route):
                    requests.append(route.request.url)
                    if mode["fail"]:
                        route.fulfill(status=503, json={"available": False})
                    else:
                        route.fulfill(json=fixture())

                context.route("**/api/analytics*", analytics_response)
                context.route("**/api/state*", lambda route: (unexpected_presence.append(route.request.url), route.fulfill(json=live)))
                page = context.new_page()
                page.clock.install(time=NOW)
                page.goto(base + "/stats.html", wait_until="networkidle")
                expect(page.locator("#active-now")).to_have_text("4")
                expect(page.locator("#hours-table tr")).to_have_count(24)
                assert page.locator('meta[name="robots"]').get_attribute("content") == "noindex,nofollow"
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                table = page.locator("#hours-table").inner_text()
                assert "אין נתונים" in table and "כיסוי חלקי" in table
                assert "UTC+2" in table and "UTC+3" in table
                assert not unexpected_presence
                page.screenshot(path=str(ARTIFACTS / f"analytics-{width}.png"), full_page=True)

                page.locator("#metric-select").select_option("averageConcurrent")
                expect(page.locator("#chart-title")).to_have_text("ממוצע פעילים במקביל")
                page.locator('[data-range="week"]').click()
                page.wait_for_function("document.getElementById('active-now').textContent === '4'")
                query = parse_qs(urlsplit(requests[-1]).query)
                assert query == {"start": ["2026-10-19"], "end": ["2026-10-25"]}

                before = len(requests)
                page.evaluate("wakeTest.setVisibility('hidden')")
                page.clock.run_for(31_000)
                assert len(requests) == before
                expect(page.locator("#active-now")).to_have_text("—")
                page.evaluate("wakeTest.setVisibility('visible')")
                expect(page.locator("#active-now")).to_have_text("4")
                mode["fail"] = True
                page.locator("#refresh-button").click()
                expect(page.locator("#active-now")).to_have_text("—")
                expect(page.locator("#status-text")).to_contain_text("אין חיבור")
                expect(page.locator("#hours-table tr")).to_have_count(24)
                assert not unexpected_presence
                context.close()
                print(f"PASS: stats {width}px, date query, DST labels, missing/partial coverage, hidden refresh pause and stale-data failure")

            # Exercise the real HTTP/SQLite contract too, without production data.
            sys.path.insert(0, str(ROOT))
            from elevator.analytics import VisitorAnalytics
            from elevator.server import create_server

            with tempfile.TemporaryDirectory() as directory:
                now = [NOW]
                analytics = VisitorAnalytics(Path(directory) / "analytics.sqlite3", clock=lambda: now[0],
                                             monotonic_clock=lambda: now[0].timestamp())
                actual_server = create_server(port=0, live=True, analytics=analytics)
                actual_thread = Thread(target=actual_server.serve_forever, daemon=True)
                actual_thread.start()
                context = prepare()
                try:
                    actual_base = f"http://127.0.0.1:{actual_server.server_port}"
                    main_page = context.new_page()
                    main_page.clock.install(time=NOW)
                    main_page.goto(actual_base, wait_until="networkidle")
                    assert analytics.snapshot()["activeNow"] == 1
                    now[0] += timedelta(seconds=30)
                    analytics.tick()
                    stats_page = context.new_page()
                    stats_page.clock.install(time=NOW)
                    stats_page.goto(actual_base + "/stats.html", wait_until="networkidle")
                    expect(stats_page.locator("#active-now")).to_have_text("1")
                    assert analytics.snapshot()["hours"][-1]["uniqueVisitors"] == 1
                    main_page.evaluate("wakeTest.setVisibility('hidden')")
                    now[0] += timedelta(seconds=90)
                    analytics.tick()
                    stats_page.locator("#refresh-button").click()
                    expect(stats_page.locator("#active-now")).to_have_text("0")
                    hour = analytics.snapshot()["hours"][-1]
                    assert hour["uniqueVisitors"] == 1 and hour["peakConcurrent"] == 1
                    assert hour["averageConcurrent"] == .75
                    print("PASS: real frontend/server/SQLite integration, stats visits excluded, TTL expiration and weighted history")
                finally:
                    context.close()
                    actual_server.shutdown()
                    actual_server.server_close()
                    actual_thread.join()
                    analytics.close()

            browser.close()
        assert not errors, errors
        print("All analytics browser checks passed.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
