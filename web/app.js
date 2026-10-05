import { buildRoute, estimateState, nextArrival } from './model.js';
import { fetchCalendar, getActivityWindow, fetchWeather } from './services.js';

const $ = (id) => document.getElementById(id);
const state = {
  config: null, profile: null, floor: null, calendar: null, weather: null,
  live: null, liveReachable: false, polling: false, manualDemo: false,
  demoAnchor: null, wakeRequested: true, wakeLock: null, wakePending: false,
  toastTimer: null, lastCalendarFetch: 0, lastWeatherFetch: 0, calendarPending: false,
  weatherPending: false,
};

function setText(id, value) {
  const element = $(id);
  if (element && element.textContent !== String(value)) element.textContent = String(value);
}
function duration(seconds) {
  const value = Math.max(0, Math.ceil(seconds));
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
}
function validDate(value) {
  if (value === null || value === undefined || value === '') return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}
function formatTime(value, seconds = false) {
  const date = validDate(value);
  if (!date) return '—';
  return new Intl.DateTimeFormat('he-IL', {
    timeZone: state.config.timezone, hour: '2-digit', minute: '2-digit',
    ...(seconds ? { second: '2-digit' } : {}), hourCycle: 'h23',
  }).format(date);
}
function shortDate(value) {
  const date = validDate(value);
  if (!date) return '';
  return new Intl.DateTimeFormat('he-IL', { timeZone: state.config.timezone, weekday: 'short', day: 'numeric', month: 'numeric' }).format(date);
}
function observationTime(value, now) {
  const date = validDate(value);
  if (!date) return '—';
  const day = new Intl.DateTimeFormat('en-CA', { timeZone: state.config.timezone, year: 'numeric', month: '2-digit', day: '2-digit' });
  return `${day.format(date) === day.format(now) ? '' : `${shortDate(date)} · `}${formatTime(date, true)}`;
}
function announce(message) {
  setText('toast', message);
  $('toast').hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}
function storageKey() { return `shabat-elevator:${state.config.buildingId}:floor`; }
function setCountdown(value, message = false, empty = false) {
  const element = $('countdown');
  element.classList.toggle('message', message);
  element.classList.toggle('empty', empty);
  if (element.dataset.value !== value) {
    element.dataset.value = value;
    if (!message && value.includes(':')) {
      const [minutes, seconds] = value.split(':');
      element.replaceChildren(document.createTextNode(minutes), Object.assign(document.createElement('span'), { textContent: ':' }), document.createTextNode(seconds));
    } else element.textContent = value;
  }
  element.setAttribute('aria-label', message ? value : `זמן משוער להגעה: ${value}`);
}

function setupFloorPicker() {
  const select = $('floor-select');
  state.config.floors.forEach((floor) => {
    const option = document.createElement('option');
    option.value = String(floor);
    option.textContent = floor === 0 ? 'קומה 0 · כניסה' : `קומה \u2066${floor}\u2069`;
    select.append(option);
  });
  try {
    const saved = localStorage.getItem(storageKey());
    if (saved !== null && saved !== '' && state.config.floors.includes(Number(saved))) {
      state.floor = Number(saved);
      select.value = saved;
    }
  } catch { /* Private browsing or storage policies must not prevent use. */ }
  select.addEventListener('change', () => {
    state.floor = select.value === '' ? null : Number(select.value);
    try {
      if (state.floor === null) localStorage.removeItem(storageKey());
      else localStorage.setItem(storageKey(), String(state.floor));
    } catch { /* Selection remains available for the current visit. */ }
    render();
  });
}

