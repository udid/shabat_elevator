import test from 'node:test';
import assert from 'node:assert/strict';
import { buildRoute, estimateState, nextArrival, DEFAULT_ROUTE } from '../web/model.js';

const profile = buildRoute();
const anchorMs = 1_800_000_000_000;
const at = seconds => anchorMs + seconds * 1000;
const approximately = (actual, expected, epsilon = 0.001) => assert.ok(Math.abs(actual - expected) < epsilon, `${actual} ≈ ${expected}`);

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

test('measured cycle override stretches the full profile consistently', () => {
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
});
