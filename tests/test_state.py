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
        return {"deviceId": "begin17-floor7", "floor": 7, "eventId": event_id,
                "observedAt": iso(self.now), **extra}

    def tracking(self):
        self.store.observe(self.event())
        self.now += timedelta(seconds=570)
        self.store.observe(self.event("two"))

    def test_first_arrival_waits_for_calibration(self):
        self.store.observe(self.event())
        state = self.store.snapshot()
        self.assertIsNone(state["cycleSeconds"])
        self.assertEqual(state["measurementStatus"], "waiting")
        self.assertTrue(state["sourceConnected"])

    def test_two_arrivals_measure_cycle_and_do_not_advance_on_read(self):
        self.tracking()
        anchor = self.store.snapshot()["lastArrivalAt"]
        self.now += timedelta(seconds=40)
        state = self.store.snapshot()
        self.assertEqual(state["cycleSeconds"], 570)
        self.assertEqual(state["lastArrivalAt"], anchor)
        self.assertEqual(state["measurementStatus"], "tracking")

    def test_unique_id_and_same_stop_are_not_new_arrivals(self):
        event = self.event()
        self.store.observe(event)
        original = self.store.snapshot()["lastArrivalAt"]
        self.now += timedelta(seconds=5)
        self.assertEqual(self.store.observe(event)["reason"], "duplicate")
        self.assertEqual(self.store.observe(self.event("another-sound"))["reason"], "same_stop")
        self.assertEqual(self.store.snapshot()["lastArrivalAt"], original)

    def test_missed_arrival_does_not_double_learned_cycle(self):
        self.tracking()
        self.now += timedelta(seconds=1140)
        self.store.observe(self.event("after-missed-stop"))
        self.assertEqual(self.store.snapshot()["cycleSeconds"], 570)
        self.assertIsNotNone(self.store.snapshot()["message"])

    def test_short_outlier_does_not_reanchor_predictions(self):
        self.tracking()
        anchor = self.store.snapshot()["lastArrivalAt"]
        self.now += timedelta(seconds=150)
        self.assertEqual(self.store.observe(self.event("spurious"))["reason"], "too_soon")
        self.assertEqual(self.store.snapshot()["lastArrivalAt"], anchor)

    def test_delayed_heartbeat_cannot_resurrect_stale_source(self):
        self.tracking()
        self.now += timedelta(seconds=200)
        with self.assertRaises(ValueError):
            self.store.heartbeat(self.event(observedAt=iso(self.now - timedelta(seconds=110))))
        self.assertFalse(self.store.snapshot()["sourceConnected"])

    def test_heartbeat_proves_connectivity_not_an_arrival(self):
        self.tracking()
        anchor = self.store.snapshot()["lastArrivalAt"]
        self.now += timedelta(seconds=200)
        self.assertEqual(self.store.snapshot()["measurementStatus"], "stale")
        self.store.heartbeat(self.event())
        state = self.store.snapshot()
        self.assertEqual(state["measurementStatus"], "tracking")
        self.assertEqual(state["lastArrivalAt"], anchor)

    def test_prediction_expiry_never_invents_new_cycle(self):
        self.tracking()
        anchor = self.store.snapshot()["lastArrivalAt"]
        self.now += timedelta(seconds=570)
        self.store.heartbeat(self.event())
        state = self.store.snapshot()
        self.assertEqual(state["measurementStatus"], "uncertain")
        self.assertEqual(state["lastArrivalAt"], anchor)

    def test_old_arrival_does_not_prove_connected(self):
        old = self.now - timedelta(seconds=300)
        self.store.observe(self.event(observedAt=iso(old), cycleSeconds=570))
        self.assertFalse(self.store.snapshot()["sourceConnected"])

    def test_reported_cycle_can_initialize_device_with_existing_history(self):
        self.store.observe(self.event(cycleSeconds=570))
        self.assertEqual(self.store.snapshot()["measurementStatus"], "tracking")

    def test_invalid_observations_do_not_modify_state(self):
        bad = [self.event(floor=-1), self.event(floor=True), self.event(deviceId="other"),
               self.event(eventId=""), self.event(cycleSeconds=float("nan")),
               self.event(cycleSeconds=True), self.event(observedAt="2026-10-09T15:00:00"),
               self.event(observedAt=iso(self.now + timedelta(seconds=30)))]
        for event in bad:
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.store.observe(event)
        self.assertIsNone(self.store.snapshot()["lastArrivalAt"])

    def test_out_of_order_event_rejected(self):
        self.tracking()
        with self.assertRaises(ValueError):
            self.store.observe(self.event("late", observedAt=iso(self.now - timedelta(seconds=300))))

    def test_restart_preserves_measurement_not_connectivity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.store = ObservationStore(path, clock=lambda: self.now)
            self.tracking()
            saved = self.store.snapshot()
            restored = ObservationStore(path, clock=lambda: self.now)
            self.assertEqual(restored.snapshot()["lastArrivalAt"], saved["lastArrivalAt"])
            self.assertEqual(restored.snapshot()["cycleSeconds"], 570)
            self.assertFalse(restored.snapshot()["sourceConnected"])
            self.assertFalse(restored.observe(self.event("two"))["accepted"])


if __name__ == "__main__":
    unittest.main()
