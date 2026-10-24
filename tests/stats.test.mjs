import test from 'node:test';
import assert from 'node:assert/strict';
import { localDate, shiftDate, validRange, formatMetric, hourParts, coverageDetails } from '../web/stats-model.js';

test('date presets use Israel local dates across midnight and year boundaries', () => {
  assert.equal(localDate('2026-10-24T22:30:00Z'), '2026-10-25');
  assert.equal(shiftDate('2026-01-01', -6), '2025-12-26');
  assert.equal(shiftDate('2028-03-01', -1), '2028-02-29');
});

test('inclusive date range rejects impossible, reversed and over-90-day dates', () => {
  assert.equal(validRange('2026-01-01', '2026-03-31'), '');
  assert.ok(validRange('2026-01-01', '2026-04-01'));
  assert.ok(validRange('2026-02-30', '2026-03-01'));
  assert.ok(validRange('2026-10-24', '2026-10-23'));
  assert.ok(validRange('', '2026-10-24'));
  assert.equal(validRange('2026-10-24', '2026-10-24'), '');
});

test('repeated autumn hours retain distinct offsets and an explicit transition range', () => {
  const first = hourParts({ start: '2026-10-24T22:00:00Z', end: '2026-10-24T23:00:00Z' });
  const second = hourParts({ start: '2026-10-24T23:00:00Z', end: '2026-10-25T00:00:00Z' });
  assert.equal(first.short, '01:00');
  assert.equal(second.short, '01:00');
  assert.equal(first.offset, 'UTC+3');
  assert.equal(second.offset, 'UTC+2');
  assert.equal(first.range, '01:00 UTC+3 → 01:00 UTC+2');
  assert.equal(second.range, '01:00–02:00 UTC+2');
});

test('spring-forward range states both offsets despite skipping a local hour', () => {
  const hour = hourParts({ start: '2026-03-26T23:00:00Z', end: '2026-03-27T00:00:00Z' });
  assert.equal(hour.range, '01:00 UTC+2 → 03:00 UTC+3');
});

test('missing measurements differ from zero visitors and partial collector coverage', () => {
  const hour = { start: '2026-10-24T09:00:00Z', end: '2026-10-24T10:00:00Z', uniqueVisitors: null, peakConcurrent: null, averageConcurrent: null, coverageSeconds: 0, partial: false };
  const now = '2026-10-24T11:00:00Z';
  assert.equal(coverageDetails(hour, now).kind, 'missing');
  assert.equal(formatMetric(null), '—');
  assert.equal(formatMetric(0), '0');
  const measuredZero = { ...hour, uniqueVisitors: 0, peakConcurrent: 0, averageConcurrent: 0, coverageSeconds: 3600 };
  assert.equal(coverageDetails(measuredZero, now).kind, 'complete');
  const gap = coverageDetails({ ...measuredZero, coverageSeconds: 1800 }, now);
  assert.equal(gap.kind, 'partial');
  assert.match(gap.detail, /50%/);
});

test('current hours, instantaneous startup and cardinality limits are explicit', () => {
  const hour = { start: '2026-10-24T09:00:00Z', end: '2026-10-24T10:00:00Z', uniqueVisitors: 1, peakConcurrent: 1, averageConcurrent: null, coverageSeconds: 0, partial: true };
  const startup = coverageDetails(hour, '2026-10-24T09:00:00Z');
  assert.equal(startup.kind, 'partial');
  assert.equal(startup.current, true);
  assert.match(startup.label, /תחילת מדידה/);
  const current = coverageDetails({ ...hour, coverageSeconds: 1800, averageConcurrent: 1 }, '2026-10-24T09:30:00Z');
  assert.equal(current.label, 'שעה נוכחית');
  assert.match(current.detail, /100%/);
  assert.match(coverageDetails({ ...hour, coverageSeconds: 3600, partial: true, limited: true }, '2026-10-24T10:00:00Z').label, /מוגבלים/);
});
