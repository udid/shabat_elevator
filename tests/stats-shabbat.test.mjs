import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeShabbatSpans, createShabbatLoader } from '../web/stats-shabbat.js';

const config = { latitude: 32.078, longitude: 34.847, timezone: 'Asia/Jerusalem', candleLightingMinutes: 18, havdalahMinutes: null, schedulePaddingMinutes: 10 };
const range = ['2026-10-23T00:00:00Z', '2026-10-25T00:00:00Z'];
const event = (category, date) => ({ category, date });
const payload = () => ({ items: [event('candles', '2026-10-23T17:15:00+03:00'), event('havdalah', '2026-10-24T18:11:00+03:00')] });
const expected = { start: '2026-10-23T14:15:00.000Z', end: '2026-10-24T15:11:00.000Z' };
const response = (value) => ({ ok: true, json: async () => value });

test('weekly bounds retain exact calendar events without elevator padding or sunset inference', () => {
  assert.deepEqual(normalizeShabbatSpans(payload(), config, ...range), [expected]);
  // Actual Hebcal response: regional candle defaults/rounding yield 16:26,
  // while its separate sunset endpoint reports 16:47 (minus18 would be wrong).
  const actual = { items: [event('candles', '2026-11-06T16:26:00+02:00'), event('havdalah', '2026-11-07T17:24:00+02:00')] };
  assert.deepEqual(normalizeShabbatSpans(actual, config, '2026-11-06T00:00:00Z', '2026-11-08T00:00:00Z'),
    [{ start: '2026-11-06T14:26:00.000Z', end: '2026-11-07T15:24:00.000Z' }]);
});

test('Saturday-only and Friday-end ranges keep actual boundaries, with exclusive overlap', () => {
  assert.deepEqual(normalizeShabbatSpans(payload(), config, '2026-10-24T13:00:00Z', '2026-10-24T14:00:00Z'), [expected]);
  assert.deepEqual(normalizeShabbatSpans(payload(), config, '2026-10-23T14:00:00Z', '2026-10-23T14:30:00Z'), [expected]);
  assert.deepEqual(normalizeShabbatSpans(payload(), config, '2026-10-23T14:00:00Z', expected.start), []);
  assert.deepEqual(normalizeShabbatSpans(payload(), config, expected.end, '2026-10-24T17:00:00Z'), []);
});

test('adjacent holidays do not extend Sabbath into Sunday or mark weekdays', () => {
  const holiday = { items: [event('candles', '2026-09-11T18:32:00+03:00'),
    event('candles', '2026-09-12T19:27:00+03:00'), event('havdalah', '2026-09-13T19:26:00+03:00')] };
  assert.deepEqual(normalizeShabbatSpans(holiday, config, '2026-09-10T00:00:00Z', '2026-09-14T00:00:00Z'),
    [{ start: '2026-09-11T15:32:00.000Z', end: '2026-09-12T16:27:00.000Z' }]);
  assert.deepEqual(normalizeShabbatSpans({}, config, '2026-09-13T00:00:00Z', '2026-09-14T00:00:00Z'), []);
});

test('Israel timezone determines date near midnight and uses each timestamp offset across DST', () => {
  const afterDst = payload();
  afterDst.items.push(event('candles', '2026-10-30T16:37:00+02:00'), event('havdalah', '2026-10-31T17:33:00+02:00'));
  assert.deepEqual(normalizeShabbatSpans(afterDst, config, '2026-10-23T21:30:00Z', '2026-11-01T00:00:00Z'),
    [expected, { start: '2026-10-30T14:37:00.000Z', end: '2026-10-31T15:33:00.000Z' }]);
});

