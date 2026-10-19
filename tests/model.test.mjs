import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildRoute, estimateState, nextArrival, liveTiming, DEFAULT_ROUTE } from '../web/model.js';

const profile = buildRoute();
const anchorMs = 1_800_000_000_000;
const at = seconds => anchorMs + seconds * 1000;
const approximately = (actual, expected, epsilon = 0.001) => assert.ok(Math.abs(actual - expected) < epsilon, `${actual} ≈ ${expected}`);

test('fixed travel overhead applies once per trip, including the route boundary', () => {
  const route = buildRoute({ route: [0, 1, 12], anchorFloor: 0, cycleSeconds: 87,
    dwellSeconds: 10, travelSecondsPerFloor: 2, travelStartSeconds: 3 });
  assert.deepEqual(route.segments.filter(s => s.phase === 'moving').map(s => s.endSeconds - s.startSeconds), [5, 25, 27]);
  for (const value of [-1, Infinity, NaN, '3']) {
    assert.throws(() => buildRoute({ travelStartSeconds: value }), RangeError);
  }
});

test('site timing fits held-out weak chimes using only the preceding anchor and a fixed cycle', () => {
  const config = JSON.parse(readFileSync(new URL('../web/config.json', import.meta.url)));
  const evidence = JSON.parse(readFileSync(new URL('./fixtures/weak-floor-departures.json', import.meta.url)));
  const route = buildRoute(config);
  const ground = route.stops.filter(s => s.floor === 0);
  approximately(ground[0].dwellSeconds, ground[1].dwellSeconds * 3);
  assert.equal(route.stops.length, 16);
  assert.ok(route.segments.every(s => s.endSeconds > s.startSeconds));
  for (const floor of [8, 6]) {
    const stop = route.stops.find(s => s.floor === floor);
    const seconds = (stop.departureSeconds - route.stops[0].departureSeconds) * evidence.cycleSeconds / route.cycleSeconds;
    const errors = evidence.events.filter(e => e.floor === floor).map(e => Math.abs(seconds - e.elapsedSeconds));
    assert.equal(errors.length, 24);
    assert.ok(errors.reduce((a, b) => a + b, 0) / errors.length < 1.2, `floor ${floor}: held-out mean error`);
    assert.ok(Math.max(...errors) < 3.1, `floor ${floor}: held-out maximum error`);
    const arrival = nextArrival(route, anchorMs, anchorMs, floor, evidence.cycleSeconds, 'departure');
    assert.ok(arrival.seconds < seconds);
    assert.equal(estimateState(route, anchorMs, at(arrival.seconds + 1), evidence.cycleSeconds, 'departure').floor, floor);
    assert.equal(estimateState(route, anchorMs, at(seconds + 1), evidence.cycleSeconds, 'departure').phase, 'moving');
  }
});

test('demo completes a 570-second cycle with the complete ordered route', () => {
  assert.equal(profile.cycleSeconds, 570);
  assert.equal(profile.segments.at(-1).endSeconds, 570);
  approximately(profile.segments.reduce((sum, item) => sum + item.endSeconds - item.startSeconds, 0), 570);
  assert.deepEqual(profile.stops.map(stop => stop.floor), [7, 5, 3, 1, -1, 0, 12, 10, 8, 6, 4, 2, -1, 0, 11, 9]);
  assert.deepEqual(DEFAULT_ROUTE, [0, 0, 12, 10, 8, 6, 4, 2, -1, 0, 11, 9, 7, 5, 3, 1, -1]);
});

test('only the consecutive ground-floor visit has twice the dwell', () => {
  const groundStops = profile.stops.filter(stop => stop.floor === 0);
  assert.equal(groundStops.length, 2);
  approximately(groundStops[0].dwellSeconds, groundStops[1].dwellSeconds * 2);
  assert.equal(profile.segments.some(segment => segment.phase === 'moving' && segment.fromFloor === segment.nextFloor), false);
});

test('arrival anchor means floor 7, including after multiple full cycles', () => {
  for (const elapsed of [0, 570, 1710]) {
    const state = estimateState(profile, anchorMs, at(elapsed));
    assert.equal(state.floor, 7);
    assert.equal(state.phase, 'stopped');
    assert.equal(state.nextFloor, 5);
    assert.equal(state.secondsSinceAnchor, elapsed);
    assert.equal(nextArrival(profile, anchorMs, at(elapsed), 7).isHere, true);
  }
});

