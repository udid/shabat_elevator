import { createHourlyChart } from './stats-chart.js';
import { fetchShabbatSpans } from './stats-shabbat.js';
import { localDate, shiftDate, validRange, formatMetric, formatDateTime, hourParts, coverageDetails } from './stats-model.js';

const $ = (id) => document.getElementById(id);
const METRICS = {
  uniqueVisitors: 'מבקרים שונים בשעה',
  peakConcurrent: 'שיא פעילים במקביל',
  averageConcurrent: 'ממוצע פעילים במקביל',
};
const state = { config: null, data: null, preset: '24h', range: null, metric: 'uniqueVisitors', request: 0, controller: null, receivedAt: 0, shabbat: null };
const hourlyChart = createHourlyChart($('chart-scroll'), $('hour-chart'), $('chart-detail'));

function text(id, value) { $(id).textContent = String(value); }
function connection(kind, message) {
  $('connection').dataset.status = kind;
  text('status-text', message);
}
function invalidateCurrent(message, kind = 'error') {
  text('active-now', '—');
  document.querySelectorAll('.floor-active').forEach((element) => { element.textContent = '— כעת'; });
  text('floor-current-note', 'אין נתון עדכני על פעילים לפי קומה.');
  connection(kind, message);
}
function showRangeError(message) {
  text('range-error', message);
  $('range-error').hidden = !message;
}
function chartRange() {
  const hours = state.data?.hours || [];
  const start = hours[0]?.start;
  const end = hours.at(-1)?.end;
  return { start, end, key: `${start}/${end}` };
}
function showShabbatStatus() {
  const { start, end, key } = chartRange();
  const calendar = state.shabbat;
  if (!start || !end) { text('shabbat-status', ''); return; }
  if (calendar?.key !== key || calendar.status === 'loading') {
    text('shabbat-status', 'טוען את זמני השבת…');
  } else if (calendar.status !== 'live') {
    text('shabbat-status', 'זמני השבת אינם זמינים כרגע · נתוני השימוש מוצגים ללא הדגשת שבת.');
  } else {
    const overlaps = calendar.spans.some((span) => Date.parse(span.start) < Date.parse(end) && Date.parse(span.end) > Date.parse(start));
    text('shabbat-status', overlaps ? 'הרקע הכחול מסמן שבת, מזמן הכניסה ועד היציאה.' : 'אין שעות שבת בטווח המוצג.');
  }
}
function refreshShabbat(force = false) {
  const { start, end, key } = chartRange();
  if (!start || !end) { showShabbatStatus(); return; }
  const previous = state.shabbat;
  if (previous?.key === key && (previous.status === 'loading' || previous.status === 'live'
    || !force && Date.now() - previous.attemptedAt < 60_000)) {
    showShabbatStatus();
    return;
  }
  const request = { key, status: 'loading', spans: [], attemptedAt: Date.now() };
  state.shabbat = request;
  showShabbatStatus();
  // Calendar failure must not prevent the usage data or chart from loading.
  fetchShabbatSpans(state.config, start, end).catch(() => ({ status: 'unavailable', spans: [] })).then((result) => {
    if (state.shabbat !== request) return;
    Object.assign(request, result);
    if (chartRange().key !== key || !state.data) return;
    showShabbatStatus();
    renderChart();
  });
}
function renderChart() {
  const hours = state.data?.hours || [];
  const spans = state.shabbat?.key === chartRange().key ? state.shabbat.spans : [];
  hourlyChart.render(state.data, state.metric, METRICS[state.metric], spans);
  const measured = hours.filter((hour) => coverageDetails(hour, state.data.generatedAt).kind !== 'missing').length;
  text('chart-summary', measured ? `${formatMetric(measured)} מתוך ${formatMetric(hours.length)} שעות עם מדידה. ${state.metric === 'averageConcurrent' ? 'הממוצע מתייחס לזמן שנמדד בלבד.' : 'כל הטווח מוצג בגרף, ללא גלילה.'}` : 'אין עדיין נתונים לטווח שנבחר. שעות ללא מדידה אינן נספרות כאפס.');
}

function formatUsage(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
  if (seconds === 0) return '0 דק׳';
  if (seconds < 60) return 'פחות מדקה';
  if (seconds < 3600) return `${formatMetric(Math.floor(seconds / 60))} דק׳`;
  const hours = Math.round(seconds / 360) / 10;
  return hours === 1 ? 'שעה' : `${formatMetric(hours)} שעות`;
}