test('one batch request covers a historical 90-day range and its boundary Saturdays', async () => {
  let requested;
  const load = createShabbatLoader({ fetchImpl: async (url) => {
    requested = new URL(url);
    const items = [];
    for (let date = new Date(`${requested.searchParams.get('start')}T12:00:00Z`);
      date.toISOString().slice(0, 10) <= requested.searchParams.get('end'); date.setUTCDate(date.getUTCDate() + 1)) {
      const key = date.toISOString().slice(0, 10);
      if (date.getUTCDay() === 5) items.push(event('candles', `${key}T17:00:00+03:00`));
      if (date.getUTCDay() === 6) items.push(event('havdalah', `${key}T18:00:00+03:00`));
    }
    return response({ items });
  } });
  const result = await load(config, '2026-05-02T00:00:00Z', '2026-07-31T00:00:00Z');
  assert.equal(result.status, 'live');
  assert.equal(result.spans.length, 13);
  assert.equal(requested.pathname, '/hebcal');
  assert.equal(requested.searchParams.get('start'), '2026-05-01');
  assert.equal(requested.searchParams.get('end'), '2026-08-01');
  assert.equal(requested.searchParams.get('latitude'), '32.078');
  assert.equal(requested.searchParams.get('longitude'), '34.847');
  assert.equal(requested.searchParams.get('tzid'), 'Asia/Jerusalem');
  assert.equal(requested.searchParams.get('b'), '18');
  assert.equal(requested.searchParams.get('M'), 'on');
  assert.equal(requested.searchParams.get('i'), 'on');
  assert.equal(requested.searchParams.get('maj'), 'on');
});

test('configured candle and havdalah minutes are passed to the same calendar API', async () => {
  let requested;
  const load = createShabbatLoader({ fetchImpl: async (url) => { requested = new URL(url); return response(payload()); } });
  assert.equal((await load({ ...config, candleLightingMinutes: 30, havdalahMinutes: 42 }, ...range)).status, 'live');
  assert.equal(requested.searchParams.get('b'), '30');
  assert.equal(requested.searchParams.get('m'), '42');
  assert.equal(requested.searchParams.has('M'), false);
});

test('successful and concurrent requests share cache while hours move within the same weekend', async () => {
  let calls = 0;
  let now = 0;
  const load = createShabbatLoader({ now: () => now, fetchImpl: async () => { calls++; return response(payload()); } });
  const [first, second] = await Promise.all([load(config, ...range), load(config, ...range)]);
  assert.equal(calls, 1);
  assert.deepEqual(first.spans, second.spans);
  assert.equal((await load(config, '2026-10-24T13:00:00Z', '2026-10-24T14:00:00Z')).status, 'live');
  assert.equal(calls, 1);
  now = 8 * 86400000;
  await load(config, ...range);
  assert.equal(calls, 2);
});

test('missing or invalid times stay unavailable and failed requests can recover', async () => {
  let calls = 0;
  const load = createShabbatLoader({ fetchImpl: async () => { calls++; return response(calls === 1 ? { items: [] } : payload()); } });
  assert.deepEqual((await load(config, ...range)).spans, []);
  assert.equal((await load(config, ...range)).status, 'live');
  assert.equal(calls, 2);
  const invalid = payload();
  invalid.items[1].date = '2026-10-24T18:11:00';
  assert.throws(() => normalizeShabbatSpans(invalid, config, ...range));
  invalid.items[1].date = '2026-10-25T18:11:00+02:00';
  assert.throws(() => normalizeShabbatSpans(invalid, config, ...range));
  assert.equal((await load({ ...config, candleLightingMinutes: '18' }, ...range)).status, 'unavailable');
});

test('network timeout fails independently and midweek-only ranges need no calendar request', async () => {
  const load = createShabbatLoader({ timeoutMs: 5, fetchImpl: (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('timeout')));
  }) });
  assert.equal((await load(config, ...range)).status, 'unavailable');
  const weekdays = createShabbatLoader({ fetchImpl: () => { throw new Error('Should not request weekdays'); } });
  assert.equal((await weekdays(config, '2026-11-02T00:00:00Z', '2026-11-04T00:00:00Z')).status, 'live');
});
