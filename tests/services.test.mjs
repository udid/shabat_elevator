import test from "node:test";
import assert from "node:assert/strict";
import { fetchCalendar, fetchWeather, getActivityWindow, normalizeCalendar } from "../web/services.js";

const config = { latitude: 32.078, longitude: 34.847, timezone: "Asia/Jerusalem", candleLightingMinutes: 18, havdalahMinutes: 42 };
const event = (category, date, extra = {}) => ({ category, date, title: category, ...extra });
const friday = "2026-10-09T17:55:00+03:00";
const saturday = "2026-10-10T18:56:00+03:00";
const reference = new Date("2026-10-08T10:00:00+03:00");
const weekly = () => normalizeCalendar({ items: [event("candles", friday), event("havdalah", saturday)] }, config, reference);

test("one hour of activity padding applies to both ends with correct ISO offsets", () => {
  const calendar = weekly();
  assert.equal(getActivityWindow(calendar, "2026-10-09T16:54:59+03:00").active, false);
  const start = getActivityWindow(calendar, "2026-10-09T16:55:00+03:00");
  assert.equal(start.active, true);
  assert.equal(start.window.start, "2026-10-09T13:55:00.000Z");
  assert.equal(start.window.end, "2026-10-10T16:56:00.000Z");
  assert.equal(getActivityWindow(calendar, "2026-10-10T19:56:00+03:00").active, true);
  assert.equal(getActivityWindow(calendar, "2026-10-10T19:56:01+03:00").active, false);
});

test("successive holiday candles preserve the first start until final havdalah", () => {
  const items = [
    event("candles", "2026-09-11T18:32:00+03:00"),
    event("candles", "2026-09-12T19:32:00+03:00"),
    event("holiday", "2026-09-13", { hebrew: "ראש השנה", yomtov: true }),
    event("havdalah", "2026-09-13T19:31:00+03:00"),
  ];
  const calendar = normalizeCalendar({ items }, config, new Date("2026-09-11T12:00:00Z"));
  assert.equal(calendar.spans.length, 1);
  assert.equal(calendar.spans[0].start, items[0].date);
  assert.equal(calendar.spans[0].end, items[3].date);
  assert.equal(calendar.spans[0].title, "ראש השנה");
  assert.equal(getActivityWindow(calendar, "2026-09-12T12:00:00+03:00").active, true);
});

test("touching holiday boundaries merge even when candles precede havdalah in the response", () => {
  const calendar = normalizeCalendar({ items: [
    event("candles", "2026-05-21T19:00:00+03:00"),
    event("candles", "2026-05-22T20:00:00+03:00"),
    event("havdalah", "2026-05-22T20:00:00+03:00"),
    event("havdalah", "2026-05-23T20:01:00+03:00"),
  ] }, config, new Date("2026-05-21T12:00:00Z"));
  assert.equal(calendar.spans.length, 1);
  assert.equal(calendar.spans[0].end, "2026-05-23T20:01:00+03:00");
});

test("outside a holiday returns its upcoming window without activating", () => {
  const activity = getActivityWindow(weekly(), reference);
  assert.equal(activity.active, false);
  assert.equal(activity.window, null);
  assert.equal(activity.nextWindow.eventStart, friday);
});

test("expired or unavailable calendar never enables the schedule", () => {
  const calendar = { ...weekly(), expiresAt: "2026-10-09T12:00:00Z" };
  const activity = getActivityWindow(calendar, "2026-10-09T18:00:00+03:00");
  assert.equal(activity.status, "expired");
  assert.equal(activity.active, false);
  assert.equal(activity.nextWindow, null);
  assert.equal(getActivityWindow(null, reference).status, "unavailable");
});

test("an orphan candle or timestamp without an offset cannot invent an activity span", () => {
  assert.throws(() => normalizeCalendar({ items: [event("candles", friday)] }, config, reference));
  assert.throws(() => normalizeCalendar({ items: [event("candles", "2026-10-09T17:55:00"), event("havdalah", saturday)] }, config, reference));
});

test("missing havdalah does not bridge one weekend into the next", () => {
  const calendar = normalizeCalendar({ items: [
    event("candles", "2026-10-02T18:04:00+03:00"),
    event("candles", friday), event("havdalah", saturday),
  ] }, config, reference);
  assert.equal(calendar.spans.length, 1);
  assert.equal(calendar.spans[0].start, friday);
});

test("Hebcal request uses local date, Israeli holidays, 8-day lookback and 35-day horizon", async (t) => {
  let requested;
  t.mock.method(globalThis, "fetch", async (url) => {
    requested = new URL(url);
    return { ok: true, json: async () => ({ items: [event("candles", friday), event("havdalah", saturday)] }) };
  });
  const calendar = await fetchCalendar(config, new Date("2026-10-03T22:30:00Z"));
  assert.equal(calendar.status, "live");
  const params = requested.searchParams;
  assert.equal(params.get("start"), "2026-09-26");
  assert.equal(params.get("end"), "2026-11-08");
  assert.equal(params.get("i"), "on");
  assert.equal(params.get("tzid"), "Asia/Jerusalem");
  assert.equal(params.get("b"), "18");
  assert.equal(params.get("m"), "42");
});

test("source errors are explicit and do not supply fallback calendar times", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new TypeError("Failed to fetch"); });
  const calendar = await fetchCalendar(config, reference);
  assert.equal(calendar.status, "unavailable");
  assert.deepEqual(calendar.spans, []);
  assert.equal(getActivityWindow(calendar, reference).active, false);
  const weather = await fetchWeather(config, reference);
  assert.equal(weather.status, "unavailable");
  assert.equal(weather.today, null);
});

test("havdalah defaults to astronomical nightfall when no custom minutes are set", async (t) => {
  t.mock.method(globalThis, "fetch", async (url) => {
    const params = new URL(url).searchParams;
    assert.equal(params.get("M"), "on");
    assert.equal(params.has("m"), false);
    return { ok: true, json: async () => ({ items: [event("candles", friday), event("havdalah", saturday)] }) };
  });
  const calendar = await fetchCalendar({ ...config, havdalahMinutes: null }, reference);
  assert.equal(calendar.status, "live");
});

test("weather picks Jerusalem today and upcoming Saturday, preserving zero degrees", async (t) => {
  t.mock.method(globalThis, "fetch", async (url) => {
    assert.equal(new URL(url).searchParams.get("timezone"), "Asia/Jerusalem");
    return { ok: true, json: async () => ({ current: { temperature_2m: 19.5, weather_code: 2, time: "2026-10-09T01:30" }, daily: {
      time: ["2026-10-09", "2026-10-10"], temperature_2m_min: [0, 17],
      temperature_2m_max: [21, 27], weather_code: [2, 0],
    } }) };
  });
  const weather = await fetchWeather(config, new Date("2026-10-08T22:30:00Z"));
  assert.equal(weather.status, "live");
  assert.equal(weather.today.date, "2026-10-09");
  assert.equal(weather.today.min, 0);
  assert.equal(weather.current.temperature, 19.5);
  assert.equal(weather.nextShabbat.date, "2026-10-10");
  assert.equal(weather.nextShabbat.label, "בהיר");
});