function renderFloors() {
  const usage = state.data?.floorUsage;
  const list = $('floor-list');
  list.replaceChildren();
  text('floor-current-note', '');
  if (!usage || !Array.isArray(usage.floors)) {
    text('floor-summary', 'נתוני הקומות יופיעו לאחר הפעלת איסוף הקומות בשרת.');
    return;
  }
  const started = Number.isFinite(Date.parse(usage.collectionStartedAt)) ? formatDateTime(usage.collectionStartedAt) : '';
  if (!usage.available) {
    text('floor-summary', `אין נתוני קומות בטווח שנבחר.${started ? ` האיסוף החל ב־${started}.` : ''}`);
    return;
  }
  const notes = [`זמן שימוש משוער, לפי דיווחי הדפדפנים.${started ? ` נאסף מ־${started}.` : ''}`];
  if (usage.partial) notes.push('הטווח כולל מדידה חלקית.');
  if (usage.unreportedUsageSeconds > 0) notes.push(`${formatUsage(usage.unreportedUsageSeconds)} ללא דיווח קומה (למשל, גרסה ישנה של האתר).`);
  if (usage.unreportedActiveNow > 0) text('floor-current-note', `${formatMetric(usage.unreportedActiveNow)} פעילים כרגע ללא דיווח קומה.`);
  text('floor-summary', notes.join(' '));
  const floors = [...usage.floors].sort((a, b) => a.floor === null ? 1 : b.floor === null ? -1 : a.floor - b.floor);
  const maximum = Math.max(1, ...floors.map((floor) => floor.usageSeconds || 0));
  for (const floor of floors) {
    const row = document.createElement('li');
    row.className = 'floor-row';
    row.dataset.floor = floor.floor === null ? 'unselected' : String(floor.floor);
    const label = document.createElement('span');
    label.className = 'floor-label';
    label.textContent = floor.floor === null ? 'ללא בחירה' : floor.floor === 0 ? '0 · כניסה' : `קומה ⁦${floor.floor}⁩`;
    const track = document.createElement('span');
    track.className = 'floor-track';
    track.setAttribute('aria-hidden', 'true');
    const fill = document.createElement('span');
    fill.className = 'floor-fill';
    fill.style.width = `${Math.max(0, floor.usageSeconds || 0) / maximum * 100}%`;
    track.append(fill);
    const time = document.createElement('span');
    time.className = 'floor-time';
    time.textContent = formatUsage(floor.usageSeconds);
    const active = document.createElement('span');
    active.className = 'floor-active';
    active.textContent = `${formatMetric(floor.activeNow)} כעת`;
    row.append(label, track, time, active);
    list.append(row);
  }
}

function renderTable() {
  const fragment = document.createDocumentFragment();
  for (const hour of [...state.data.hours].reverse()) {
    const row = document.createElement('tr');
    const parts = hourParts(hour);
    const coverage = coverageDetails(hour, state.data.generatedAt);
    const date = document.createElement('td');
    const time = document.createElement('time');
    time.dateTime = hour.start;
    time.title = parts.range;
    time.textContent = parts.date;
    const clock = document.createElement('span');
    clock.className = 'hour-clock';
    clock.textContent = parts.short;
    time.append(clock);
    const offset = document.createElement('small');
    offset.dir = 'ltr';
    offset.textContent = parts.offset;
    date.append(time, offset);
    row.append(date);
    for (const metric of Object.keys(METRICS)) {
      const cell = document.createElement('td');
      cell.textContent = coverage.kind === 'missing' ? '—' : formatMetric(hour[metric]);
      row.append(cell);
    }
    const status = document.createElement('td');
    status.className = `coverage-${coverage.kind}`;
    status.textContent = coverage.label;
    const detail = document.createElement('small');
    detail.textContent = coverage.detail;
    status.append(detail);
    row.append(status);
    fragment.append(row);
  }
  if (!state.data.hours.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 5;
    cell.className = 'empty-cell';
    cell.textContent = 'אין נתונים לטווח שנבחר.';
    row.append(cell);
    fragment.append(row);
  }
  $('hours-table').replaceChildren(fragment);
}

function renderData(force = false) {
  const data = state.data;
  text('active-now', formatMetric(data.activeNow));
  connection('live', 'הנתונים מעודכנים');
  text('last-refresh', formatDateTime(data.generatedAt, true));
  $('last-refresh').dateTime = data.generatedAt;
  text('collection-start', formatDateTime(data.collectionStartedAt));
  $('collection-start').dateTime = data.collectionStartedAt;
  text('range-label', state.preset === '24h' ? '24 השעות האחרונות · כולל השעה הנוכחית' : `${state.range.start.split('-').reverse().join('.')} — ${state.range.end.split('-').reverse().join('.')}`);
  renderFloors();
  refreshShabbat(force);
  renderChart();
  renderTable();
  if (document.visibilityState !== 'visible') invalidateCurrent('הרענון יתחדש כשהדף גלוי', 'paused');
  else if (navigator.onLine === false) invalidateCurrent('אין חיבור · ההיסטוריה מוצגת מהעדכון האחרון');
}

