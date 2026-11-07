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
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright

from browser_check import CALENDAR, WEATHER, WAKE_LOCK_MOCK

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts"
CHROME = os.getenv("BROWSER_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
NOW = datetime(2026, 10, 25, 12, 30, tzinfo=timezone.utc)


def shabbat_fixture(url):
    """Candle/exit events, including a Saturday-to-holiday transition."""
    query = parse_qs(urlsplit(url).query)
    first = datetime.fromisoformat(query["start"][0]).date()
    last = datetime.fromisoformat(query["end"][0]).date()
    events = []
    day = first
    while day <= last:
        if day.weekday() in (2, 4):
            events.append({"category": "candles", "date": datetime(day.year, day.month, day.day, 17, 15,
                           tzinfo=ZoneInfo("Asia/Jerusalem")).isoformat()})
        elif day.weekday() in (3, 5) or day.isoformat() == "2026-09-13":
            # Weekday holidays must not be shaded as שבת. On September 12,
            # the next holiday's candles represent Sabbath exit, not Sunday.
            category = "candles" if day.isoformat() == "2026-09-12" else "havdalah"
            events.append({"category": category, "date": datetime(day.year, day.month, day.day, 18, 11,
                           tzinfo=ZoneInfo("Asia/Jerusalem")).isoformat()})
        day += timedelta(days=1)
    return {"items": events}


def fixture(count=24):
    beginning = NOW.replace(minute=0) - timedelta(hours=count - 1)
    hours = []
    for index in range(count):
        start = beginning + timedelta(hours=index)
        missing = index in (0, count // 2)
        partial = index in (3, count - 1)
        zero = index == 1
        hours.append({
            "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(),
            "uniqueVisitors": None if missing else 0 if zero else 7 + index % 4,
            "peakConcurrent": None if missing else 0 if zero else 3 + index % 3,
            "averageConcurrent": None if missing else 0 if zero else 1.5 + index % 3 * .3,
            "coverageSeconds": 0 if missing else 1800 if partial else 3600,
            "partial": missing or partial, "limited": index == 4,
        })
    return {
        "available": True, "generatedAt": NOW.isoformat(), "timezone": "Asia/Jerusalem",
        "retentionDays": 90, "activeWindowSeconds": 90, "activeNow": 4,
        "collectionStartedAt": (beginning + timedelta(hours=1)).isoformat(),
        "rangeStart": beginning.isoformat(), "rangeEnd": (beginning + timedelta(hours=count)).isoformat(),
        "hours": hours,
        "floorUsage": {
            "collectionStartedAt": (beginning + timedelta(hours=1)).isoformat(),
            "available": True, "partial": True, "coverageSeconds": (count - 3) * 3600,
            "unreportedActiveNow": 1, "unreportedUsageSeconds": 120,
            "floors": [
                {"floor": floor, "usageSeconds": {-1: 90, 0: 540, 7: 3600, None: 300}.get(floor, 0),
                 "activeNow": {7: 2, None: 1}.get(floor, 0)}
                for floor in [*range(-1, 13), None]
            ],
        },
    }


def assert_chart_fits(page):
    # The browser clock is fixed for deterministic Israel dates; permit resize
    # rendering queued via animation frames before asserting SVG geometry.
    page.clock.run_for(100)
    page.wait_for_function("""() => {
        const container = document.getElementById('chart-scroll');
        const chart = document.getElementById('hour-chart');
        return document.documentElement.scrollWidth <= innerWidth
            && container.scrollWidth <= container.clientWidth
            && chart.getBoundingClientRect().width <= container.clientWidth + 1
            && Math.abs(chart.viewBox.baseVal.width - chart.clientWidth) <= 2;
    }""")


def assert_shabbat_bands(page, count, expected_count):
    expect(page.locator(".chart-shabbat")).to_have_count(expected_count)
    expect(page.locator(".legend-shabbat")).to_be_visible()
    data = fixture(count)
    geometry = page.evaluate("""() => {
        const grid = document.querySelector('.chart-grid');
        return {
            left: +grid.getAttribute('x1'), right: +grid.getAttribute('x2'),
            bands: Array.from(document.querySelectorAll('.chart-shabbat'), band => ({
                x: +band.getAttribute('x'), width: +band.getAttribute('width'),
                start: band.dataset.start, end: band.dataset.end,
                visibleStart: band.dataset.visibleStart, visibleEnd: band.dataset.visibleEnd,
            })),
        };
    }""")
    start = datetime.fromisoformat(data["rangeStart"]).timestamp()
    end = datetime.fromisoformat(data["rangeEnd"]).timestamp()
    scale = (geometry["right"] - geometry["left"]) / (end - start)
    for band in geometry["bands"]:
        original_start = datetime.fromisoformat(band["start"].replace("Z", "+00:00"))
        original_end = datetime.fromisoformat(band["end"].replace("Z", "+00:00"))
        local_start = original_start.astimezone(ZoneInfo("Asia/Jerusalem"))
        local_end = original_end.astimezone(ZoneInfo("Asia/Jerusalem"))
        assert (local_start.weekday(), local_start.hour, local_start.minute) == (4, 17, 15)
        assert (local_end.weekday(), local_end.hour, local_end.minute) == (5, 18, 11)
        visible_start = max(start, original_start.timestamp())
        visible_end = min(end, original_end.timestamp())
        assert datetime.fromisoformat(band["visibleStart"].replace("Z", "+00:00")).timestamp() == visible_start
        assert datetime.fromisoformat(band["visibleEnd"].replace("Z", "+00:00")).timestamp() == visible_end
        assert abs(band["x"] - (geometry["left"] + (visible_start - start) * scale)) < .1
        assert abs(band["width"] - (visible_end - visible_start) * scale) < .1
        assert band["x"] >= geometry["left"] and band["x"] + band["width"] <= geometry["right"] + .1


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

            def prepare(width=1366, height=900, touch=False):
                context = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch)
                context.add_init_script(f"({WAKE_LOCK_MOCK})('granted')")
                context.route("**/config.json", lambda route: route.fulfill(json=config))
                context.route("https://www.hebcal.com/**", lambda route: route.fulfill(json=CALENDAR))
                context.route("https://www.hebcal.com/hebcal*", lambda route: route.fulfill(
                    json=shabbat_fixture(route.request.url) if "/stats.html" in route.request.frame.page.url else CALENDAR))
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
            assert parse_qs(urlsplit(visits[-1]).query)["floor"] == ["none"]
            assert first.locator('a[href*="stats"]').count() == 0
            assert first.locator("#active-now").count() == 0
            for selection in ("7", "0", "-1", ""):
                floor = selection or "none"
                with first.expect_response(lambda response: parse_qs(urlsplit(response.url).query).get("floor") == [floor]):
                    first.locator("#floor-select").select_option(selection)
                query = parse_qs(urlsplit(page_visits[first][-1]).query)
                assert query["visitor"] == [identifier] and query["floor"] == [floor]

            second = context.new_page()
            second.clock.install(time=NOW)
            second.goto(base, wait_until="networkidle")
            assert parse_qs(urlsplit(visits[-1]).query)["visitor"] == [identifier]
            first.evaluate("wakeTest.setVisibility('hidden')")
            prior = len(page_visits[first])
            first.clock.run_for(30_100)
            assert len(page_visits[first]) > prior
            assert "visitor" not in parse_qs(urlsplit(page_visits[first][-1]).query)
            assert "floor" not in parse_qs(urlsplit(page_visits[first][-1]).query)
            second.clock.run_for(30_100)
            assert parse_qs(urlsplit(page_visits[second][-1]).query)["visitor"] == [identifier]
            with first.expect_response(re.compile(r"/api/state\?visitor=")):
                first.evaluate("wakeTest.setVisibility('visible')")
            assert parse_qs(urlsplit(page_visits[first][-1]).query)["visitor"] == [identifier]
            context.close()
            print("PASS: selected/cleared floor heartbeats, visible-only shared browser identity and no stats UI on main page")

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
            assert "floor" not in parse_qs(urlsplit(visits[-1]).query)
            context.close()
            print("PASS: unavailable browser storage preserves elevator operation")

            for width, height in ((1366, 900), (390, 844), (320, 640)):
                context = prepare(width, height, touch=width < 700)
                requests = []
                unexpected_presence = []
                mode = {"fail": False, "count": 24, "floor": "available"}

                def analytics_response(route):
                    requests.append(route.request.url)
                    if mode["fail"]:
                        route.fulfill(status=503, json={"available": False})
                    else:
                        data = fixture(mode["count"])
                        if mode["floor"] == "unsupported":
                            del data["floorUsage"]
                        elif mode["floor"] == "unavailable":
                            data["floorUsage"]["available"] = False
                            for floor in data["floorUsage"]["floors"]:
                                floor["usageSeconds"] = None
                        route.fulfill(json=data)

                context.route("**/api/analytics*", analytics_response)
                context.route("**/api/state*", lambda route: (unexpected_presence.append(route.request.url), route.fulfill(json=live)))
                page = context.new_page()
                page.clock.install(time=NOW)
                page.goto(base + "/stats.html", wait_until="networkidle")
                expect(page.locator("#active-now")).to_have_text("4")
                expect(page.locator("#hours-table tr")).to_have_count(24)
                assert page.locator('meta[name="robots"]').get_attribute("content") == "noindex,nofollow"
                assert_chart_fits(page)
                assert_shabbat_bands(page, 24, 1)
                table = page.locator("#hours-table").inner_text()
                assert "אין נתונים" in table and "כיסוי חלקי" in table
                assert "UTC+2" in table and "UTC+3" in table
                assert not unexpected_presence
                floor_seven = page.locator('.floor-row[data-floor="7"]')
                expect(floor_seven.locator(".floor-active")).to_have_text("2 כעת")
                expect(page.locator('.floor-row[data-floor="unselected"] .floor-active')).to_have_text("1 כעת")
                expect(page.locator('.floor-row[data-floor="-1"]')).to_be_visible()
                floor_history = floor_seven.locator(".floor-time").inner_text()

                chart = page.locator("#chart-scroll")
                chart.focus()
                chart.press("Home")
                expect(page.locator("#chart-detail")).to_contain_text("אין נתונים")
                expect(page.locator("#chart-detail")).to_contain_text("שבת")
                expect(page.locator(".chart-cursor")).to_have_attribute("data-index", "0")
                expect(page.locator(".chart-selected")).to_have_count(0)
                missing_detail = page.locator("#chart-detail").inner_text()
                chart.press("ArrowRight")
                expect(page.locator("#chart-detail")).not_to_contain_text("אין נתונים")
                expect(page.locator("#chart-detail")).to_contain_text("0")
                expect(page.locator(".chart-selected")).to_have_attribute("data-index", "1")
                expect(page.locator("#chart-detail")).to_contain_text("שבת")
                chart.press("ArrowRight")
                expect(page.locator("#chart-detail")).to_contain_text("שבת")
                expect(page.locator("#chart-detail")).to_contain_text("18:11")
                chart.press("ArrowLeft")
                expect(page.locator('.chart-point[data-index="12"]')).to_have_count(0)
                expect(page.locator('.chart-point[data-index="3"]')).to_have_class(re.compile(r"\bpartial\b"))
                assert page.evaluate(r"""() => {
                    const point = (index) => document.querySelector(`.chart-point[data-index="${index}"]`);
                    const gap = (+point(11).getAttribute('cx') + +point(13).getAttribute('cx')) / 2;
                    const baseline = Math.max(...Array.from(document.querySelectorAll('.chart-grid'), line => +line.getAttribute('y1')));
                    if (+point(1).getAttribute('cy') !== baseline) return false;
                    return Array.from(document.querySelectorAll('.chart-line')).every(path => {
                        let previous = null;
                        for (const command of path.getAttribute('d').matchAll(/([ML])([\d.e+-]+),([\d.e+-]+)/g)) {
                            const x = +command[2];
                            if (command[1] === 'L' && previous < gap && x > gap) return false;
                            previous = x;
                        }
                        return true;
                    });
                }""")
                chart.press("ArrowLeft")
                expect(page.locator("#chart-detail")).to_have_text(missing_detail, use_inner_text=True)
                chart.press("End")
                expect(page.locator("#chart-detail")).to_contain_text("שעה נוכחית")
                expect(page.locator("#chart-detail")).not_to_contain_text("שבת")
                if width < 700:
                    page.locator("#hour-chart").tap(position={"x": 45, "y": 80})
                    expect(page.locator("#chart-detail")).not_to_contain_text("שעה נוכחית")
                page.screenshot(path=str(ARTIFACTS / f"analytics-{width}.png"), full_page=True)

                page.locator("#metric-select").select_option("averageConcurrent")
                expect(page.locator("#chart-title")).to_have_text("ממוצע פעילים במקביל")
                mode["count"] = 168
                page.locator('[data-range="week"]').click()
                page.wait_for_function("document.getElementById('active-now').textContent === '4'")
                query = parse_qs(urlsplit(requests[-1]).query)
                assert query == {"start": ["2026-10-19"], "end": ["2026-10-25"]}
                expect(page.locator("#hours-table tr")).to_have_count(168)
                assert_chart_fits(page)
                assert_shabbat_bands(page, 168, 1)

                mode["count"] = 2160
                page.locator("#start-date").fill("2026-07-28")
                page.locator("#end-date").fill("2026-10-25")
                page.locator('#range-form button[type="submit"]').click()
                expect(page.locator("#hours-table tr")).to_have_count(2160)
                assert_chart_fits(page)
                assert_shabbat_bands(page, 2160, 13)
                page.set_viewport_size({"width": 320 if width > 700 else 1366, "height": height})
                assert_chart_fits(page)
                assert_shabbat_bands(page, 2160, 13)
                page.set_viewport_size({"width": width, "height": height})
                assert_chart_fits(page)

                before = len(requests)
                page.evaluate("wakeTest.setVisibility('hidden')")
                page.clock.run_for(31_000)
                assert len(requests) == before
                expect(page.locator("#active-now")).to_have_text("—")
                expect(floor_seven.locator(".floor-active")).to_have_text("— כעת")
                page.evaluate("wakeTest.setVisibility('visible')")
                expect(page.locator("#active-now")).to_have_text("4")
                expect(floor_seven.locator(".floor-active")).to_have_text("2 כעת")
                mode["fail"] = True
                page.locator("#refresh-button").click()
                expect(page.locator("#active-now")).to_have_text("—")
                expect(page.locator("#status-text")).to_contain_text("אין חיבור")
                expect(page.locator("#hours-table tr")).to_have_count(2160)
                expect(floor_seven.locator(".floor-active")).to_have_text("— כעת")
                expect(floor_seven.locator(".floor-time")).to_have_text(floor_history)
                assert not unexpected_presence
                mode.update(fail=False, count=24, floor="unsupported")
                page.locator("#refresh-button").click()
                expect(page.locator("#floor-list .floor-row")).to_have_count(0)
                expect(page.locator("#floor-summary")).to_contain_text("לאחר הפעלת איסוף הקומות")
                mode["floor"] = "unavailable"
                page.locator("#refresh-button").click()
                expect(page.locator("#floor-summary")).to_contain_text("אין נתוני קומות בטווח שנבחר")
                expect(page.locator("#floor-list .floor-row")).to_have_count(0)
                context.close()
                print(f"PASS: stats {width}px, 24/168/2160 hours without chart scrolling, exact/clipped Sabbath bands, resize/touch/keyboard, floor use, DST and stale data")

            # Calendar failure must not hide analytics or invent Sabbath times.
            context = prepare(390, 844)
            calendar_mode = {"fail": True}

            def calendar_response(route):
                if calendar_mode["fail"]:
                    route.fulfill(status=503, json={"error": "unavailable"})
                else:
                    route.fulfill(json=shabbat_fixture(route.request.url))

            context.route("https://www.hebcal.com/hebcal*", calendar_response)
            context.route("**/api/analytics*", lambda route: route.fulfill(json=fixture()))
            page = context.new_page()
            page.clock.install(time=NOW)
            page.goto(base + "/stats.html", wait_until="networkidle")
            expect(page.locator("#active-now")).to_have_text("4")
            expect(page.locator("#hours-table tr")).to_have_count(24)
            expect(page.locator(".chart-shabbat")).to_have_count(0)
            expect(page.locator("#shabbat-status")).to_contain_text("זמינ")
            assert_chart_fits(page)
            calendar_mode["fail"] = False
            page.locator("#refresh-button").click()
            assert_shabbat_bands(page, 24, 1)
            expect(page.locator("#active-now")).to_have_text("4")
            context.close()
            print("PASS: unavailable Sabbath times leave analytics intact, manual refresh restores highlights")

            # A delayed response for an old range must not replace the current bands.
            context = prepare()
            delayed_calendar = []
            race_mode = {"count": 24}

            def delayed_calendar_response(route):
                if not delayed_calendar:
                    delayed_calendar.append(route)
                else:
                    route.fulfill(json=shabbat_fixture(route.request.url))

            context.route("https://www.hebcal.com/hebcal*", delayed_calendar_response)
            context.route("**/api/analytics*", lambda route: route.fulfill(json=fixture(race_mode["count"])))
            page = context.new_page()
            page.clock.install(time=NOW)
            with page.expect_request("https://www.hebcal.com/hebcal*"):
                page.goto(base + "/stats.html", wait_until="domcontentloaded")
            expect(page.locator("#active-now")).to_have_text("4")
            assert len(delayed_calendar) == 1
            race_mode["count"] = 2160
            page.locator("#start-date").fill("2026-07-28")
            page.locator("#end-date").fill("2026-10-25")
            page.locator('#range-form button[type="submit"]').click()
            assert_shabbat_bands(page, 2160, 13)
            delayed_calendar[0].fulfill(json=shabbat_fixture(delayed_calendar[0].request.url))
            page.clock.run_for(100)
            assert_shabbat_bands(page, 2160, 13)
            context.close()
            print("PASS: stale calendar response cannot replace the selected range's highlights")

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
                    with main_page.expect_response(lambda response: parse_qs(urlsplit(response.url).query).get("floor") == ["7"]):
                        main_page.locator("#floor-select").select_option("7")
                    now[0] += timedelta(seconds=30)
                    analytics.tick()
                    stats_page = context.new_page()
                    stats_page.clock.install(time=NOW)
                    stats_page.goto(actual_base + "/stats.html", wait_until="networkidle")
                    expect(stats_page.locator("#active-now")).to_have_text("1")
                    expect(stats_page.locator('.floor-row[data-floor="7"] .floor-active')).to_have_text("1 כעת")
                    floor = next(item for item in analytics.snapshot()["floorUsage"]["floors"] if item["floor"] == 7)
                    assert floor["usageSeconds"] == 30 and floor["activeNow"] == 1
                    assert analytics.snapshot()["hours"][-1]["uniqueVisitors"] == 1
                    main_page.evaluate("wakeTest.setVisibility('hidden')")
                    now[0] += timedelta(seconds=90)
                    analytics.tick()
                    stats_page.locator("#refresh-button").click()
                    expect(stats_page.locator("#active-now")).to_have_text("0")
                    expect(stats_page.locator('.floor-row[data-floor="7"] .floor-active')).to_have_text("0 כעת")
                    floor = next(item for item in analytics.snapshot()["floorUsage"]["floors"] if item["floor"] == 7)
                    assert floor["usageSeconds"] == 90 and floor["activeNow"] == 0
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