function setupRoute() {
  const route = state.config.route;
  // The repeated zero is one stop with double dwell, not two arrivals.
  const groups = [];
  for (const floor of route) {
    const previous = groups.at(-1);
    if (previous?.floor === floor) previous.weight += 1;
    else groups.push({ floor, weight: 1 });
  }
  const secondZero = groups.findIndex((stop, index) => index > 0 && stop.floor === 0);
  const even = secondZero === -1 ? groups : groups.slice(0, secondZero);
  const odd = secondZero === -1 ? [] : groups.slice(secondZero);
  [['even-route', even], ['odd-route', odd]].forEach(([id, stops]) => {
    stops.forEach(({ floor, weight }) => {
      const node = document.createElement('span');
      node.className = `stop-node${weight > 1 ? ' double' : ''}`;
      node.dataset.floor = String(floor);
      node.textContent = String(floor);
      node.setAttribute('aria-label', `קומה ${floor}${weight > 1 ? ', עצירה כפולה' : ''}`);
      if (weight > 1) node.append(Object.assign(document.createElement('small'), { textContent: `×${weight}` }));
      $(id).append(node);
    });
  });
}

function renderClock(now) {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: state.config.timezone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(now);
  const part = (name) => parts.find((item) => item.type === name)?.value || '00';
  const clock = $('wall-clock');
  clock.replaceChildren(document.createTextNode(`${part('hour')}:${part('minute')}`), Object.assign(document.createElement('span'), { textContent: part('second') }));
  clock.dateTime = now.toISOString();
  setText('hebrew-date', new Intl.DateTimeFormat('he-IL-u-ca-hebrew', { timeZone: state.config.timezone, day: 'numeric', month: 'long', year: 'numeric' }).format(now));
  setText('gregorian-date', new Intl.DateTimeFormat('he-IL', { timeZone: state.config.timezone, weekday: 'long', day: 'numeric', month: 'numeric' }).format(now));
}

function activityAt(now) {
  if (!state.calendar) return { status: 'loading', active: false, window: null, nextWindow: null };
  try { return getActivityWindow(state.calendar, now, state.config.schedulePaddingMinutes); }
  catch { return { status: 'unavailable', active: false, window: null, nextWindow: null }; }
}

function renderCalendar(activity) {
  const span = activity.window || activity.nextWindow;
  if (span) {
    setText('shabbat-title', span.holidays?.length ? 'שבת וחג' : (activity.active ? 'השבת שלנו' : 'השבת הקרובה'));
    setText('candle-time', formatTime(span.eventStart));
    setText('havdalah-time', formatTime(span.eventEnd));
    setText('candle-date', shortDate(span.eventStart));
    setText('havdalah-date', shortDate(span.eventEnd));
    setText('schedule-note', `פעילות המעלית: ${shortDate(span.start)}, ${formatTime(span.start)} עד ${shortDate(span.end)}, ${formatTime(span.end)}`);
  } else if (activity.status !== 'loading') {
    setText('candle-time', '--:--');
    setText('havdalah-time', '--:--');
    setText('candle-date', '');
    setText('havdalah-date', '');
    setText('schedule-note', 'זמני שבת וחג אינם זמינים');
  }
  const parasha = state.calendar?.parasha;
  setText('parasha-name', parasha?.title || (state.calendar?.status === 'unavailable' ? 'לא זמין כרגע' : '—'));
  setText('parasha-detail', parasha?.date ? shortDate(parasha.date) : '');
  const holidays = (span?.holidays || []).map((holiday) => typeof holiday === 'string' ? holiday : (holiday.hebrew || holiday.title)).filter(Boolean);
  setText('holiday-label', [...new Set(holidays)].join(' · '));
  $('holiday-label').hidden = holidays.length === 0;
}

function renderWeather() {
  const weather = state.weather;
  if (!weather) return;
  if (weather.status === 'unavailable' || !weather.current || (validDate(weather.expiresAt) && Date.now() > new Date(weather.expiresAt).getTime())) {
    setText('weather-temperature', '—°');
    setText('weather-description', 'מזג האוויר אינו זמין כרגע');
    setText('weather-detail', '');
    return;
  }
  setText('weather-temperature', `${Math.round(weather.current.temperature)}°`);
  setText('weather-description', weather.current.label || '');
  const today = weather.today;
  const range = today && Number.isFinite(today.min) && Number.isFinite(today.max) ? `היום ${Math.round(today.min)}°–${Math.round(today.max)}° · ` : '';
  setText('weather-detail', `${range}עדכון ${formatTime(weather.fetchedAt)}`);
}

