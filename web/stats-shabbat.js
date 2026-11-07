// Use the same calendar events/settings as the main page. Deriving candle
// lighting from sunset can differ from Hebcal's regional defaults/rounding.
// Weekly bounds remain Friday/Saturday even beside a multi-day holiday.
// https://www.hebcal.com/home/195/jewish-calendar-rest-api
const DAY = 86_400_000;
const TIMED_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/;
const SOURCE = { source: 'Hebcal', sourceUrl: 'https://www.hebcal.com/home/195/jewish-calendar-rest-api' };

function timestamp(value) {
  const result = typeof value === 'string' && TIMED_ISO.test(value) ? Date.parse(value) : NaN;
  if (!Number.isFinite(result)) throw new Error('Invalid timestamp');
  return result;
}

function shift(date, days) {
  return new Date(Date.parse(`${date}T12:00:00Z`) + days * DAY).toISOString().slice(0, 10);
}

function requestDetails(config, rangeStart, rangeEnd) {
  const start = timestamp(rangeStart);
  const end = timestamp(rangeEnd);
  if (end <= start || end - start > 91 * DAY) throw new Error('Invalid statistics range');
  const { latitude, longitude } = config;
  if (!Number.isFinite(latitude) || Math.abs(latitude) > 90 ||
      !Number.isFinite(longitude) || Math.abs(longitude) > 180) throw new Error('Invalid location');
  const timezone = config.timezone || 'Asia/Jerusalem';
  const formatter = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' });
  const dateKey = (instant) => {
    const parts = formatter.formatToParts(new Date(instant));
    return ['year', 'month', 'day'].map((type) => parts.find((part) => part.type === type).value).join('-');
  };
  const candleMinutes = config.candleLightingMinutes ?? 18;
  const havdalahMinutes = config.havdalahMinutes ?? null;
  if (!Number.isFinite(candleMinutes) || candleMinutes < 0 || candleMinutes > 180 ||
      (havdalahMinutes !== null && (!Number.isFinite(havdalahMinutes) || havdalahMinutes < 0 || havdalahMinutes > 180))) {
    throw new Error('Invalid Sabbath settings');
  }
  const firstDate = dateKey(start);
  const lastDate = dateKey(end - 1);
  const weekday = new Date(`${firstDate}T12:00:00Z`).getUTCDay();
  const fridays = [];
  for (let friday = shift(firstDate, -((weekday + 2) % 7)); friday <= lastDate; friday = shift(friday, 7)) {
    if (shift(friday, 1) >= firstDate) fridays.push(friday);
  }
  const url = new URL('https://www.hebcal.com/hebcal');
  if (fridays.length) {
    url.search = new URLSearchParams({ v: '1', cfg: 'json', geo: 'pos', latitude, longitude, tzid: timezone,
      start: fridays[0], end: shift(fridays.at(-1), 1), c: 'on', b: candleMinutes,
      ...(havdalahMinutes === null ? { M: 'on' } : { m: havdalahMinutes }),
      maj: 'on', s: 'on', i: 'on', lg: 'he', leyning: 'off' }).toString();
  }
  return { start, end, dateKey, fridays, candleMinutes, havdalahMinutes, url };
}

/** Return true, un-clipped Sabbath boundaries overlapping [rangeStart, rangeEnd). */
export function normalizeShabbatSpans(payload, config, rangeStart, rangeEnd) {
  const request = requestDetails(config, rangeStart, rangeEnd);
  if (!request.fridays.length) return [];
  if (!Array.isArray(payload?.items) || payload.error) throw new Error('Invalid calendar response');
  const events = payload.items.filter((event) => ['candles', 'havdalah'].includes(event?.category))
    .filter((event) => typeof event.date === 'string' && TIMED_ISO.test(event.date))
    .map((event) => ({ ...event, instant: timestamp(event.date) }));
  const timeFor = (date, category) => {
    const values = [...new Set(events.filter((event) => event.category === category && request.dateKey(event.instant) === date)
      .map((event) => event.instant))];
    if (values.length > 1) throw new Error('Ambiguous Sabbath boundary');
    return values[0];
  };
  return request.fridays.map((friday) => {
    const saturday = shift(friday, 1);
    const start = timeFor(friday, 'candles');
    // When a festival continues on Sunday, Hebcal emits Saturday candle
    // lighting at nightfall instead of havdalah. Do not extend to Sunday's end.
    const end = timeFor(saturday, 'havdalah') ?? timeFor(saturday, 'candles');
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || end - start > 2 * DAY) {
      throw new Error('Incomplete Sabbath boundaries');
    }
    return { start: new Date(start).toISOString(), end: new Date(end).toISOString() };
  }).filter((span) => Date.parse(span.start) < request.end && Date.parse(span.end) > request.start);
}

/** Share successful/in-flight weekly range requests without polling the calendar. */
export function createShabbatLoader({ fetchImpl = (...args) => fetch(...args), now = () => Date.now(), timeoutMs = 10_000 } = {}) {
  const cache = new Map();
  return async (config, rangeStart, rangeEnd) => {
    try {
      const request = requestDetails(config, rangeStart, rangeEnd);
      if (!request.fridays.length) return { status: 'live', spans: [], ...SOURCE };
      const key = `${request.url}|${request.candleMinutes}|${request.havdalahMinutes}`;
      let entry = cache.get(key);
      if (entry && entry.expiresAt <= now()) { cache.delete(key); entry = null; }
      if (!entry) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), timeoutMs);
        const promise = (async () => {
          try {
            const response = await fetchImpl(request.url, { signal: controller.signal, credentials: 'omit', headers: { Accept: 'application/json' } });
            if (!response.ok) throw new Error('Calendar unavailable');
            const payload = await response.json();
            normalizeShabbatSpans(payload, config, rangeStart, rangeEnd);
            return payload;
          } finally { clearTimeout(timeout); }
        })();
        entry = { promise, expiresAt: now() + 7 * DAY };
        cache.set(key, entry);
        while (cache.size > 8) cache.delete(cache.keys().next().value);
        promise.catch(() => { if (cache.get(key) === entry) cache.delete(key); });
      }
      const spans = normalizeShabbatSpans(await entry.promise, config, rangeStart, rangeEnd);
      return { status: 'live', spans, ...SOURCE };
    } catch {
      return { status: 'unavailable', spans: [], ...SOURCE };
    }
  };
}

export const fetchShabbatSpans = createShabbatLoader();