async function refresh(force = false) {
  if (!state.config || document.visibilityState !== 'visible') return;
  if (state.controller && !force) return;
  const today = localDate();
  for (const id of ['start-date', 'end-date']) {
    $(id).max = today;
    $(id).min = shiftDate(today, -89);
  }
  if (state.preset === 'today' || state.preset === 'week') {
    state.range = { start: state.preset === 'week' ? shiftDate(today, -6) : today, end: today };
    $('start-date').value = state.range.start;
    $('end-date').value = state.range.end;
  }
  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  const request = ++state.request;
  const timeout = setTimeout(() => controller.abort(), 10_000);
  $('refresh-button').disabled = true;
  if (!state.data) connection('loading', 'טוען נתונים…');
  try {
    const base = (state.config.liveApiUrl || '').replace(/\/$/, '');
    const query = state.range ? `?${new URLSearchParams(state.range)}` : '';
    const response = await fetch(`${base}/api/analytics${query}`, { cache: 'no-store', credentials: 'omit', signal: controller.signal });
    if (!response.ok) throw new Error('Analytics unavailable');
    const data = await response.json();
    if (!data.available || !Array.isArray(data.hours) || !Number.isInteger(data.activeNow)
      || !Number.isFinite(Date.parse(data.generatedAt)) || !Number.isFinite(Date.parse(data.collectionStartedAt))) throw new Error('Invalid analytics');
    if (request !== state.request) return;
    state.data = data;
    state.receivedAt = Date.now();
    renderData(force);
  } catch {
    if (request !== state.request) return;
    invalidateCurrent(state.data ? 'אין חיבור · ההיסטוריה מוצגת מהעדכון האחרון' : 'הנתונים אינם זמינים כרגע · נסו לרענן');
    if (!state.data) {
      text('chart-summary', 'ממתינים לחיבור לשירות הנתונים.');
      text('shabbat-status', '');
      text('floor-summary', 'לא ניתן לטעון את נתוני הקומות כרגע.');
      text('chart-detail', 'הנתונים אינם זמינים כרגע.');
      const cell = document.createElement('td');
      cell.colSpan = 5;
      cell.className = 'empty-cell';
      cell.textContent = 'לא ניתן לטעון את נתוני השימוש כרגע.';
      const row = document.createElement('tr');
      row.append(cell);
      $('hours-table').replaceChildren(row);
    }
  } finally {
    clearTimeout(timeout);
    if (request === state.request) { state.controller = null; $('refresh-button').disabled = false; }
  }
}

function selectRange(preset, range) {
  showRangeError('');
  state.preset = preset;
  state.range = range;
  state.data = null;
  document.querySelectorAll('[data-range]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.range === preset)));
  text('active-now', '—');
  text('range-label', preset === '24h' ? '24 השעות האחרונות' : `${range.start.split('-').reverse().join('.')} — ${range.end.split('-').reverse().join('.')}`);
  hourlyChart.clear();
  $('floor-list').replaceChildren();
  text('floor-summary', 'טוען נתוני קומות…');
  text('floor-current-note', '');
  text('chart-summary', 'טוען את הטווח שנבחר…');
  text('shabbat-status', 'טוען את זמני השבת…');
  $('hours-table').replaceChildren();
  refresh(true);
}

function setupControls() {
  const today = localDate();
  $('start-date').value = localDate(Date.now() - 23 * 3_600_000);
  $('end-date').value = today;
  for (const id of ['start-date', 'end-date']) {
    $(id).max = today;
    $(id).min = shiftDate(today, -89);
  }
  document.querySelectorAll('[data-range]').forEach((button) => button.addEventListener('click', () => {
    const preset = button.dataset.range;
    const end = localDate();
    const start = preset === 'week' ? shiftDate(end, -6) : preset === 'today' ? end : localDate(Date.now() - 23 * 3_600_000);
    $('start-date').value = start;
    $('end-date').value = end;
    selectRange(preset, preset === '24h' ? null : { start, end });
  }));
  $('range-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const range = { start: $('start-date').value, end: $('end-date').value };
    const error = validRange(range.start, range.end);
    if (error) { showRangeError(error); return; }
    selectRange('custom', range);
  });
  $('metric-select').addEventListener('change', () => {
    state.metric = $('metric-select').value;
    if (state.data) renderChart();
  });
  $('refresh-button').addEventListener('click', () => refresh(true));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refresh(true);
    else invalidateCurrent('הרענון יתחדש כשהדף גלוי', 'paused');
  });
  window.addEventListener('online', () => refresh(true));
  window.addEventListener('offline', () => invalidateCurrent('אין חיבור · ההיסטוריה מוצגת מהעדכון האחרון'));
  setInterval(() => {
    if (state.receivedAt && Date.now() - state.receivedAt > 90_000) invalidateCurrent('העדכון האחרון התיישן · ממתינים לנתונים חדשים');
    refresh();
  }, 30_000);
}

async function init() {
  setupControls();
  try {
    const response = await fetch('./config.json', { cache: 'no-store' });
    if (!response.ok) throw new Error('Configuration unavailable');
    state.config = await response.json();
    text('building-name', state.config.buildingName ? `· ${state.config.buildingName}` : '');
    await refresh();
  } catch { invalidateCurrent('טעינת הגדרות האתר נכשלה · רעננו את הדף'); }
}

init();