function liveModel(now) {
  const live = state.live;
  const anchor = validDate(live?.lastArrivalAt);
  const seen = validDate(live?.lastSeenAt);
  const cycle = Number(live?.cycleSeconds);
  const cycleValid = Number.isFinite(cycle) && cycle > 0 && live?.cycleSeconds !== null;
  const fresh = seen && now - seen <= state.config.staleAfterSeconds * 1000 && seen - now <= 5000;
  const connected = state.liveReachable && live?.sourceConnected === true && fresh;
  const anchorValid = anchor && anchor <= now && cycleValid;
  const withinCycle = anchorValid && now - anchor < cycle * 1000;
  const usable = connected && withinCycle && live.measurementStatus === 'tracking';
  return { usable, anchor: anchor?.getTime() ?? null, cycle: cycleValid ? cycle : null, connected, seen, live };
}

function render() {
  if (!state.config) return;
  const now = new Date();
  renderClock(now);
  const activity = activityAt(now);
  renderCalendar(activity);
  const demo = state.manualDemo;
  const simulation = demo || state.config.sourceMode !== 'live';
  const scheduled = activity.active === true;
  const live = simulation ? null : liveModel(now);
  let anchor = null;
  let cycle = simulation ? state.config.cycleSeconds : live.cycle;
  let available = false;
  if (simulation && (demo || scheduled)) {
    // All clients share the activity window as the simulation epoch. Preview
    // has a separate epoch and never writes or impersonates a sensor event.
    const epoch = demo ? state.demoAnchor : new Date(activity.window.start).getTime();
    if (Number.isFinite(epoch)) {
      anchor = epoch + Math.floor((now.getTime() - epoch) / (cycle * 1000)) * cycle * 1000;
      available = true;
    }
  } else if (!simulation) {
    anchor = live.anchor;
    available = live.usable && scheduled;
  }
  const scheduleKnown = activity.status === 'live';
  $('mode-banner').classList.toggle('live-mode', !simulation);
  $('mode-banner').classList.toggle('disconnected', !simulation && !live.connected);
  setText('mode-tag', simulation ? 'הדגמה' : live.connected ? 'חיישן מחובר' : 'חיישן מנותק');
  setText('mode-copy', simulation ? 'הנתונים אינם מהמעלית' : live.seen ? `עדכון חיישן ${observationTime(live.seen, now)}` : 'טרם התקבל דיווח');
  $('demo-button').disabled = false;
  $('demo-button').setAttribute('aria-pressed', String(demo));
  setText('demo-button-label', demo ? 'סיום ההדגמה' : 'הפעלת הדגמה');

  const position = available ? estimateState(state.profile, anchor, now.getTime(), cycle) : null;
  $('position-readout').hidden = !position;
  $('elevator-visual').hidden = !position;
  $('arrival-layout').classList.toggle('without-position', !position);
  if (position) {
    setText('current-floor', position.phase === 'stopped' ? position.floor : position.nextFloor);
    setText('position-label', position.phase === 'stopped' ? 'עצירה משוערת' : 'התחנה הבאה · אומדן');
    $('position-readout').setAttribute('aria-label', position.phase === 'stopped' ? `עצירה משוערת בקומה ${position.floor}` : `התחנה הבאה ${position.nextFloor}, ${position.direction === 'up' ? 'בעלייה' : 'בירידה'}, לפי התחזית`);
    $('direction-arrow').classList.toggle('down', position.direction === 'down');
    $('direction-arrow').classList.toggle('stopped', position.phase === 'stopped');
  } else {
    setText('current-floor', '—');
    setText('position-label', 'מיקום משוער');
    $('direction-arrow').classList.add('stopped');
  }
  document.querySelectorAll('.stop-node').forEach((node) => {
    node.classList.toggle('selected', state.floor !== null && Number(node.dataset.floor) === state.floor);
    node.classList.toggle('current', position?.phase === 'stopped' && Number(node.dataset.floor) === position.floor);
  });

  $('countdown').hidden = !available || state.floor === null;
  $('arrival-note').hidden = true;
  $('countdown-label').classList.toggle('forecast-message', !available || state.floor === null);
  if (!available) {
    setCountdown('--:--', false, true);
    if (!scheduled && !demo && scheduleKnown) {
      setText('countdown-label', 'מחוץ לשעות הפעילות');
      const next = activity.nextWindow;
      setText('arrival-note', next ? `הפעילות הבאה: ${shortDate(next.start)} בשעה ${formatTime(next.start)}` : '');
      $('arrival-note').hidden = !next;
    } else if (!scheduleKnown && !demo) {
      setText('countdown-label', activity.status === 'loading' ? 'טוען את זמני השבת והחג' : 'זמני הפעילות אינם זמינים');
    } else if (!simulation) {
      const waiting = !live.anchor || !live.cycle || live.live?.measurementStatus === 'waiting';
      setText('countdown-label', !live.connected ? 'התחזית אינה זמינה' : waiting ? 'ממתינים למדידת מחזור' : 'התחזית ממתינה לסנכרון');
    }
  } else if (state.floor === null) {
    setCountdown('--:--', false, true);
    setText('countdown-label', 'בחרו קומה להצגת התחזית');
  } else {
    const arrival = nextArrival(state.profile, anchor, now.getTime(), state.floor, cycle);
    setText('countdown-label', 'זמן משוער להגעה');
    if (arrival.isHere) {
      setCountdown('בקומה שלכם', true);
    } else {
      setCountdown(duration(arrival.seconds));
    }
  }

  setText('last-arrival-label', simulation ? 'זיהוי מדומה בקומה 7' : 'זיהוי אחרון בקומה 7');
  setText('last-arrival', observationTime(anchor, now));
  setText('cycle-duration', cycle ? duration(cycle) : '—');
}

