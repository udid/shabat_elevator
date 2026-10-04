/** Public, read-only calendar and weather services. No elevator telemetry is inferred here. */
const DAY_MS = 86_400_000;
const CALENDAR_TTL_MS = 7 * DAY_MS;
const WEATHER_TTL_MS = 60 * 60_000;
const REQUEST_TIMEOUT_MS = 10_000;
const DEFAULT_TIMEZONE = "Asia/Jerusalem";
const TIMED_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

function dateKey(date, timezone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date);
  const value = (type) => parts.find((part) => part.type === type).value;
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function shiftDate(key, days) {
  return new Date(Date.parse(`${key}T12:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10);
}

function timestamp(value) {
  return typeof value === "string" && TIMED_ISO.test(value) ? Date.parse(value) : NaN;
}

function location(config) {
  const { latitude, longitude } = config;
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 ||
      !Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
    throw new Error("Missing or invalid location");
  }
  const timezone = config.timezone || DEFAULT_TIMEZONE;
  dateKey(new Date(), timezone); // Reject invalid IANA zone names before making a request.
  return { latitude, longitude, timezone };
}

async function requestJSON(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal, headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (payload?.error) throw new Error("Source rejected the request");
    return payload;
  } finally {
    clearTimeout(timeout);
  }
}

function failed(source, timezone, now, error) {
  return {
    status: "unavailable", source, timezone,
    fetchedAt: now.toISOString(), expiresAt: now.toISOString(),
    error: error?.name === "AbortError" ? "מקור הנתונים לא השיב בזמן" : "מקור הנתונים אינו זמין כרגע",
  };
}

/** Pair a continuous run of candles with its final havdalah, including multi-day festivals. */
export function normalizeCalendar(payload, config, now = new Date()) {
  const { timezone } = location(config);
  if (!Array.isArray(payload?.items)) throw new Error("Invalid calendar response");
  const events = payload.items.filter((item) => item && typeof item.date === "string")
    .map((item) => ({
      title: item.hebrew || item.title || "", hebrew: item.hebrew || "",
      category: item.category, date: item.date.slice(0, 10),
      at: Number.isFinite(timestamp(item.date)) ? item.date : null,
      yomtov: item.yomtov === true, memo: item.memo || "", link: item.link || null,
    }))
    .filter((item) => DATE_ONLY.test(item.date));
  const timed = events.filter((event) => event.at && ["candles", "havdalah"].includes(event.category))
    .sort((a, b) => timestamp(a.at) - timestamp(b.at) || (a.category === "havdalah" ? -1 : 1));
  const pairs = [];
  let firstCandle = null;
  let lastCandle = null;
  for (const event of timed) {
    if (event.category === "candles") {
      // A missing havdalah must not accidentally join separate weekends.
      if (!firstCandle || timestamp(event.at) - timestamp(lastCandle.at) > 36 * 60 * 60_000) {
        firstCandle = event;
      }
      lastCandle = event;
    } else if (firstCandle) {
      const length = timestamp(event.at) - timestamp(firstCandle.at);
      if (length > 0 && length <= 4 * DAY_MS &&
          timestamp(event.at) - timestamp(lastCandle.at) <= 36 * 60 * 60_000) {
        pairs.push({ start: firstCandle.at, end: event.at });
      }
      firstCandle = null;
      lastCandle = null;
    }
  }
  const spans = [];
  for (const pair of pairs) {
    const previous = spans.at(-1);
    if (previous && timestamp(pair.start) <= timestamp(previous.end)) previous.end = pair.end;
    else spans.push({ ...pair });
  }
  for (const span of spans) {
    span.events = events.filter((event) => event.date >= span.start.slice(0, 10) && event.date <= span.end.slice(0, 10));
    span.holidays = span.events.filter((event) => event.category === "holiday" && event.yomtov);
    span.parasha = span.events.find((event) => event.category === "parashat") || null;
    span.title = span.holidays.map((event) => event.title).join(" · ") || span.parasha?.title || "שבת";
    span.label = span.title;
  }
  if (!spans.length) throw new Error("Calendar has no complete candle/havdalah pair");
  const today = dateKey(now, timezone);
  return {
    status: "live", source: "Hebcal", timezone,
    sourceUrl: "https://www.hebcal.com/", fetchedAt: now.toISOString(),
    expiresAt: new Date(now.getTime() + CALENDAR_TTL_MS).toISOString(),
    events, spans,
    parasha: events.find((event) => event.category === "parashat" && event.date >= today) || null,
    holidays: events.filter((event) => event.category === "holiday" && event.date >= today),
  };
}

/** Times are fetched for Israel, using the configured fixed candle/havdalah offsets. */
export async function fetchCalendar(config, date = new Date()) {
  try {
    const { latitude, longitude, timezone } = location(config);
    const today = dateKey(date, timezone);
    const url = new URL("https://www.hebcal.com/hebcal");
    url.search = new URLSearchParams({
      v: "1", cfg: "json", start: shiftDate(today, -8), end: shiftDate(today, 35),
      c: "on", geo: "pos", latitude, longitude, tzid: timezone,
      b: config.candleLightingMinutes ?? 18,
      ...(config.havdalahMinutes == null ? { M: "on" } : { m: config.havdalahMinutes }),
      maj: "on", s: "on", i: "on", lg: "he", leyning: "off",
    }).toString();
    return normalizeCalendar(await requestJSON(url), config, date);
  } catch (error) {
    return { ...failed("Hebcal", config.timezone || DEFAULT_TIMEZONE, date, error), events: [], spans: [], parasha: null, holidays: [] };
  }
}

/** The active display window includes padding only at the two ends of a continuous holiday. */
export function getActivityWindow(calendar, now = new Date(), paddingMinutes = 60) {
  const current = new Date(now).getTime();
  const empty = { active: false, window: null, nextWindow: null, windows: [] };
  if (!calendar || calendar.status !== "live") return { ...empty, status: "unavailable" };
  if (!Number.isFinite(current) || !Number.isFinite(timestamp(calendar.expiresAt)) || current >= timestamp(calendar.expiresAt)) {
    return { ...empty, status: "expired" };
  }
  const padding = Number.isFinite(paddingMinutes) ? Math.max(0, paddingMinutes) * 60_000 : 60 * 60_000;
  const windows = [];
  for (const span of calendar.spans || []) {
    const start = timestamp(span.start), end = timestamp(span.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
    const candidate = {
      start: new Date(start - padding).toISOString(), end: new Date(end + padding).toISOString(),
      eventStart: span.start, eventEnd: span.end, title: span.title, label: span.label || span.title,
      parasha: span.parasha || null, holidays: [...(span.holidays || [])],
    };
    const previous = windows.at(-1);
    if (previous && timestamp(candidate.start) <= timestamp(previous.end)) {
      previous.end = candidate.end;
      previous.eventEnd = candidate.eventEnd;
      previous.holidays.push(...candidate.holidays);
    } else windows.push(candidate);
  }
  const window = windows.find((item) => current >= timestamp(item.start) && current <= timestamp(item.end)) || null;
  const nextWindow = windows.find((item) => timestamp(item.start) > current) || null;
  return { active: window !== null, window, nextWindow, windows, status: "live" };
}

function weatherLabel(code) {
  if (code === 0) return "בהיר";
  if (code === 1) return "בהיר בעיקר";
  if (code === 2) return "מעונן חלקית";
  if (code === 3) return "מעונן";
  if ([45, 48].includes(code)) return "ערפל";
  if ([51, 53, 55, 56, 57].includes(code)) return "טפטוף";
  if ([61, 63, 65, 66, 67, 80, 81, 82].includes(code)) return "גשם";
  if ([71, 73, 75, 77, 85, 86].includes(code)) return "שלג";
  if ([95, 96, 97, 99].includes(code)) return "סופות רעמים";
  return "תחזית";
}

/** Daily forecast dates are interpreted in the building timezone, never the device timezone. */
export async function fetchWeather(config, date = new Date()) {
  try {
    const { latitude, longitude, timezone } = location(config);
    const url = new URL("https://api.open-meteo.com/v1/forecast");
    url.search = new URLSearchParams({
      latitude, longitude, timezone, forecast_days: "8",
      current: "temperature_2m,weather_code",
      daily: "temperature_2m_max,temperature_2m_min,weather_code", temperature_unit: "celsius",
    }).toString();
    const payload = await requestJSON(url);
    const daily = payload?.daily;
    if (!Array.isArray(daily?.time)) throw new Error("Invalid weather response");
    const days = daily.time.map((day, index) => ({
      date: day, min: daily.temperature_2m_min?.[index], max: daily.temperature_2m_max?.[index],
      code: daily.weather_code?.[index], label: weatherLabel(daily.weather_code?.[index]),
    })).filter((day) => DATE_ONLY.test(day.date) && Number.isFinite(day.min) && Number.isFinite(day.max) && Number.isFinite(day.code));
    const todayKey = dateKey(date, timezone);
    const weekday = new Date(`${todayKey}T12:00:00Z`).getUTCDay();
    const shabbatKey = shiftDate(todayKey, (6 - weekday + 7) % 7);
    const today = days.find((day) => day.date === todayKey) || null;
    if (!today) throw new Error("Forecast does not cover today");
    const current = Number.isFinite(payload.current?.temperature_2m) && Number.isFinite(payload.current?.weather_code)
      ? {
        temperature: payload.current.temperature_2m, code: payload.current.weather_code,
        label: weatherLabel(payload.current.weather_code), time: payload.current.time, timezone,
      } : null;
    return {
      status: "live", source: "Open-Meteo", sourceUrl: "https://open-meteo.com/", timezone,
      fetchedAt: date.toISOString(), expiresAt: new Date(date.getTime() + WEATHER_TTL_MS).toISOString(),
      current, today, nextShabbat: days.find((day) => day.date === shabbatKey) || null, days,
    };
  } catch (error) {
    return { ...failed("Open-Meteo", config.timezone || DEFAULT_TIMEZONE, date, error), current: null, today: null, nextShabbat: null, days: [] };
  }
}
