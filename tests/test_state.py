import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from elevator.state import ObservationStore, iso


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        self.store = ObservationStore(clock=lambda: self.now)

    def event(self, event_id="one", **extra):
        return {"deviceId": "begin17-floor7", "floor": 7, "eventKind": "departure",
                "eventId": event_id, "observedAt": iso(self.now), **extra}

    def advance_connected(self, seconds):
        remaining = seconds
        while remaining > 0:
            step = min(30, remaining)
            self.now += timedelta(seconds=step)
            self.store.heartbeat(self.event())
            remaining -= step

    def tracking(self):
        self.store.observe(self.event())
        self.advance_connected(570)
        self.store.observe(self.event("two"))

    def test_first_departure_without_default_waits_for_measured_cycle(self):
        self.store.observe(self.event())
        state = self.store.snapshot()
        self.assertIsNone(state["cycleSeconds"])
        self.assertIsNone(state["cycleSource"])
        self.assertEqual(state["measurementStatus"], "waiting")
        self.assertTrue(state["sourceConnected"])

    def test_default_hidden_until_departure_then_replaced_by_measured_cycle(self):
        self.store = ObservationStore(clock=lambda: self.now, default_cycle_seconds=560)
        self.store.heartbeat(self.event())
        self.assertIsNone(self.store.snapshot()["cycleSeconds"])
        self.store.observe(self.event())
        state = self.store.snapshot()
        self.assertEqual((state["cycleSeconds"], state["cycleSource"]), (560, "default"))
        self.assertEqual(state["measurementStatus"], "tracking")
        self.assertIsNone(state["latestCycleSeconds"])
        self.advance_connected(600)
        self.store.observe(self.event("two"))
        state = self.store.snapshot()
        self.assertEqual((state["cycleSeconds"], state["cycleSource"]), (600, "measured"))
        self.assertEqual(state["latestCycleSeconds"], 600)

    def test_two_departures_measure_cycle_and_reads_do_not_advance_anchor(self):
        self.tracking()
        anchor = self.store.snapshot()["lastDepartureAt"]
        self.now += timedelta(seconds=40)
        state = self.store.snapshot()
        self.assertEqual(state["cycleSeconds"], 570)
        self.assertEqual(state["lastDepartureAt"], anchor)
        self.assertEqual(state["lastDetectionAt"], anchor)
        self.assertEqual(state["anchorKind"], "departure")
        self.assertIsNone(state["lastArrivalAt"])
        self.assertEqual(state["measurementStatus"], "tracking")

    def test_unique_id_and_same_stop_are_not_new_departures(self):
        event = self.event()
        self.store.observe(event)
        original = self.store.snapshot()["lastDepartureAt"]
        self.now += timedelta(seconds=5)
        self.assertEqual(self.store.observe(event)["reason"], "duplicate")
        self.assertEqual(self.store.observe(self.event("another-sound"))["reason"], "same_stop")
        self.assertEqual(self.store.snapshot()["lastDepartureAt"], original)

    def test_missed_departure_preserves_cycle_and_requires_valid_interval(self):
        self.tracking()
        self.advance_connected(1140)
        self.store.observe(self.event("after-missed-stop"))
        state = self.store.snapshot()
        self.assertEqual(state["cycleSeconds"], 570)
        self.assertEqual(state["measurementStatus"], "uncertain")
        self.assertIsNotNone(state["message"])
        self.advance_connected(570)
        self.store.observe(self.event("recovered"))
        self.assertEqual(self.store.snapshot()["measurementStatus"], "tracking")
        self.assertIsNone(self.store.snapshot()["message"])

    def test_short_outlier_does_not_reanchor_predictions(self):
        self.tracking()
        anchor = self.store.snapshot()["lastDepartureAt"]
        self.advance_connected(350)
        self.assertEqual(self.store.observe(self.event("spurious"))["reason"], "too_soon")
        self.assertEqual(self.store.snapshot()["lastDepartureAt"], anchor)

    def test_delayed_heartbeat_cannot_resurrect_stale_source(self):
        self.tracking()
        self.now += timedelta(seconds=200)
        with self.assertRaises(ValueError):
            self.store.heartbeat(self.event(observedAt=iso(self.now - timedelta(seconds=110))))
        self.assertFalse(self.store.snapshot()["sourceConnected"])

    def test_heartbeat_after_dropout_never_restores_tracking_without_real_detection(self):
        self.tracking()
        anchor = self.store.snapshot()["lastDepartureAt"]
        self.now += timedelta(seconds=200)
        self.store.heartbeat(self.event())
        state = self.store.snapshot()
        self.assertTrue(state["sourceConnected"])
        self.assertEqual(state["measurementStatus"], "uncertain")
        self.assertEqual(state["lastDepartureAt"], anchor)
        self.advance_connected(500)
        self.store.observe(self.event("fresh-session"))
        self.assertEqual(self.store.snapshot()["measurementStatus"], "tracking")
        self.assertEqual(self.store.snapshot()["cycleSeconds"], 570)

    def test_snapshot_remembers_dropout_when_heartbeat_returns(self):
        self.tracking()
        self.now += timedelta(seconds=100)
        self.assertEqual(self.store.snapshot()["measurementStatus"], "stale")
        self.store.heartbeat(self.event())
        self.assertEqual(self.store.snapshot()["measurementStatus"], "uncertain")

    def test_explicit_sensor_failure_invalidates_confidence(self):
        self.tracking()
        self.store.sensor_unavailable()
        self.assertEqual(self.store.snapshot()["measurementStatus"], "stale")
        self.store.heartbeat(self.event())
        self.assertEqual(self.store.snapshot()["measurementStatus"], "uncertain")

    def test_prediction_expiry_never_invents_new_cycle(self):
        self.tracking()
        anchor = self.store.snapshot()["lastDepartureAt"]
        self.advance_connected(570)
        state = self.store.snapshot()
        self.assertEqual(state["measurementStatus"], "uncertain")
        self.assertEqual(state["lastDepartureAt"], anchor)

    def test_old_departure_is_rejected_and_cannot_establish_anchor(self):
        with self.assertRaisesRegex(ValueError, "too old"):
            self.store.observe(self.event(observedAt=iso(self.now - timedelta(seconds=300))))
        self.assertFalse(self.store.snapshot()["sourceConnected"])
        self.assertIsNone(self.store.snapshot()["lastDepartureAt"])

    def test_reported_cycle_does_not_claim_server_measured_interval(self):
        self.store.observe(self.event(cycleSeconds=570))
        state = self.store.snapshot()
        self.assertEqual(state["measurementStatus"], "waiting")
        self.assertIsNone(state["cycleSeconds"])

    def test_invalid_observations_do_not_modify_state(self):
        bad = [self.event(floor=-1), self.event(floor=True), self.event(deviceId="other"),
               self.event(eventId=""), self.event(eventKind=None), self.event(eventKind="arrival"),
               self.event(cycleSeconds=float("nan")), self.event(cycleSeconds=True),
               self.event(cycleSeconds=300), self.event(cycleSeconds=1800),
               self.event(observedAt="2026-10-09T15:00:00"),
               self.event(observedAt=iso(self.now + timedelta(seconds=30)))]
        missing_kind = self.event()
        del missing_kind["eventKind"]
        bad.append(missing_kind)
        for event in bad:
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.store.observe(event)
        self.assertIsNone(self.store.snapshot()["lastDepartureAt"])

    def test_small_clock_skew_never_creates_future_anchor(self):
        self.store.observe(self.event(observedAt=iso(self.now + timedelta(seconds=3))))
        self.assertEqual(self.store.snapshot()["lastDepartureAt"], iso(self.now))

    def test_out_of_order_event_rejected(self):
        self.tracking()
        with self.assertRaisesRegex(ValueError, "predates"):
            self.store.observe(self.event("late", observedAt=iso(self.now - timedelta(seconds=10))))

    def test_cycle_boundaries_are_strict(self):
        for seconds in (300, 300.1, 1799.9, 1800):
            with self.subTest(seconds=seconds):
                self.store = ObservationStore(clock=lambda: self.now, default_cycle_seconds=560)
                self.store.observe(self.event())
                self.advance_connected(seconds)
                result = self.store.observe(self.event("second"))
                state = self.store.snapshot()
                if seconds == 300:
                    self.assertFalse(result["accepted"])
                    self.assertEqual(state["cycleSource"], "default")
                elif seconds == 1800:
                    self.assertTrue(result["accepted"])
                    self.assertEqual(state["cycleSource"], "default")
                    self.assertEqual(state["measurementStatus"], "uncertain")
                else:
                    self.assertTrue(result["accepted"])
                    self.assertAlmostEqual(state["cycleSeconds"], seconds)
                    self.assertEqual(state["cycleSource"], "measured")

    def test_invalid_defaults_are_rejected(self):
        for value in (True, 300, 1800, float("nan"), float("inf"), "560"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ObservationStore(default_cycle_seconds=value)
        with self.assertRaises(ValueError):
            ObservationStore(event_kind="arrival")

    def test_restart_preserves_history_but_heartbeat_never_reactivates_old_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.store = ObservationStore(path, clock=lambda: self.now)
            self.tracking()
            saved = self.store.snapshot()
            restored = ObservationStore(path, clock=lambda: self.now)
            self.assertEqual(restored.snapshot()["lastDepartureAt"], saved["lastDepartureAt"])
            self.assertEqual(restored.snapshot()["cycleSeconds"], 570)
            self.assertFalse(restored.snapshot()["sourceConnected"])
            self.assertFalse(restored.observe(self.event("two"))["accepted"])
            restored.heartbeat(self.event())
            self.assertEqual(restored.snapshot()["measurementStatus"], "uncertain")
            self.store = restored
            self.advance_connected(600)
            self.store.observe(self.event("fresh-after-restart"))
            self.assertEqual(self.store.snapshot()["measurementStatus"], "tracking")
            self.assertEqual(self.store.snapshot()["cycleSeconds"], 570)

    def test_legacy_arrival_history_is_not_relabelled_as_departure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"deviceId": "begin17-floor7", "lastArrivalAt": iso(self.now),
                                        "cycles": [570], "events": ["legacy"]}), encoding="utf-8")
            restored = ObservationStore(path, clock=lambda: self.now, default_cycle_seconds=560)
            restored.heartbeat(self.event())
            self.assertIsNone(restored.snapshot()["lastDepartureAt"])
            self.assertIsNone(restored.snapshot()["cycleSeconds"])
            self.assertEqual(restored.snapshot()["measurementStatus"], "waiting")

    def test_corrupt_persistent_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            invalid_date = json.dumps({"deviceId": "begin17-floor7", "eventKind": "departure",
                                       "lastDepartureAt": "invalid"})
            for data in ("not-json", "null", invalid_date):
                with self.subTest(data=data):
                    path.write_text(data, encoding="utf-8")
                    restored = ObservationStore(path, clock=lambda: self.now)
                    self.assertIsNone(restored.snapshot()["lastDepartureAt"])


if __name__ == "__main__":
    unittest.main()