async function refreshCalendar() {
  if (state.calendarPending) return;
  state.calendarPending = true;
  try {
    const calendar = await fetchCalendar(state.config, new Date());
    if (calendar.status !== 'unavailable' || !state.calendar || (validDate(state.calendar.expiresAt) && Date.now() > new Date(state.calendar.expiresAt).getTime())) state.calendar = calendar;
  } catch { if (!state.calendar) state.calendar = { status: 'unavailable' }; }
  finally { state.calendarPending = false; state.lastCalendarFetch = Date.now(); render(); }
}
async function refreshWeather() {
  if (state.weatherPending) return;
  state.weatherPending = true;
  try { state.weather = await fetchWeather(state.config); }
  catch { state.weather = { status: 'unavailable' }; }
  finally { state.weatherPending = false; state.lastWeatherFetch = Date.now(); renderWeather(); }
}

async function pollLive() {
  if (state.config.sourceMode !== 'live' || state.polling) return;
  state.polling = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const base = (state.config.liveApiUrl || '').replace(/\/$/, '');
    const response = await fetch(`${base}/api/state`, { signal: controller.signal, cache: 'no-store', credentials: 'omit' });
    if (!response.ok) throw new Error('State source unavailable');
    const live = await response.json();
    if (!live || live.mode !== 'live') throw new Error('Invalid state response');
    state.live = live;
    state.liveReachable = true;
  } catch { state.liveReachable = false; }
  finally { clearTimeout(timeout); state.polling = false; render(); }
}

