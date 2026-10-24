export const TIMEZONE = 'Asia/Jerusalem';
const numberFormat = new Intl.NumberFormat('he-IL', { maximumFractionDigits: 2 });
const hourClock = new Intl.DateTimeFormat('en-GB', { timeZone: TIMEZONE, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const hourOffset = new Intl.DateTimeFormat('en', { timeZone: TIMEZONE, timeZoneName: 'shortOffset' });
const hourDate = new Intl.DateTimeFormat('he-IL', { timeZone: TIMEZONE, day: '2-digit', month: '2-digit', year: 'numeric' });

export function localDate(value = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: TIMEZONE, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date(value));
  return ['year', 'month', 'day'].map((type) => parts.find((part) => part.type === type).value).join('-');
}

export function shiftDate(date, days) {
  const value = new Date(`${date}T12:00:00Z`);
  value.setUTCDate(value.getUTCDate() + days);
  return value.toISOString().slice(0, 10);
}

export function validRange(start, end, maxDays = 90) {
  const validDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(`${value}T00:00:00Z`))
    && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
  if (!validDate(start) || !validDate(end)) return 'יש לבחור תאריך התחלה ותאריך סיום תקינים.';
  if (start > end) return 'תאריך הסיום צריך להיות אחרי תאריך ההתחלה או באותו יום.';
  const days = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000 + 1;
  return days > maxDays ? `אפשר להציג עד ${maxDays} ימים בכל פעם.` : '';
}

export function formatMetric(value) {
  return typeof value === 'number' && Number.isFinite(value) ? numberFormat.format(value) : '—';
}

export function formatDateTime(value, seconds = false) {
  return new Intl.DateTimeFormat('he-IL', {
    timeZone: TIMEZONE, day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}), hourCycle: 'h23',
  }).format(new Date(value));
}

export function hourParts(hour) {
  const start = new Date(hour.start);
  const end = new Date(hour.end);
  const startOffset = hourOffset.formatToParts(start).find((part) => part.type === 'timeZoneName').value.replace('GMT', 'UTC');
  const endOffset = hourOffset.formatToParts(end).find((part) => part.type === 'timeZoneName').value.replace('GMT', 'UTC');
  return {
    date: hourDate.format(start),
    time: `${hourClock.format(start)}–${hourClock.format(end)}`,
    short: hourClock.format(start),
    offset: startOffset,
    range: startOffset === endOffset ? `${hourClock.format(start)}–${hourClock.format(end)} ${startOffset}`
      : `${hourClock.format(start)} ${startOffset} → ${hourClock.format(end)} ${endOffset}`,
  };
}

export function coverageDetails(hour, generatedAt) {
  const start = Date.parse(hour.start);
  const end = Date.parse(hour.end);
  const now = Date.parse(generatedAt);
  const current = start <= now && now < end;
  const coverage = Number.isFinite(hour.coverageSeconds) ? Math.max(0, hour.coverageSeconds) : 0;
  const elapsed = Math.max(0, (Math.min(end, now) - start) / 1000);
  const missing = elapsed > coverage + 2;
  const hasMeasurement = ['uniqueVisitors', 'peakConcurrent', 'averageConcurrent'].some((metric) => typeof hour[metric] === 'number');
  if (!coverage && !hasMeasurement) return { label: 'אין נתונים', kind: 'missing', current, detail: 'לא נאספו נתונים בשעה הזו' };
  if (!coverage) return { label: current ? 'שעה נוכחית · תחילת מדידה' : 'מדידה נקודתית', kind: 'partial', current, detail: 'טרם נצבר זמן מדידה' };
  const labels = [];
  if (current) labels.push('שעה נוכחית');
  if (missing) labels.push('כיסוי חלקי');
  if (hour.limited) labels.push('נתונים מוגבלים');
  if (hour.partial && !current && !missing && !hour.limited) labels.push('שעה חלקית');
  const percentage = elapsed > 0 ? Math.min(100, Math.round(coverage / elapsed * 100)) : 100;
  return {
    label: labels.join(' · ') || 'מלא',
    kind: missing || hour.limited || hour.partial ? 'partial' : 'complete',
    current,
    detail: `${formatMetric(coverage / 60)} דקות מדידה · ${percentage}% מהזמן שחלף`,
  };
}
