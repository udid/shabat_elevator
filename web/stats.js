import { localDate, shiftDate, validRange, formatMetric, formatDateTime, hourParts, coverageDetails } from './stats-model.js';

const $ = (id) => document.getElementById(id);
const METRICS = {
  uniqueVisitors: 'מבקרים שונים בשעה',
  peakConcurrent: 'שיא פעילים במקביל',
  averageConcurrent: 'ממוצע פעילים במקביל',
};
const state = { config: null, data: null, preset: '24h', range: null, metric: 'uniqueVisitors', request: 0, controller: null, receivedAt: 0 };
const svgNS = 'http://www.w3.org/2000/svg';

function text(id, value) { $(id).textContent = String(value); }
function connection(kind, message) {
  $('connection').dataset.status = kind;
  text('status-text', message);
}
function invalidateCurrent(message, kind = 'error') {
  text('active-now', '—');
  connection(kind, message);
}
function showRangeError(message) {
  text('range-error', message);
  $('range-error').hidden = !message;
}
function svgNode(name, attributes = {}, value) {
  const node = document.createElementNS(svgNS, name);
  Object.entries(attributes).forEach(([key, attribute]) => node.setAttribute(key, String(attribute)));
  if (value !== undefined) node.textContent = value;
  return node;
}

function renderChart() {
  const hours = state.data?.hours || [];
  const chart = $('hour-chart');
  const width = Math.max(900, hours.length * 30 + 64);
  const left = 42;
  const right = width - 12;
  const top = 14;
  const bottom = 195;
  const height = bottom - top;
  chart.style.width = `${width}px`;
  chart.setAttribute('viewBox', `0 0 ${width} 260`);
  chart.replaceChildren(
    svgNode('title', { id: 'chart-title' }, METRICS[state.metric]),
    svgNode('desc', { id: 'chart-description' }, 'כל עמודה היא שעה אחת, מהישן לחדש. הערכים והכיסוי המדויקים מופיעים בטבלה שמתחת לגרף.'),
  );
  const defs = svgNode('defs');
  const pattern = svgNode('pattern', { id: 'missing-pattern', width: 6, height: 6, patternUnits: 'userSpaceOnUse' });
  pattern.append(svgNode('path', { d: 'M0 6 L6 0', stroke: '#626975', 'stroke-width': 1 }));
  defs.append(pattern);
  chart.append(defs);
  const values = hours.map((hour) => hour[state.metric]).filter((value) => typeof value === 'number' && Number.isFinite(value));
  const maximum = Math.max(1, Math.ceil(Math.max(0, ...values)));
  const ticks = maximum < 4 ? maximum : 4;
  for (let index = 0; index <= ticks; index += 1) {
    const value = maximum * index / ticks;
    const y = bottom - height * index / ticks;
    chart.append(svgNode('line', { x1: left, x2: right, y1: y, y2: y, class: 'chart-grid' }));
    chart.append(svgNode('text', { x: left - 8, y: y + 4, 'text-anchor': 'end', class: 'chart-label' }, formatMetric(value)));
  }
  const cell = (right - left) / Math.max(1, hours.length);
  const step = Math.max(1, Math.ceil(100 / cell));
  hours.forEach((hour, index) => {
    const parts = hourParts(hour);
    const coverage = coverageDetails(hour, state.data.generatedAt);
    const x = left + cell * index;
    const value = hour[state.metric];
    const hasValue = typeof value === 'number' && Number.isFinite(value) && coverage.kind !== 'missing';
    const tooltip = `${parts.date} ${parts.range} · ${METRICS[state.metric]}: ${hasValue ? formatMetric(value) : 'אין נתונים'} · ${coverage.label}`;
    const group = svgNode('g');
    group.append(svgNode('title', {}, tooltip));
    if (!hasValue) {
      group.append(svgNode('rect', { x: x + 3, y: bottom - 14, width: Math.max(2, cell - 6), height: 14, fill: 'url(#missing-pattern)', stroke: '#626975', 'stroke-width': .5 }));
    } else if (value === 0) {
      group.append(svgNode('line', { x1: x + 4, x2: x + cell - 4, y1: bottom - 1, y2: bottom - 1, class: 'chart-zero' }));
    } else {
      const barHeight = Math.max(1, value / maximum * height);
      group.append(svgNode('rect', { x: x + 3, y: bottom - barHeight, width: Math.max(2, cell - 6), height: barHeight, rx: 2, class: `chart-bar${coverage.kind === 'partial' ? ' partial' : ''}` }));
    }
    chart.append(group);
    if (index % step === 0 || index === hours.length - 1 && hours.length % step > 1) {
      chart.append(svgNode('text', { x: x + cell / 2, y: bottom + 22, 'text-anchor': 'middle', class: 'chart-label' }, parts.short));
      chart.append(svgNode('text', { x: x + cell / 2, y: bottom + 39, 'text-anchor': 'middle', class: 'chart-label' }, parts.date.replace(/\.\d{4}$/, '')));
      chart.append(svgNode('text', { x: x + cell / 2, y: bottom + 54, 'text-anchor': 'middle', class: 'chart-label' }, parts.offset));
    }
  });
  const measured = hours.filter((hour) => coverageDetails(hour, state.data.generatedAt).kind !== 'missing').length;
  text('chart-summary', measured ? `${formatMetric(measured)} מתוך ${formatMetric(hours.length)} שעות עם מדידה. ${state.metric === 'averageConcurrent' ? 'הממוצע מתייחס לזמן שנמדד בלבד.' : 'שעות ללא מדידה מסומנות בקווים.'}` : 'אין עדיין נתונים לטווח שנבחר. שעות ללא מדידה אינן נספרות כאפס.');
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

function renderData() {
  const data = state.data;
  text('active-now', formatMetric(data.activeNow));
  connection('live', 'הנתונים מעודכנים');
  text('last-refresh', formatDateTime(data.generatedAt, true));
  $('last-refresh').dateTime = data.generatedAt;
  text('collection-start', formatDateTime(data.collectionStartedAt));
  $('collection-start').dateTime = data.collectionStartedAt;
  text('range-label', state.preset === '24h' ? '24 השעות האחרונות · כולל השעה הנוכחית' : `${state.range.start.split('-').reverse().join('.')} — ${state.range.end.split('-').reverse().join('.')}`);
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
    const firstRender = !state.data;
    state.data = data;
    state.receivedAt = Date.now();
    renderData();
    if (firstRender) $('chart-scroll').scrollLeft = $('chart-scroll').scrollWidth;
  } catch {
    if (request !== state.request) return;
    invalidateCurrent(state.data ? 'אין חיבור · ההיסטוריה מוצגת מהעדכון האחרון' : 'הנתונים אינם זמינים כרגע · נסו לרענן');
    if (!state.data) {
      text('chart-summary', 'ממתינים לחיבור לשירות הנתונים.');
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
  $('hour-chart').replaceChildren();
  text('chart-summary', 'טוען את הטווח שנבחר…');
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