function supportsWakeLock() {
  return window.isSecureContext && typeof navigator.wakeLock?.request === 'function';
}
function renderWakeState() {
  const supported = supportsWakeLock();
  $('wake-button').disabled = !supported;
  $('wake-button').setAttribute('aria-pressed', String(supported && state.wakeRequested));
  setText('wake-label', !supported ? 'השארת מסך דולק אינה נתמכת' : state.wakeLock ? 'המסך נשאר דולק' : state.wakeRequested ? state.wakePending ? 'מבקש להשאיר מסך דולק…' : 'ממתין לחזרת האתר למסך' : 'השארת מסך דולק');
  setText('wake-description', !supported ? 'אפשר לשנות את זמן כיבוי המסך בהגדרות המכשיר.' : state.wakeLock ? 'נעילת המסך פעילה. השאירו את האתר גלוי ואת המכשיר מחובר לחשמל.' : 'אפשר לבקש מהדפדפן לשמור על המסך דולק כשהאתר גלוי.');
}
async function acquireWakeLock() {
  if (!supportsWakeLock() || !state.wakeRequested || state.wakeLock || state.wakePending || document.visibilityState !== 'visible') return;
  state.wakePending = true;
  renderWakeState();
  try {
    const lock = await navigator.wakeLock.request('screen');
    if (!state.wakeRequested) { await lock.release(); return; }
    state.wakeLock = lock;
    lock.addEventListener('release', () => {
      if (state.wakeLock !== lock) return;
      state.wakeLock = null;
      renderWakeState();
      // A system release while visible can reflect low power or a platform
      // policy. Ask again only after the next visibility change/user action.
      if (state.wakeRequested && document.visibilityState === 'visible') {
        state.wakeRequested = false;
        renderWakeState();
        announce('המכשיר שחרר את נעילת המסך. ניתן להפעיל אותה שוב.');
      }
    });
  } catch {
    state.wakeRequested = false;
    announce('הדפדפן לא אישר להשאיר את המסך דולק. בדקו את הגדרות המכשיר.');
  } finally { state.wakePending = false; renderWakeState(); }
}

function setupControls() {
  $('demo-button').addEventListener('click', () => {
    state.manualDemo = !state.manualDemo;
    if (state.manualDemo) state.demoAnchor = Date.now();
    render();
  });
  $('wake-button').addEventListener('click', async () => {
    state.wakeRequested = !state.wakeRequested;
    if (state.wakeRequested) await acquireWakeLock();
    else if (state.wakeLock) {
      const lock = state.wakeLock;
      state.wakeLock = null;
      renderWakeState();
      try { await lock.release(); } catch { /* Already released. */ }
    }
    renderWakeState();
  });
  const fullscreen = $('fullscreen-button');
  fullscreen.hidden = !document.fullscreenEnabled;
  fullscreen.addEventListener('click', async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch { announce('מסך מלא אינו זמין כרגע בדפדפן הזה.'); }
  });
  document.addEventListener('fullscreenchange', () => {
    const label = document.fullscreenElement ? 'יציאה ממסך מלא' : 'מעבר למסך מלא';
    fullscreen.setAttribute('aria-label', label);
    fullscreen.title = label;
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    acquireWakeLock();
    render();
    pollLive();
    refreshServicesIfNeeded();
  });
  window.addEventListener('online', () => { refreshCalendar(); refreshWeather(); pollLive(); });
  renderWakeState();
  acquireWakeLock();
}

function refreshServicesIfNeeded() {
  const now = Date.now();
  const calendarInterval = state.calendar?.status === 'unavailable' ? 60_000 : 6 * 60 * 60_000;
  const weatherInterval = state.weather?.status === 'unavailable' ? 60_000 : 15 * 60_000;
  if (now - state.lastCalendarFetch > calendarInterval) refreshCalendar();
  if (now - state.lastWeatherFetch > weatherInterval) refreshWeather();
}

async function init() {
  try {
    const response = await fetch('./config.json', { cache: 'no-store' });
    if (!response.ok) throw new Error('Configuration unavailable');
    const config = await response.json();
    if (!Array.isArray(config.floors) || !config.floors.length || !config.timezone) throw new Error('Invalid configuration');
    state.config = config;
    state.profile = buildRoute(config);
    setText('city-name', config.city);
    setupFloorPicker();
    setupRoute();
    setupControls();
    render();
    setInterval(render, 1000);
    setInterval(pollLive, 5000);
    setInterval(() => { refreshServicesIfNeeded(); renderWeather(); }, 30_000);
    await Promise.allSettled([refreshCalendar(), refreshWeather(), pollLive()]);
  } catch (error) {
    setText('mode-tag', 'לא זמין');
    setText('mode-copy', '');
    setText('countdown-label', 'טעינת האתר נכשלה · נסו לרענן');
    $('countdown').hidden = true;
    $('arrival-note').hidden = true;
    $('position-readout').hidden = true;
    console.error('Unable to initialize elevator display', error);
  }
}

init();