test('departure changes to motion without claiming an intermediate floor', () => {
  const departure = profile.stops[0].departureSeconds;
  const stopped = estimateState(profile, anchorMs, at(departure - 0.01));
  const moving = estimateState(profile, anchorMs, at(departure + 0.01));
  assert.equal(stopped.floor, 7);
  assert.equal(moving.phase, 'moving');
  assert.equal(moving.floor, null);
  assert.equal(moving.direction, 'down');
  assert.equal(moving.nextFloor, 5);
  assert.ok(moving.progress > 0 && moving.progress < 1);
});

test('basement floor -1 is a real stop, not a direction sentinel', () => {
  const basement = profile.stops.find(stop => stop.floor === -1);
  const state = estimateState(profile, anchorMs, at(basement.arrivalSeconds + 1));
  assert.equal(state.floor, -1);
  assert.equal(state.phase, 'stopped');
  assert.equal(state.direction, null);
  assert.equal(nextArrival(profile, anchorMs, at(basement.arrivalSeconds + 1), -1).seconds, 0);
});

test('countdown selects the next occurrence when a floor appears twice', () => {
  for (const floor of [0, -1]) {
    const [first, second] = profile.stops.filter(stop => stop.floor === floor);
    const elapsed = first.departureSeconds + 1;
    const result = nextArrival(profile, anchorMs, at(elapsed), floor);
    approximately(result.seconds, second.arrivalSeconds - elapsed);
    approximately(result.arrivalMs, at(second.arrivalSeconds));
    assert.equal(result.isHere, false);
    const afterSecond = second.departureSeconds + 1;
    approximately(nextArrival(profile, anchorMs, at(afterSecond), floor).seconds, 570 + first.arrivalSeconds - afterSecond);
  }
});

test('last segment returns to the anchor at the cycle boundary', () => {
  const justBefore = estimateState(profile, anchorMs, at(569.99));
  assert.equal(justBefore.phase, 'moving');
  assert.equal(justBefore.floor, null);
  assert.equal(justBefore.nextFloor, 7);
  approximately(nextArrival(profile, anchorMs, at(569.99), 7).seconds, 0.01);
  assert.equal(estimateState(profile, anchorMs, at(570)).floor, 7);
});

test('configured cycle override stretches the full profile consistently', () => {
  const target = profile.stops[1];
  const override = 660;
  const expectedArrival = target.arrivalSeconds * override / 570;
  approximately(nextArrival(profile, anchorMs, anchorMs, target.floor, override).seconds, expectedArrival);
  assert.equal(estimateState(profile, anchorMs, at(expectedArrival + 1), override).floor, target.floor);
  assert.equal(estimateState(profile, anchorMs, at(660), override).floor, 7);
});

test('configurable stop weights preserve the total duration', () => {
  const weighted = buildRoute({ cycleSeconds: 600, stopWeights: { 7: 2 } });
  assert.equal(weighted.segments.at(-1).endSeconds, 600);
  approximately(weighted.stops[0].dwellSeconds, weighted.stops[1].dwellSeconds * 2);
});

test('malformed timing and unsupported floors fail explicitly', () => {
  assert.throws(() => buildRoute({ cycleSeconds: 0 }), RangeError);
  assert.throws(() => buildRoute({ route: [0, 13] }), RangeError);
  assert.throws(() => buildRoute({ route: [0, 0] }), RangeError);
  assert.throws(() => buildRoute({ stopWeights: { 0: -1 } }), RangeError);
  assert.throws(() => estimateState(profile, null, anchorMs), RangeError);
  assert.throws(() => nextArrival(profile, anchorMs, anchorMs, null), RangeError);
  assert.throws(() => estimateState(profile, anchorMs, anchorMs, 570, 'chime'), RangeError);
});

test('a departure anchor starts motion and forecasts return before the next departure', () => {
  const position = estimateState(profile, anchorMs, anchorMs, 570, 'departure');
  assert.equal(position.phase, 'moving');
  assert.equal(position.floor, null);
  assert.equal(position.fromFloor, 7);
  assert.equal(position.nextFloor, 5);
  assert.equal(position.direction, 'down');
  approximately(position.progress, 0);
  const next = nextArrival(profile, anchorMs, anchorMs, 7, 570, 'departure');
  assert.equal(next.isHere, false);
  approximately(next.seconds, 570 - profile.stops[0].dwellSeconds);
  approximately(next.arrivalMs, at(next.seconds));
  const target = profile.stops.find(stop => stop.floor === 5);
  approximately(nextArrival(profile, anchorMs, anchorMs, 5, 570, 'departure').seconds,
    target.arrivalSeconds - profile.stops[0].departureSeconds);
});

