/** Pure timing model. Profiles start at arrival; live anchors may record departure. */
export const DEFAULT_ROUTE = Object.freeze([0, 0, 12, 10, 8, 6, 4, 2, -1, 0, 11, 9, 7, 5, 3, 1, -1]);

function positive(value, name) {
  if (!Number.isFinite(value) || value <= 0) throw new RangeError(`${name} must be positive`);
  return value;
}

function finite(value, name) {
  if (!Number.isFinite(value)) throw new RangeError(`${name} must be finite`);
  return value;
}

function modulo(value, divisor) {
  const remainder = value % divisor;
  return remainder < 0 ? remainder + divisor : remainder;
}

/**
 * Repeated adjacent floors extend a dwell; distinct visits remain independent.
 * All durations are scaled to cycleSeconds. Default timings are demo assumptions.
 */
export function buildRoute(config = {}) {
  const route = config.route ?? DEFAULT_ROUTE;
  const cycleSeconds = positive(config.cycleSeconds ?? 570, 'cycleSeconds');
  const travelSecondsPerFloor = positive(config.travelSecondsPerFloor ?? 3, 'travelSecondsPerFloor');
  const dwellSeconds = positive(config.dwellSeconds ?? 12, 'dwellSeconds');
  const anchorFloor = config.anchorFloor ?? 7;
  const stopWeights = config.stopWeights ?? {};
  if (!Array.isArray(route) || route.length < 2 || route.some(floor => !Number.isInteger(floor) || floor < -1 || floor > 12)) {
    throw new RangeError('route must contain floors from -1 to 12');
  }
  const visits = [];
  for (const floor of route) {
    const weight = positive(stopWeights[floor] ?? 1, `stopWeights[${floor}]`);
    if (visits.at(-1)?.floor === floor) visits.at(-1).weight += weight;
    else visits.push({ floor, weight });
  }
  // A route is circular, including the seam in a user-supplied profile.
  if (visits.length > 1 && visits[0].floor === visits.at(-1).floor) {
    visits[0].weight += visits.pop().weight;
  }
  if (visits.length < 2) throw new RangeError('route must visit at least two different floors');
  const anchorIndex = visits.findIndex(stop => stop.floor === anchorFloor);
  if (anchorIndex < 0) throw new RangeError('anchorFloor must be a stop on the route');
  const ordered = [...visits.slice(anchorIndex), ...visits.slice(0, anchorIndex)];
  const rawDuration = ordered.reduce((total, stop, index) => {
    const next = ordered[(index + 1) % ordered.length];
    return total + stop.weight * dwellSeconds + Math.abs(next.floor - stop.floor) * travelSecondsPerFloor;
  }, 0);
  const scale = cycleSeconds / rawDuration;
  const stops = [];
  const segments = [];
  let cursor = 0;
  ordered.forEach((visit, visitIndex) => {
    const nextFloor = ordered[(visitIndex + 1) % ordered.length].floor;
    const arrivalSeconds = cursor;
    const departureSeconds = cursor + visit.weight * dwellSeconds * scale;
    const stop = { ...visit, visitIndex, arrivalSeconds, departureSeconds, dwellSeconds: departureSeconds - arrivalSeconds, nextFloor };
    stops.push(stop);
    segments.push({ phase: 'stopped', floor: visit.floor, nextFloor, direction: null, visitIndex, startSeconds: cursor, endSeconds: departureSeconds });
    cursor = departureSeconds;
    const endSeconds = visitIndex === ordered.length - 1
      ? cycleSeconds
      : cursor + Math.abs(nextFloor - visit.floor) * travelSecondsPerFloor * scale;
    segments.push({ phase: 'moving', floor: null, fromFloor: visit.floor, nextFloor, direction: nextFloor > visit.floor ? 'up' : 'down', visitIndex, startSeconds: cursor, endSeconds });
    cursor = endSeconds;
  });
  return { cycleSeconds, anchorFloor, rawRoute: [...route], stops, segments };
}

function timing(profile, anchorMs, nowMs, cycleSeconds, anchorKind) {
  finite(anchorMs, 'anchorMs');
  finite(nowMs, 'nowMs');
  positive(profile?.cycleSeconds, 'profile.cycleSeconds');
  const actualCycle = positive(cycleSeconds ?? profile.cycleSeconds, 'cycleSeconds');
  const secondsSinceAnchor = (nowMs - anchorMs) / 1000;
  const cycleIndex = Math.floor(secondsSinceAnchor / actualCycle);
  const elapsedCycleSeconds = modulo(secondsSinceAnchor, actualCycle);
  const scale = actualCycle / profile.cycleSeconds;
  if (anchorKind !== 'arrival' && anchorKind !== 'departure') throw new RangeError('anchorKind must be arrival or departure');
  const anchorOffset = anchorKind === 'departure' ? profile.stops[0].departureSeconds : 0;
  const profilePhase = modulo(elapsedCycleSeconds / scale + anchorOffset, profile.cycleSeconds);
  return { actualCycle, secondsSinceAnchor, cycleIndex, elapsedCycleSeconds, scale, profilePhase };
}

