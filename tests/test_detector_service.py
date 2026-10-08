import threading
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from elevator.detector_service import DetectorSupervisor
from elevator.state import ObservationStore, utc_now


class DetectorServiceTests(unittest.TestCase):
    def test_capture_callbacks_publish_real_departure_and_bootstrap_cycle(self):
        store = ObservationStore(default_cycle_seconds=560)
        ready = threading.Event()

        def listener(config, event, heartbeat, stop, **kwargs):
            heartbeat(utc_now())
            event(utc_now())
            ready.set()
            stop.wait(2)

        service = DetectorSupervisor(store, {"floor": 7}, listener=listener)
        try:
            service.start()
            self.assertTrue(ready.wait(2))
            state = store.snapshot()
            self.assertEqual(state["anchorKind"], "departure")
            self.assertIsNotNone(state["lastDepartureAt"])
            self.assertIsNone(state["lastArrivalAt"])
            self.assertEqual(state["cycleSeconds"], 560)
            self.assertEqual(state["cycleSource"], "default")
            self.assertTrue(state["sourceConnected"])
        finally:
            service.stop()
        self.assertFalse(service.thread.is_alive())
        self.assertFalse(store.snapshot()["sourceConnected"])

    def test_capture_failure_clears_connectivity_and_retries_without_fake_heartbeats(self):
        store = Mock(device_id="begin17-floor7")
        ready = threading.Event()
        calls = []

        def listener(config, event, heartbeat, stop, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise OSError("USB disconnected")
            ready.set()
            stop.wait(2)

        service = DetectorSupervisor(store, {"floor": 7}, device="USB mic",
                                     listener=listener, retry_seconds=.01)
        with patch("elevator.detector_service.LOG"):
            try:
                service.start()
                self.assertTrue(ready.wait(2))
                store.sensor_unavailable.assert_called_once()
                store.heartbeat.assert_not_called()
                store.observe.assert_not_called()
                self.assertEqual(calls[1]["device"], "USB mic")
            finally:
                service.stop()
        self.assertFalse(service.thread.is_alive())

    def test_stop_interrupts_retry_wait(self):
        failed = threading.Event()
        store = Mock(device_id="begin17-floor7")

        def listener(*args, **kwargs):
            failed.set()
            raise OSError("No input device")

        service = DetectorSupervisor(store, {}, listener=listener, retry_seconds=60)
        with patch("elevator.detector_service.LOG"):
            service.start()
            self.assertTrue(failed.wait(2))
            service.stop()
        self.assertFalse(service.thread.is_alive())

    def test_monitor_mode_tests_audio_without_inventing_elevator_observations(self):
        store = ObservationStore(default_cycle_seconds=560)
        ready = threading.Event()

        def listener(config, event, heartbeat, stop, **kwargs):
            heartbeat(utc_now())
            event(utc_now())
            ready.set()
            stop.wait(2)

        service = DetectorSupervisor(store, {"floor": 7}, listener=listener, monitor_only=True)
        try:
            service.start()
            self.assertTrue(ready.wait(2))
            state = store.snapshot()
            self.assertTrue(state["sourceConnected"])
            self.assertIsNone(state["lastDepartureAt"])
            self.assertIsNone(state["cycleSeconds"])
            self.assertEqual(state["measurementStatus"], "waiting")
        finally:
            service.stop()

    def test_diagnostics_record_cycle_decisions_and_throttle_health(self):
        now = utc_now()
        store = ObservationStore(default_cycle_seconds=560, clock=lambda: now)
        diagnostics = Mock()
        diagnostics.status.return_value = {"recording": True}
        service = DetectorSupervisor(store, {"floor": 7}, diagnostics=diagnostics)
        with patch("elevator.detector_service.time.monotonic", side_effect=[10, 15, 75]):
            service._heartbeat(now)
            service._observe(now)
            now += timedelta(seconds=5)
            service._heartbeat(now)
            service._observe(now)
            for _ in range(11):
                now += timedelta(seconds=50)
                store.heartbeat({"deviceId": store.device_id, "observedAt": now.isoformat()})
            now += timedelta(seconds=5)
            service._heartbeat(now)
            service._observe(now)
        events = diagnostics.event.call_args_list
        decisions = [call.kwargs for call in events if call.args[0] == "observation_decision"]
        self.assertEqual(len(decisions), 3)
        self.assertIsNone(decisions[0]["intervalSeconds"])
        self.assertEqual(decisions[1]["result"], {"accepted": False, "reason": "same_stop"})
        self.assertEqual(decisions[2]["intervalSeconds"], 560)
        self.assertEqual(decisions[2]["state"]["latestCycleSeconds"], 560)
        self.assertEqual(decisions[2]["state"]["cycleSource"], "measured")
        self.assertEqual(len([c for c in events if c.args[0] == "detector_health"]), 2)

    def test_broken_diagnostic_sink_does_not_interrupt_reporting(self):
        store = ObservationStore(default_cycle_seconds=560)
        diagnostics = Mock()
        diagnostics.event.side_effect = OSError("Disk unavailable")
        diagnostics.status.side_effect = OSError("Disk unavailable")
        service = DetectorSupervisor(store, {"floor": 7}, diagnostics=diagnostics)
        with patch("elevator.detector_service.LOG"):
            service._heartbeat(utc_now())
            service._observe(utc_now())
        self.assertTrue(store.snapshot()["sourceConnected"])
        self.assertIsNotNone(store.snapshot()["lastDepartureAt"])


if __name__ == "__main__":
    unittest.main()