test('departure profile wraps into the anchor dwell and scales without losing the observation', () => {
  for (const cycle of [570, 900]) {
    const dwell = profile.stops[0].dwellSeconds * cycle / profile.cycleSeconds;
    const expectedArrival = cycle - dwell;
    const duringDwell = at(expectedArrival + 1);
    const position = estimateState(profile, anchorMs, duringDwell, cycle, 'departure');
    assert.equal(position.phase, 'stopped');
    assert.equal(position.floor, 7);
    const arrival = nextArrival(profile, anchorMs, duringDwell, 7, cycle, 'departure');
    assert.equal(arrival.isHere, true);
    approximately(arrival.arrivalMs, at(expectedArrival));
    assert.equal(estimateState(profile, anchorMs, at(cycle), cycle, 'departure').phase, 'moving');
    const laterArrival = nextArrival(profile, anchorMs, at(expectedArrival + cycle + 1), 7, cycle, 'departure');
    approximately(laterArrival.arrivalMs, at(expectedArrival + cycle));
  }
});

const departureSnapshot = (overrides = {}) => ({
  mode: 'live', anchorKind: 'departure', sourceConnected: true,
  lastDepartureAt: new Date(anchorMs).toISOString(), lastArrivalAt: null,
  lastSeenAt: new Date(at(1)).toISOString(), cycleSeconds: 558,
  cycleSource: 'configured', latestCycleSeconds: null, measurementStatus: 'tracking', calibrated: false,
  ...overrides,
});
const connected = { reachable: true, staleAfterSeconds: 180 };

test('a confirmed departure forecasts using the configured cycle', () => {
  const live = liveTiming(departureSnapshot(), at(2), connected);
  assert.equal(live.usable, true);
  assert.equal(live.anchorKind, 'departure');
  assert.equal(live.anchor, anchorMs);
  assert.equal(live.cycleSource, 'configured');
  assert.equal(live.cycle, 558);
});

test('a configured cycle alone cannot start forecasting before a confirmed departure', () => {
  const waiting = liveTiming(departureSnapshot({ lastDepartureAt: null, measurementStatus: 'waiting' }), at(2), connected);
  assert.equal(waiting.usable, false);
  assert.equal(waiting.anchor, null);
  assert.equal(waiting.cycleSource, 'configured');
  assert.equal(waiting.cycle, 558);
  assert.equal(waiting.connected, true);
  const restored = liveTiming(departureSnapshot({ measurementStatus: 'waiting' }), at(2), connected);
  assert.equal(restored.usable, false);
  assert.equal(restored.anchor, anchorMs);
});

test('older default and measured cycle sources remain readable during deployment', () => {
  for (const cycleSource of ['default', 'measured']) {
    const live = liveTiming(departureSnapshot({ cycleSource, cycleSeconds: 600 }), at(2), connected);
    assert.equal(live.usable, true);
    assert.equal(live.cycleSource, cycleSource);
    assert.equal(live.cycle, 600);
  }
});

test('missed detections keep forecasting repeated cycles from the unchanged real departure', () => {
  const cycle = 514.5;
  const dwell = profile.stops[0].dwellSeconds * cycle / profile.cycleSeconds;
  for (const missedCycles of [1, 2, 4]) {
    for (const phase of [0, 3]) {
      const elapsed = missedCycles * cycle + phase;
      const snapshot = Object.freeze(departureSnapshot({
        cycleSeconds: cycle,
        lastSeenAt: new Date(at(elapsed)).toISOString(),
      }));
      const before = { ...snapshot };
      const live = liveTiming(snapshot, at(elapsed), connected);
      assert.equal(live.usable, true);
      assert.equal(live.anchor, anchorMs);
      const position = estimateState(profile, live.anchor, at(elapsed), live.cycle, live.anchorKind);
      assert.equal(position.phase, 'moving');
      assert.equal(position.floor, null);
      assert.equal(position.fromFloor, 7);
      assert.equal(position.nextFloor, 5);
      assert.equal(position.cycleIndex, missedCycles);
      const arrival = nextArrival(profile, live.anchor, at(elapsed), 7, live.cycle, live.anchorKind);
      assert.equal(arrival.isHere, false);
      approximately(arrival.seconds, cycle - dwell - phase);
      approximately(arrival.arrivalMs, at((missedCycles + 1) * cycle - dwell));
      assert.deepEqual(snapshot, before);
    }
  }
});