/** Numeric floor means a predicted dwell. In transit, floor is deliberately null. */
export function estimateState(profile, anchorMs, nowMs, cycleSeconds, anchorKind = 'arrival') {
  const time = timing(profile, anchorMs, nowMs, cycleSeconds, anchorKind);
  const segment = profile.segments.find(item => time.profilePhase >= item.startSeconds && time.profilePhase < item.endSeconds)
    ?? profile.segments[0];
  const segmentDuration = segment.endSeconds - segment.startSeconds;
  const progress = Math.min(1, Math.max(0, (time.profilePhase - segment.startSeconds) / segmentDuration));
  return {
    floor: segment.floor,
    direction: segment.direction,
    phase: segment.phase,
    nextFloor: segment.nextFloor,
    fromFloor: segment.fromFloor ?? segment.floor,
    progress,
    secondsSinceAnchor: time.secondsSinceAnchor,
    phaseSeconds: time.elapsedCycleSeconds,
    cycleIndex: time.cycleIndex,
    visitIndex: segment.visitIndex,
    remainingSeconds: (segment.endSeconds - time.profilePhase) * time.scale,
  };
}

/** Finds the nearest visit, including the current dwell, for repeated route stops. */
export function nextArrival(profile, anchorMs, nowMs, targetFloor, cycleSeconds, anchorKind = 'arrival') {
  const time = timing(profile, anchorMs, nowMs, cycleSeconds, anchorKind);
  const visits = profile.stops.filter(stop => stop.floor === targetFloor);
  if (!visits.length) throw new RangeError('targetFloor must be a stop on the route');
  const current = visits.find(stop => time.profilePhase >= stop.arrivalSeconds && time.profilePhase < stop.departureSeconds);
  if (current) {
    return {
      seconds: 0,
      arrivalMs: nowMs - (time.profilePhase - current.arrivalSeconds) * time.scale * 1000,
      isHere: true,
    };
  }
  const seconds = Math.min(...visits.map(stop => modulo((stop.arrivalSeconds - time.profilePhase) * time.scale, time.actualCycle)));
  return { seconds, arrivalMs: nowMs + seconds * 1000, isHere: false };
}

function timestamp(value) {
  // Network timestamps must include their timezone; never interpret an empty or
  // numeric value as a local date or the Unix epoch.
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const result = Date.parse(value);
  return Number.isFinite(result) ? result : null;
}

/** Validate a sensor snapshot without advancing or inventing its observation. */
export function liveTiming(live, nowMs, { reachable = false, staleAfterSeconds = 90 } = {}) {
  finite(nowMs, 'nowMs');
  positive(staleAfterSeconds, 'staleAfterSeconds');
  // A legacy arrival snapshot is supported only when its event kind is absent.
  // A departure snapshot must never fall back to its old lastArrivalAt field.
  const anchorKind = live?.anchorKind === undefined ? 'arrival' : live.anchorKind;
  const kindValid = anchorKind === 'departure' || anchorKind === 'arrival';
  const observed = timestamp(anchorKind === 'departure' ? live?.lastDepartureAt : live?.lastArrivalAt);
  const anchor = kindValid && observed !== null && observed <= nowMs ? observed : null;
  const seen = timestamp(live?.lastSeenAt);
  const cycleSource = live?.cycleSource === 'default' || live?.cycleSource === 'measured' ? live.cycleSource : null;
  const legacyArrival = live?.anchorKind === undefined && anchorKind === 'arrival';
  const cycleValid = typeof live?.cycleSeconds === 'number' && Number.isFinite(live.cycleSeconds)
    && live.cycleSeconds > 300 && live.cycleSeconds < 1800 && (cycleSource !== null || legacyArrival);
  const cycle = cycleValid ? live.cycleSeconds : null;
  const fresh = seen !== null && nowMs - seen <= staleAfterSeconds * 1000 && seen - nowMs <= 5000;
  const connected = reachable === true && live?.mode === 'live' && live?.sourceConnected === true && fresh;
  const withinCycle = anchor !== null && cycle !== null && nowMs - anchor < cycle * 1000;
  const monitorOnly = live?.monitorOnly === true;
  const usable = !monitorOnly && connected && withinCycle && seen >= anchor && live?.measurementStatus === 'tracking';
  return { usable, anchor, anchorKind: kindValid ? anchorKind : null, cycle, cycleSource, connected, seen, monitorOnly, live };
}
