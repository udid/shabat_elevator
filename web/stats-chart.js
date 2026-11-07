import { formatMetric, formatDateTime, hourParts, coverageDetails } from './stats-model.js';

const svgNS = 'http://www.w3.org/2000/svg';
function node(name, attributes = {}, value) {
  const element = document.createElementNS(svgNS, name);
  for (const [key, attribute] of Object.entries(attributes)) element.setAttribute(key, String(attribute));
  if (value !== undefined) element.textContent = value;
  return element;
}

// Keep every hourly sample, even for long ranges: hourly unique visitors cannot
// be added together to calculate daily unique visitors.
export function createHourlyChart(container, chart, detail) {
  let data = null;
  let metric;
  let label;
  let selected = null;
  let points = [];
  let geometry;
  let selection;
  let renderedWidth = 0;
  let shabbatSpans = [];

  function select(index) {
    if (!points.length) return;
    selected = Math.max(0, Math.min(points.length - 1, index));
    const point = points[selected];
    const hour = data.hours[selected];
    const parts = hourParts(hour);
    selection.replaceChildren(node('line', {
      x1: point.x, x2: point.x, y1: geometry.top, y2: geometry.bottom,
      class: 'chart-cursor', 'data-index': selected,
    }));
    if (point.hasValue) selection.append(node('circle', {
      cx: point.x, cy: point.y, r: 5, class: 'chart-selected', 'data-index': selected,
    }));
    const heading = document.createElement('strong');
    heading.textContent = `${label}: ${point.hasValue ? formatMetric(hour[metric]) : 'אין נתונים'}`;
    const time = document.createElement('div');
    time.dir = 'ltr';
    time.textContent = `${parts.date} · ${parts.range}`;
    const coverage = document.createElement('small');
    coverage.textContent = `${point.coverage.label} · ${point.coverage.detail}`;
    detail.replaceChildren(heading, time, coverage);
    const shabbat = shabbatSpans.find((span) => Date.parse(span.start) < Date.parse(hour.end) && Date.parse(span.end) > Date.parse(hour.start));
    if (shabbat) {
      const note = document.createElement('small');
      note.className = 'chart-shabbat-detail';
      const fullHour = Date.parse(shabbat.start) <= Date.parse(hour.start) && Date.parse(shabbat.end) >= Date.parse(hour.end);
      note.textContent = `${fullHour ? 'שבת' : 'חלק מהשעה בשבת'} · כניסה ${formatDateTime(shabbat.start)} · יציאה ${formatDateTime(shabbat.end)}`;
      detail.append(note);
    }
  }

  function draw() {
    if (!data) return;
    const hours = data.hours;
    const width = container.clientWidth;
    if (!width) return;
    renderedWidth = width;
    const left = 48;
    const right = width - 14;
    const top = 30;
    const bottom = 208;
    const start = Date.parse(hours[0]?.start);
    const end = Date.parse(hours.at(-1)?.end);
    const timeX = (value) => left + (value - start) / (end - start) * (right - left);
    geometry = { left, right, top, bottom, width, start, end };
    chart.setAttribute('viewBox', `0 0 ${width} 260`);
    chart.replaceChildren(
      node('title', { id: 'chart-title' }, label),
      node('desc', { id: 'chart-description' }, 'גרף קווי לפי שעה, מהישן לחדש. רקע כחול מסמן שבת מזמן הכניסה ועד היציאה. פער בקו מציין שאין מדידה. קו מקווקו ונקודה חלולה מציינים מדידה חלקית. הערכים המדויקים מופיעים גם בטבלה.'),
    );
    for (const span of shabbatSpans) {
      const visibleStart = Math.max(start, Date.parse(span.start));
      const visibleEnd = Math.min(end, Date.parse(span.end));
      if (!(visibleEnd > visibleStart)) continue;
      const x = timeX(visibleStart);
      const bandWidth = timeX(visibleEnd) - x;
      chart.append(node('rect', {
        x, y: 8, width: bandWidth, height: bottom - 8, class: 'chart-shabbat',
        'data-start': span.start, 'data-end': span.end,
        'data-visible-start': new Date(visibleStart).toISOString(),
        'data-visible-end': new Date(visibleEnd).toISOString(), 'aria-hidden': 'true',
      }));
      for (const boundary of [Date.parse(span.start), Date.parse(span.end)]) {
        if (boundary >= start && boundary <= end) chart.append(node('line', {
          x1: timeX(boundary), x2: timeX(boundary), y1: 8, y2: bottom,
          class: 'chart-shabbat-boundary', 'aria-hidden': 'true',
        }));
      }
      if (bandWidth >= 32) chart.append(node('text', {
        x: x + bandWidth / 2, y: 22, 'text-anchor': 'middle', class: 'chart-shabbat-label', 'aria-hidden': 'true',
      }, 'שבת'));
    }
    const values = hours.map((hour) => hour[metric]).filter((value) => typeof value === 'number' && Number.isFinite(value));
    const maximum = Math.max(1, Math.max(0, ...values));
    const rawStep = maximum / 4;
    const magnitude = 10 ** Math.floor(Math.log10(rawStep));
    let step = [1, 2, 5, 10].map((factor) => factor * magnitude).find((value) => value >= rawStep);
    if (metric !== 'averageConcurrent') step = Math.max(1, step);
    const ceiling = Math.ceil(maximum / step) * step;
    for (let index = 0; index <= Math.round(ceiling / step); index += 1) {
      const value = index * step;
      const y = bottom - value / ceiling * (bottom - top);
      chart.append(node('line', { x1: left, x2: right, y1: y, y2: y, class: 'chart-grid' }));
      chart.append(node('text', { x: left - 8, y: y + 4, 'text-anchor': 'end', class: 'chart-label' }, formatMetric(value)));
    }
    points = hours.map((hour, index) => {
      const coverage = coverageDetails(hour, data.generatedAt);
      const value = hour[metric];
      return {
        x: timeX((Date.parse(hour.start) + Date.parse(hour.end)) / 2),
        y: bottom - (typeof value === 'number' ? value : 0) / ceiling * (bottom - top),
        hasValue: typeof value === 'number' && Number.isFinite(value) && coverage.kind !== 'missing',
        coverage,
      };
    });
    const paths = { complete: '', partial: '' };
    const previousEnds = { complete: -1, partial: -1 };
    points.forEach((point, index) => {
      const previous = points[index - 1];
      if (!point.hasValue || !previous?.hasValue) return;
      const kind = point.coverage.kind === 'partial' || previous.coverage.kind === 'partial' ? 'partial' : 'complete';
      if (previousEnds[kind] !== index - 1) paths[kind] += `M${previous.x},${previous.y}`;
      paths[kind] += `L${point.x},${point.y}`;
      previousEnds[kind] = index;
    });
    for (const [kind, d] of Object.entries(paths)) {
      if (d) chart.append(node('path', { d, class: `chart-line ${kind}` }));
    }
    points.forEach((point, index) => {
      if (!point.hasValue) return;
      const isolated = !points[index - 1]?.hasValue && !points[index + 1]?.hasValue;
      if (hours.length <= 168 || isolated || point.coverage.kind === 'partial') {
        chart.append(node('circle', {
          cx: point.x, cy: point.y, r: hours.length <= 48 || isolated ? 3 : 2,
          class: `chart-point ${point.coverage.kind}`, 'data-index': index,
        }));
      }
    });
    const count = Math.min(hours.length, Math.max(2, Math.min(8, Math.floor((right - left) / 100) + 1)));
    for (let index = 0; index < count; index += 1) {
      const hourIndex = count === 1 ? 0 : Math.round(index / (count - 1) * (hours.length - 1));
      const parts = hourParts(hours[hourIndex]);
      const anchor = count === 1 ? 'middle' : index === 0 ? 'start' : index === count - 1 ? 'end' : 'middle';
      const x = points[hourIndex].x;
      chart.append(node('text', { x, y: bottom + 22, 'text-anchor': anchor, class: 'chart-label' }, parts.short));
      chart.append(node('text', { x, y: bottom + 39, 'text-anchor': anchor, class: 'chart-label' }, parts.date.replace(/[./]\d{4}$/, '')));
    }
    selection = node('g', { 'aria-hidden': 'true' });
    chart.append(selection);
    if (hours.length) select(selected ?? hours.length - 1);
    else detail.textContent = 'אין נתונים לטווח שנבחר.';
  }

  function selectAt(event) {
    if (!geometry || !points.length) return;
    const bounds = chart.getBoundingClientRect();
    const x = (event.clientX - bounds.left) / bounds.width * geometry.width;
    const at = geometry.start + (x - geometry.left) / (geometry.right - geometry.left) * (geometry.end - geometry.start);
    const index = data.hours.findIndex((hour) => at < Date.parse(hour.end));
    select(index < 0 ? points.length - 1 : index);
  }
  chart.addEventListener('pointerdown', selectAt);
  chart.addEventListener('pointermove', (event) => { if (event.pointerType === 'mouse') selectAt(event); });
  container.addEventListener('keydown', (event) => {
    if (!points.length || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Home') select(0);
    else if (event.key === 'End') select(points.length - 1);
    else select((selected ?? points.length - 1) + (event.key === 'ArrowLeft' ? -1 : 1));
  });
  new ResizeObserver(() => { if (Math.abs(container.clientWidth - renderedWidth) > 1) draw(); }).observe(container);
  return {
    render(nextData, nextMetric, nextLabel, nextShabbatSpans = []) {
      if (data?.hours[0]?.start !== nextData.hours[0]?.start) selected = null;
      data = nextData;
      metric = nextMetric;
      label = nextLabel;
      shabbatSpans = nextShabbatSpans;
      draw();
    },
    clear() {
      data = null;
      points = [];
      selected = null;
      shabbatSpans = [];
      chart.replaceChildren();
      detail.textContent = 'טוען את הטווח שנבחר…';
    },
  };
}