test('two-hour detection expiry suppresses forecasting even before the next API poll', () => {
  for (const elapsed of [7199.999, 7200, 7200.001, 7500]) {
    const snapshot = Object.freeze(departureSnapshot({
      lastSeenAt: new Date(at(elapsed)).toISOString(),
    }));
    const live = liveTiming(snapshot, at(elapsed), connected);
    assert.equal(live.connected, true);
    assert.equal(live.usable, elapsed < 7200);
    assert.equal(live.anchor, elapsed <= 7200 ? anchorMs : null);
    assert.equal(live.cycle, 558);
  }
  const recovered = liveTiming(departureSnapshot({
    lastDepartureAt: new Date(at(7500)).toISOString(),
    lastSeenAt: new Date(at(7500)).toISOString(),
  }), at(7501), connected);
  assert.equal(recovered.usable, true);
  assert.equal(recovered.anchor, at(7500));
});

test('observations older than two hours are ignored online and in cached offline snapshots', () => {
  for (const reachable of [true, false]) {
    for (const elapsed of [7199.999, 7200, 7200.001, 86400]) {
      for (const anchorKind of ['departure', 'arrival']) {
        const snapshot = Object.freeze(departureSnapshot({
          anchorKind: anchorKind === 'arrival' ? undefined : anchorKind,
          lastArrivalAt: anchorKind === 'arrival' ? new Date(anchorMs).toISOString() : null,
          lastSeenAt: new Date(at(elapsed)).toISOString(),
        }));
        const live = liveTiming(snapshot, at(elapsed), { ...connected, reachable });
        assert.equal(live.anchor, elapsed <= 7200 ? anchorMs : null);
        assert.equal(live.usable, elapsed < 7200);
        assert.equal(live.connected, reachable);
        assert.equal(live.cycle, 558);
        assert.equal(live.seen, at(elapsed));
        assert.equal(snapshot.lastDepartureAt, new Date(anchorMs).toISOString());
      }
    }
  }
  const cached = Object.freeze(departureSnapshot());
  const expired = liveTiming(cached, at(7201), { ...connected, reachable: false });
  assert.equal(expired.anchor, null);
  assert.equal(expired.seen, at(1));
  const recovered = liveTiming(departureSnapshot({
    lastDepartureAt: new Date(at(7201)).toISOString(),
    lastSeenAt: new Date(at(7201)).toISOString(),
  }), at(7202), connected);
  assert.equal(recovered.usable, true);
  assert.equal(recovered.anchor, at(7201));
});

test('connection loss and resynchronization preserve forecasting from the confirmed anchor', () => {
  const snapshot = departureSnapshot();
  const unreachable = liveTiming(snapshot, at(2), { ...connected, reachable: false });
  assert.equal(unreachable.usable, true);
  assert.equal(unreachable.connected, false);
  assert.equal(unreachable.anchor, anchorMs);
  for (const options of [connected, { reachable: true }]) {
    for (const age of [90, 179.999, 180]) {
      const fresh = liveTiming(snapshot, at(1 + age), options);
      assert.equal(fresh.connected, true);
      assert.equal(fresh.usable, true);
      assert.equal(fresh.anchor, anchorMs);
    }
    const stale = liveTiming(snapshot, at(181.001), options);
    assert.equal(stale.connected, false);
    assert.equal(stale.usable, true);
  }
  for (const measurementStatus of ['stale', 'uncertain']) {
    const recovering = liveTiming(departureSnapshot({
      measurementStatus, lastSeenAt: new Date(at(1140)).toISOString(),
    }), at(1140), connected);
    assert.equal(recovering.connected, true);
    assert.equal(recovering.usable, true);
    assert.equal(recovering.anchor, anchorMs);
  }
  assert.equal(snapshot.lastDepartureAt, new Date(anchorMs).toISOString());
});

