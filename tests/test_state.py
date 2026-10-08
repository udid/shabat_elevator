import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from elevator.state import ObservationStore, iso


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        self.store = self.new_store()

    def new_store(self, path=None, **options):
        return ObservationStore(path, clock=lambda: self.now,
                                **{"default_cycle_seconds": 558, **options})

    def event(self, event_id="one", **extra):
        return {"deviceId": "begin17-floor7", "floor": 7, "eventKind": "departure",
                "eventId": event_id, "observedAt": iso(self.now), **extra}

    def advance_connected(self, seconds):
        target = self.now + timedelta(seconds=seconds)
        while self.now < target:
            self.now = min(self.now + timedelta(seconds=30), target)
            self.store.heartbeat(self.event())

    def tracking(self):
        self.assertFalse(self.store.observe(self.event())["accepted"])
        self.advance_connected(558)
        self.assertTrue(self.store.observe(self.event("two"))["accepted"])

    def test_configuration_reported_before_first_sound_without_enabling_forecast(self):
        state = self.store.snapshot()
        self.assertEqual((state["cycleSeconds"], state["cycleSource"]), (558, "configured"))
        self.assertIsNone(state["latestCycleSeconds"])
        self.assertIsNone(state["lastDetectionAt"])
        self.assertEqual(state["measurementStatus"], "waiting")
        self.assertFalse(state["sourceConnected"])

    def test_first_sound_is_only_a_candidate_and_second_sound_establishes_anchor(self):
        self.assertEqual(self.store.observe(self.event()), {"accepted": False, "reason": "awaiting_cycle_match"})
        state = self.store.snapshot()
        self.assertTrue(state["sourceConnected"])
        self.assertIsNone(state["lastDepartureAt"])
        self.assertEqual(state["measurementStatus"], "waiting")
        self.advance_connected(558)
        self.assertTrue(self.store.observe(self.event("two"))["accepted"])
        self.assertEqual(self.store.snapshot()["lastDetectionAt"], iso(self.now))
        self.assertEqual(self.store.snapshot()["measurementStatus"], "tracking")

    def test_no_configuration_never_learns_a_cycle_from_events_or_client_hint(self):
        self.store = self.new_store(default_cycle_seconds=None)
        for event_id in ("one", "two", "three"):
            self.assertEqual(self.store.observe(self.event(event_id, cycleSeconds=558))["reason"], "missing_cycle_config")
            self.advance_connected(558)
        state = self.store.snapshot()
        self.assertIsNone(state["cycleSeconds"])
        self.assertIsNone(state["cycleSource"])
        self.assertIsNone(state["lastDepartureAt"])
        self.assertEqual(state["measurementStatus"], "waiting")

    def test_fixed_cycle_never_changes_with_jitter_two_laps_or_client_hint(self):
        self.store.observe(self.event(cycleSeconds=900))
        for index, seconds in enumerate((600, 580, 620, 590, 610, 640, 1116)):
            self.advance_connected(seconds)
            self.assertTrue(self.store.observe(self.event(f"lap-{index}", cycleSeconds=900))["accepted"])
            state = self.store.snapshot()
            self.assertEqual((state["cycleSeconds"], state["cycleSource"]), (558, "configured"))
            self.assertIsNone(state["latestCycleSeconds"])

    def test_single_and_double_cycle_boundaries_are_inclusive(self):
        cases = ((474.299999, False), (474.3, True), (641.7, True), (641.700001, False),
                 (948.599999, False), (948.6, True), (1283.4, True), (1283.400001, False))
        for seconds, accepted in cases:
            with self.subTest(seconds=seconds):
                self.store = self.new_store()
                self.store.observe(self.event())
                self.advance_connected(seconds)
                self.assertEqual(self.store.observe(self.event("two"))["accepted"], accepted)
                self.assertEqual(self.store.snapshot()["cycleSeconds"], 558)

    def test_custom_and_zero_tolerance_apply_to_both_windows(self):
        cases = ((5, 500, False), (5, 550, True), (5, 1000, False), (5, 1100, True),
                 (0, 557.999999, False), (0, 558, True), (0, 1116, True), (0, 1116.000001, False))
        for tolerance, seconds, accepted in cases:
            with self.subTest(tolerance=tolerance, seconds=seconds):
                self.store = self.new_store(cycle_tolerance_percent=tolerance)
                self.store.observe(self.event())
                self.advance_connected(seconds)
                self.assertEqual(self.store.observe(self.event("two"))["accepted"], accepted)

    def test_any_previous_candidate_can_match_despite_intervening_sound(self):
        self.store.observe(self.event())
        self.advance_connected(240)
        self.assertFalse(self.store.observe(self.event("noise-between"))["accepted"])
        self.advance_connected(318)
        self.assertTrue(self.store.observe(self.event("real-second"))["accepted"])
        self.assertEqual(self.store.snapshot()["lastDepartureAt"], iso(self.now))

    def test_two_cycle_pair_bootstraps_after_one_missed_lap(self):
        self.store.observe(self.event())
        self.advance_connected(1116)
        self.assertTrue(self.store.observe(self.event("after-one-missed-lap"))["accepted"])
        self.assertEqual(self.store.snapshot()["cycleSeconds"], 558)

    def test_three_cycle_gap_needs_a_new_pair(self):
        self.store.observe(self.event())
        self.advance_connected(1674)
        self.assertEqual(self.store.observe(self.event("new-seed"))["reason"], "awaiting_cycle_match")
        self.assertIsNone(self.store.snapshot()["lastDetectionAt"])
        self.advance_connected(558)
        self.assertTrue(self.store.observe(self.event("new-pair"))["accepted"])

    def test_minimum_spacing_between_accepted_sounds_is_inclusive(self):
        for seconds, accepted in ((474.299999, False), (474.3, True)):
            with self.subTest(seconds=seconds):
                self.store = self.new_store()
                self.tracking()
                previous = self.store.snapshot()["lastDetectionAt"]
                self.advance_connected(seconds)
                result = self.store.observe(self.event("next"))
                self.assertEqual(result["accepted"], accepted)
                if not accepted:
                    self.assertEqual(result["reason"], "too_soon")
                    self.assertEqual(self.store.snapshot()["lastDetectionAt"], previous)

    def test_too_close_matching_candidate_remains_eligible_for_future_pair(self):
        self.tracking()  # candidates at 0 and 558; only 558 is approved
        previous = self.store.snapshot()["lastDetectionAt"]
        self.advance_connected(442)  # 1000 matches two cycles from 0, but is too close to 558
        self.assertEqual(self.store.observe(self.event("blocked"))["reason"], "too_soon")
        self.assertEqual(self.store.snapshot()["lastDetectionAt"], previous)
        self.advance_connected(475)  # 1475 can match only the unapproved candidate at 1000
        self.assertTrue(self.store.observe(self.event("uses-blocked"))["accepted"])
        self.assertEqual(self.store.snapshot()["cycleSeconds"], 558)

    def test_duplicate_pending_and_accepted_ids_cannot_supply_new_candidates(self):
        self.store.observe(self.event())
        self.advance_connected(558)
        self.assertEqual(self.store.observe(self.event())["reason"], "duplicate")
        self.assertIsNone(self.store.snapshot()["lastDepartureAt"])
        self.assertTrue(self.store.observe(self.event("two"))["accepted"])
        anchor = self.store.snapshot()["lastDetectionAt"]
        self.advance_connected(558)
        self.assertEqual(self.store.observe(self.event("two"))["reason"], "duplicate")
        self.assertEqual(self.store.snapshot()["lastDetectionAt"], anchor)

    def test_nearby_sound_does_not_move_approved_anchor(self):
        self.tracking()
        anchor = self.store.snapshot()["lastDetectionAt"]
        self.advance_connected(5)
        self.assertEqual(self.store.observe(self.event("same-stop"))["reason"], "same_stop")
        self.assertEqual(self.store.snapshot()["lastDetectionAt"], anchor)

    def test_forecast_continues_without_inventing_detections(self):
        self.tracking()
        anchor = self.store.snapshot()["lastDetectionAt"]
        for seconds in (558, 1, 558, 1116):
            self.advance_connected(seconds)
            state = self.store.snapshot()
            self.assertEqual(state["measurementStatus"], "tracking")
            self.assertEqual(state["lastDepartureAt"], anchor)
            self.assertEqual(state["lastDetectionAt"], anchor)
            self.assertEqual(state["cycleSeconds"], 558)
            self.assertEqual(state["anchorKind"], "departure")
            self.assertIsNone(state["lastArrivalAt"])
            self.assertEqual(self.store.events, ["two"])

    def test_two_hour_expiry_requires_new_pair_not_heartbeat_or_single_candidate(self):
        self.tracking()
        original = self.store.snapshot()
        self.advance_connected(7199.999)
        self.assertEqual(self.store.snapshot()["measurementStatus"], "tracking")
        self.advance_connected(.001)
        self.assertEqual(self.store.snapshot()["measurementStatus"], "uncertain")
        self.assertTrue(self.store.snapshot()["sourceConnected"])
        self.assertEqual(self.store.observe(self.event("two"))["reason"], "duplicate")
        self.assertEqual(self.store.observe(self.event("after-two-hours"))["reason"], "awaiting_cycle_match")
        state = self.store.snapshot()
        self.assertEqual(state["measurementStatus"], "uncertain")
        self.assertEqual(state["lastDetectionAt"], original["lastDetectionAt"])
        self.assertEqual(state["cycleSeconds"], 558)
        self.advance_connected(558)
        self.assertTrue(self.store.observe(self.event("confirmed-again"))["accepted"])
        self.assertEqual(self.store.snapshot()["measurementStatus"], "tracking")
        self.assertEqual(self.store.snapshot()["lastDetectionAt"], iso(self.now))

    def test_delayed_heartbeat_cannot_resurrect_stale_source(self):
        self.tracking()
        self.now += timedelta(seconds=200)
        with self.assertRaisesRegex(ValueError, "too old"):
            self.store.heartbeat(self.event(observedAt=iso(self.now - timedelta(seconds=110))))
        self.assertFalse(self.store.snapshot()["sourceConnected"])

    def test_dropout_discards_candidates_even_without_a_snapshot(self):
        for recovery in ("heartbeat", "observation"):
            with self.subTest(recovery=recovery):
                self.store = self.new_store()
                self.tracking()
                anchor = self.store.snapshot()["lastDetectionAt"]
                self.now += timedelta(seconds=558)  # No heartbeat: capture continuity was lost.
                if recovery == "heartbeat":
                    self.store.heartbeat(self.event())
                    self.assertEqual(self.store.snapshot()["measurementStatus"], "uncertain")
                self.assertEqual(self.store.observe(self.event("new-seed"))["reason"], "awaiting_cycle_match")
                self.assertEqual(self.store.snapshot()["lastDetectionAt"], anchor)
                self.assertEqual(self.store.snapshot()["measurementStatus"], "uncertain")
                self.advance_connected(558)
                self.assertTrue(self.store.observe(self.event("new-pair"))["accepted"])

    def test_explicit_sensor_failure_discards_even_recent_candidates(self):
        self.tracking()
        self.store.sensor_unavailable()
        self.assertEqual(self.store.snapshot()["measurementStatus"], "stale")
        self.store.heartbeat(self.event())
        self.assertEqual(self.store.snapshot()["measurementStatus"], "uncertain")
        self.advance_connected(558)
        self.assertFalse(self.store.observe(self.event("fresh-session"))["accepted"])
        self.advance_connected(558)
        self.assertTrue(self.store.observe(self.event("fresh-pair"))["accepted"])

    def test_pre_interruption_candidate_cannot_be_replayed_to_seed_recovery(self):
        for interruption in ("failure", "restart"):
            with self.subTest(interruption=interruption):
                self.store = self.new_store()
                original = self.event()
                self.store.observe(original)
                self.now += timedelta(seconds=1)
                if interruption == "failure":
                    self.store.sensor_unavailable()
                    self.assertEqual(self.store.observe(self.event())["reason"], "duplicate")
                else:
                    self.store = self.new_store()
                self.store.heartbeat(self.event())
                replay = {**original, "eventId": "retry-with-new-id"}
                self.assertEqual(self.store.observe(replay)["reason"], "previous_capture")
                self.advance_connected(557)
                self.assertFalse(self.store.observe(self.event("fresh-seed"))["accepted"])
                self.advance_connected(558)
                self.assertTrue(self.store.observe(self.event("fresh-pair"))["accepted"])

    def test_snapshot_remembers_dropout_when_heartbeat_returns(self):
        self.tracking()
        self.now += timedelta(seconds=100)
        self.assertEqual(self.store.snapshot()["measurementStatus"], "stale")
        self.store.heartbeat(self.event())
        self.assertEqual(self.store.snapshot()["measurementStatus"], "uncertain")

    def test_long_configured_cycle_allows_two_laps_above_1800_seconds(self):
        self.store = self.new_store(default_cycle_seconds=1200)
        self.store.observe(self.event())
        self.advance_connected(2400)
        self.assertTrue(self.store.observe(self.event("two-laps"))["accepted"])
        self.assertEqual(self.store.snapshot()["cycleSeconds"], 1200)

    def test_old_or_invalid_observations_cannot_establish_candidates(self):
        bad = [self.event(floor=-1), self.event(floor=True), self.event(deviceId="other"),
               self.event(eventId=""), self.event(eventKind=None), self.event(eventKind="arrival"),
               self.event(cycleSeconds=float("nan")), self.event(cycleSeconds=True),
               self.event(cycleSeconds=300), self.event(cycleSeconds=1800),
               self.event(observedAt="2026-10-09T15:00:00"),
               self.event(observedAt=iso(self.now + timedelta(seconds=30))),
               self.event(observedAt=iso(self.now - timedelta(seconds=300)))]
        missing_kind = self.event()
        del missing_kind["eventKind"]
        bad.append(missing_kind)
        for event in bad:
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.store.observe(event)
        self.assertFalse(self.store.snapshot()["sourceConnected"])
        self.advance_connected(558)
        self.assertFalse(self.store.observe(self.event("only-valid-sound"))["accepted"])
        self.assertIsNone(self.store.snapshot()["lastDepartureAt"])

    def test_small_clock_skew_never_creates_future_anchor(self):
        self.store.observe(self.event(observedAt=iso(self.now + timedelta(seconds=3))))
        self.advance_connected(558)
        self.store.observe(self.event("two", observedAt=iso(self.now + timedelta(seconds=3))))
        self.assertEqual(self.store.snapshot()["lastDepartureAt"], iso(self.now))

    def test_out_of_order_pending_or_approved_event_is_rejected(self):
        self.store.observe(self.event())
        self.advance_connected(20)
        self.store.observe(self.event("later-candidate"))
        with self.assertRaisesRegex(ValueError, "predates"):
            self.store.observe(self.event("late-candidate", observedAt=iso(self.now - timedelta(seconds=10))))
        self.advance_connected(538)
        self.store.observe(self.event("two"))
        with self.assertRaisesRegex(ValueError, "predates"):
            self.store.observe(self.event("late", observedAt=iso(self.now - timedelta(seconds=10))))

    def test_invalid_configuration_is_rejected(self):
        for value in (True, 300, 1800, float("nan"), float("inf"), "558", 10 ** 1000):
            with self.subTest(cycle=value), self.assertRaises(ValueError):
                self.new_store(default_cycle_seconds=value)
        for value in (True, None, -1, 100, float("nan"), float("inf"), "15", 10 ** 1000):
            with self.subTest(tolerance=value), self.assertRaises(ValueError):
                self.new_store(cycle_tolerance_percent=value)
        with self.assertRaises(ValueError):
            self.new_store(event_kind="arrival")

    def test_restart_preserves_anchor_but_requires_new_pair_and_current_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.store = self.new_store(path)
            self.tracking()
            saved = self.store.snapshot()
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("cycles", disk)
            self.assertNotIn("candidates", disk)
            self.store = self.new_store(path, default_cycle_seconds=600)
            self.assertEqual(self.store.snapshot()["lastDepartureAt"], saved["lastDepartureAt"])
            self.assertEqual(self.store.snapshot()["cycleSeconds"], 600)
            self.assertFalse(self.store.snapshot()["sourceConnected"])
            self.assertEqual(self.store.observe(self.event("two"))["reason"], "duplicate")
            self.store.heartbeat(self.event())
            self.assertEqual(self.store.snapshot()["measurementStatus"], "uncertain")
            self.advance_connected(600)
            self.assertFalse(self.store.observe(self.event("fresh-after-restart"))["accepted"])
            self.advance_connected(600)
            self.assertTrue(self.store.observe(self.event("fresh-pair"))["accepted"])
            self.assertEqual(self.store.snapshot()["measurementStatus"], "tracking")

    def test_legacy_measured_history_is_ignored_even_if_malformed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for old_cycles in ([700, 720, 730], "not-a-list", None):
                with self.subTest(old_cycles=old_cycles):
                    path.write_text(json.dumps({"schemaVersion": 2, "deviceId": "begin17-floor7",
                        "eventKind": "departure", "lastDepartureAt": iso(self.now - timedelta(days=1)),
                        "cycles": old_cycles, "events": ["old"]}), encoding="utf-8")
                    self.store = self.new_store(path)
                    self.store.heartbeat(self.event())
                    state = self.store.snapshot()
                    self.assertEqual((state["cycleSeconds"], state["cycleSource"]), (558, "configured"))
                    self.assertEqual(state["measurementStatus"], "uncertain")
                    self.assertIsNone(state["latestCycleSeconds"])
                    self.assertFalse(self.store.observe(self.event("first-today"))["accepted"])
                    self.advance_connected(558)
                    self.assertTrue(self.store.observe(self.event("second-today"))["accepted"])

    def test_legacy_arrival_history_is_not_relabelled_as_departure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"deviceId": "begin17-floor7", "lastArrivalAt": iso(self.now),
                                        "cycles": [570], "events": ["legacy"]}), encoding="utf-8")
            restored = self.new_store(path)
            restored.heartbeat(self.event())
            self.assertIsNone(restored.snapshot()["lastDepartureAt"])
            self.assertEqual(restored.snapshot()["cycleSeconds"], 558)
            self.assertEqual(restored.snapshot()["measurementStatus"], "waiting")

    def test_corrupt_persistent_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            invalid_date = json.dumps({"deviceId": "begin17-floor7", "eventKind": "departure",
                                       "lastDepartureAt": "invalid"})
            for data in ("not-json", "null", invalid_date):
                with self.subTest(data=data):
                    path.write_text(data, encoding="utf-8")
                    restored = self.new_store(path)
                    self.assertIsNone(restored.snapshot()["lastDepartureAt"])


if __name__ == "__main__":
    unittest.main()