test('microphone failure, reconnection and API outage never reset the anchor or its deadline', () => {
  const samples = [
    { elapsed: 200, reachable: true, sourceConnected: false, lastSeenAt: null, measurementStatus: 'stale' },
    { elapsed: 500, reachable: true, sourceConnected: true, lastSeenAt: new Date(at(500)).toISOString(), measurementStatus: 'uncertain' },
    { elapsed: 7199, reachable: false, sourceConnected: true, lastSeenAt: new Date(at(500)).toISOString(), measurementStatus: 'uncertain' },
    { elapsed: 7200, reachable: false, sourceConnected: true, lastSeenAt: new Date(at(500)).toISOString(), measurementStatus: 'uncertain' },
    { elapsed: 7201, reachable: true, sourceConnected: true, lastSeenAt: new Date(at(7201)).toISOString(), measurementStatus: 'uncertain' },
  ];
  for (const { elapsed, reachable, ...fields } of samples) {
    const snapshot = Object.freeze(departureSnapshot(fields));
    const live = liveTiming(snapshot, at(elapsed), { ...connected, reachable });
    assert.equal(live.usable, elapsed < 7200);
    assert.equal(live.anchor, elapsed <= 7200 ? anchorMs : null);
    assert.equal(live.connected, elapsed === 500 || elapsed === 7201);
    if (live.usable) {
      for (const floor of [7, 6, 0, -1]) {
        assert.deepEqual(nextArrival(profile, live.anchor, at(elapsed), floor, live.cycle, live.anchorKind),
          nextArrival(profile, anchorMs, at(elapsed), floor, 558, 'departure'));
      }
      const first = nextArrival(profile, live.anchor, at(elapsed), 6, live.cycle, live.anchorKind);
      const later = nextArrival(profile, live.anchor, at(elapsed + 1), 6, live.cycle, live.anchorKind);
      assert.ok(first.seconds > 1);
      approximately(later.seconds, first.seconds - 1);
    }
    assert.equal(snapshot.lastDepartureAt, new Date(anchorMs).toISOString());
  }
});

test('capture health does not determine whether an unexpired confirmed departure is usable', () => {
  for (const overrides of [
    { lastSeenAt: null, sourceConnected: false, measurementStatus: 'stale' },
    { lastSeenAt: null, sourceConnected: true, measurementStatus: 'uncertain' },
    { lastSeenAt: 'invalid' }, { lastSeenAt: new Date(at(210)).toISOString() },
    { lastSeenAt: new Date(at(-1)).toISOString() }, { sourceConnected: false },
  ]) {
    const live = liveTiming(departureSnapshot(overrides), at(200), connected);
    assert.equal(live.connected, false);
    assert.equal(live.usable, true);
    assert.equal(live.anchor, anchorMs);
  }
});

test('malformed live cycles and observations never produce a forecast', () => {
  for (const cycleSeconds of [null, undefined, 0, -1, 300, 1800, Infinity, '570', true]) {
    const live = liveTiming(departureSnapshot({ cycleSeconds }), at(2), connected);
    assert.equal(live.usable, false);
    assert.equal(live.cycle, null);
  }
  for (const lastDepartureAt of [null, '', 'invalid', 0, new Date(at(3)).toISOString(), '2027-01-01T12:00:00']) {
    const live = liveTiming(departureSnapshot({ lastDepartureAt, lastArrivalAt: new Date(anchorMs).toISOString() }), at(2), connected);
    assert.equal(live.usable, false);
    assert.equal(live.anchor, null);
  }
  for (const overrides of [
    { anchorKind: 'chime' }, { anchorKind: null, lastArrivalAt: new Date(anchorMs).toISOString() },
    { cycleSource: 'simulation' }, { cycleSource: null },
    { mode: 'simulation' }, { measurementStatus: 'waiting' },
    { measurementStatus: null }, { measurementStatus: 'unknown' },
  ]) {
    assert.equal(liveTiming(departureSnapshot(overrides), at(2), connected).usable, false);
  }
  assert.equal(liveTiming(null, at(2), connected).usable, false);
});

test('legacy arrival snapshots retain arrival semantics without changing departure data', () => {
  const snapshot = {
    mode: 'live', sourceConnected: true, lastArrivalAt: new Date(anchorMs).toISOString(),
    lastSeenAt: new Date(anchorMs).toISOString(), cycleSeconds: 570, measurementStatus: 'tracking',
  };
  const live = liveTiming(snapshot, anchorMs, connected);
  assert.equal(live.usable, true);
  assert.equal(live.anchorKind, 'arrival');
  assert.equal(live.cycleSource, null);
  assert.equal(estimateState(profile, live.anchor, anchorMs, live.cycle, live.anchorKind).floor, 7);
});

test('monitor-only capture cannot enable a forecast even with a fresh stored departure', () => {
  const live = liveTiming(departureSnapshot({ monitorOnly: true }), at(2), connected);
  assert.equal(live.connected, true);
  assert.equal(live.monitorOnly, true);
  assert.equal(live.usable, false);
  assert.equal(live.anchor, anchorMs);
  const waiting = liveTiming(departureSnapshot({ monitorOnly: true, lastDepartureAt: null }), at(2), connected);
  assert.equal(waiting.usable, false);
  assert.equal(waiting.anchor, null);
});
