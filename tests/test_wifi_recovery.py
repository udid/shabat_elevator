import copy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy import wifi_recovery as w


HEALTHY = w.Probe("healthy", "local connection ready")
OUTAGE = w.Probe("outage", "disconnected")
UNKNOWN = w.Probe("unknown", "timeout")
SKIPPED = w.Probe("skipped", "radio disabled")
BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"


class Harness:
    def __init__(self, state=None):
        self.now = 0
        self.state = state or w.State()
        self.result = OUTAGE
        self.post_result = OUTAGE
        self.reconnects = []
        self.reboots = []
        self.saved = []
        self.fail_save = False
        self.timeout = False
        self.engine = w.Watchdog(boot_id=BOOT_A, clock=lambda: self.now,
                                 probe=lambda: self.result, reconnect=self.reconnect,
                                 reboot=self.reboot, save=self.save, logger=lambda _: None)

    def save(self, state):
        if self.fail_save and state.reboot_latched:
            raise OSError("disk unavailable")
        self.saved.append(copy.deepcopy(state))

    def reconnect(self):
        self.reconnects.append(self.now)
        self.result = self.post_result
        if self.timeout:
            raise subprocess.TimeoutExpired("nmcli", 35)
        return subprocess.CompletedProcess([], 1)

    def reboot(self):
        assert self.saved[-1].reboot_latched
        self.reboots.append(self.now)
        return subprocess.CompletedProcess([], 0)

    def tick(self, now, result=OUTAGE):
        self.now, self.result = now, result
        self.engine.tick(self.state)

    def through(self, end, result=OUTAGE, start=0):
        for now in range(start, end + 1, 30):
            self.tick(now, result)


class PolicyTests(unittest.TestCase):
    def test_thresholds_cooldown_and_one_reboot_per_outage(self):
        h = Harness()
        h.through(570)
        self.assertEqual(h.reconnects, [120, 240, 360, 480])
        self.assertFalse(h.reboots)
        h.tick(600)
        self.assertEqual(h.reboots, [600])
        h.through(1200, start=630)
        self.assertEqual(h.reboots, [600])
        self.assertTrue(all(b - a >= 120 for a, b in zip(h.reconnects, h.reconnects[1:])))

    def test_post_reconnect_health_prevents_reboot(self):
        h = Harness()
        h.through(570)
        h.post_result = HEALTHY
        h.tick(600)
        self.assertFalse(h.reboots)
        self.assertIsNone(h.state.outage_since)
        self.assertEqual(h.state.healthy_since, 600)

    def test_latch_survives_boot_and_requires_full_healthy_period(self):
        h = Harness()
        h.through(600)
        # Simulate serializing state and rebooting with a reset monotonic clock.
        h.state = w.State(**json.loads(json.dumps(asdict(h.saved[-1]))))
        h.engine.boot_id = BOOT_B
        h.through(600)
        self.assertEqual(h.reboots, [600])
        h.through(1200, HEALTHY, start=630)
        self.assertTrue(h.state.reboot_latched)
        h.tick(1230, HEALTHY)
        self.assertFalse(h.state.reboot_latched)
        h.through(1860, start=1260)
        self.assertEqual(h.reboots, [600, 1860])

    def test_unknown_disabled_and_observation_gap_reset_continuity_preserve_latch(self):
        for interruption in (UNKNOWN, SKIPPED):
            with self.subTest(interruption=interruption):
                h = Harness(w.State(reboot_latched=True))
                h.through(570, HEALTHY)
                h.tick(600, interruption)
                self.assertTrue(h.state.reboot_latched)
                self.assertIsNone(h.state.healthy_since)
                h.through(1200, HEALTHY, start=630)
                self.assertTrue(h.state.reboot_latched)
                h.tick(1230, HEALTHY)
                self.assertFalse(h.state.reboot_latched)
        h = Harness()
        h.through(570)
        h.tick(800)
        self.assertEqual(h.state.outage_since, 800)
        self.assertFalse(h.reboots)

    def test_unknown_post_reconnect_cannot_reboot_even_after_command_timeout(self):
        for timed_out in (False, True):
            h = Harness()
            h.through(570)
            h.post_result = UNKNOWN
            h.timeout = timed_out
            h.tick(600)
            self.assertFalse(h.reboots)
            self.assertIsNone(h.state.outage_since)

    def test_reconnect_timeout_requires_postprobe_before_decision(self):
        for post in (OUTAGE, HEALTHY, UNKNOWN, SKIPPED):
            with self.subTest(post=post.status):
                h = Harness()
                h.through(570)
                h.timeout, h.post_result = True, post
                h.tick(600)
                self.assertEqual(h.reboots, [600] if post.status == "outage" else [])
                if post.status != "outage":
                    self.assertIsNone(h.state.outage_since)

    def test_unknown_probe_interrupts_outage_before_recovery_threshold(self):
        h = Harness()
        h.through(90)
        h.tick(120, UNKNOWN)
        h.through(240, start=150)
        self.assertFalse(h.reconnects)
        h.tick(270)
        self.assertEqual(h.reconnects, [270])

    def test_failed_reboot_request_keeps_latch_and_does_not_retry(self):
        h = Harness()
        h.engine.reboot = lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired("systemctl", 10))
        h.through(600)
        self.assertTrue(h.saved[-1].reboot_latched)
        with patch.object(h.engine, "reboot") as reboot:
            h.through(1200, start=630)
            reboot.assert_not_called()

    def test_save_failure_prevents_reboot(self):
        h = Harness()
        h.through(570)
        h.fail_save = True
        with self.assertRaises(OSError):
            h.tick(600)
        self.assertFalse(h.reboots)

    def test_backward_monotonic_and_boot_change_reset_outage(self):
        h = Harness()
        h.through(570)
        h.tick(0)
        self.assertEqual(h.state.outage_since, 0)
        h.engine.boot_id = BOOT_B
        h.tick(30)
        self.assertEqual(h.state.outage_since, 30)
        self.assertFalse(h.reboots)

    def test_skipped_never_reconnects_or_reboots(self):
        h = Harness()
        h.through(1800, SKIPPED)
        self.assertFalse(h.reconnects)
        self.assertFalse(h.reboots)


class ProbeTests(unittest.TestCase):
    def probe(self, outputs, exists=True):
        self.calls = []
        def run(args):
            self.calls.append(args)
            value = outputs[len(self.calls) - 1]
            if isinstance(value, Exception):
                raise value
            return subprocess.CompletedProcess(args, 0, value, "")
        return w.probe_wifi("wlan0", run=run, interface_exists=lambda: exists)

    def outputs(self):
        return ["enabled:enabled\n", "100 (connected)\n",
                json.dumps([{"ifname": "wlan0", "addr_info": [
                    {"family": "inet", "local": "192.168.31.246", "prefixlen": 24}]}]),
                json.dumps([{"dst": "default", "gateway": "192.168.31.1", "dev": "wlan0"}])]

    def test_health_depends_only_on_local_interface_address_route(self):
        self.assertEqual(self.probe(self.outputs()).status, "healthy")
        self.assertEqual([args[0] for args in self.calls], ["nmcli", "nmcli", "ip", "ip"])
        self.assertFalse(any("ping" in args or "curl" in args for args in self.calls))

    def test_device_filtered_route_can_omit_dev_but_cannot_name_another_device(self):
        outputs = self.outputs()
        outputs[3] = json.dumps([{"dst": "default", "gateway": "192.168.31.1",
                                  "protocol": "dhcp", "prefsrc": "192.168.31.246",
                                  "metric": 600, "flags": []}])
        self.assertEqual(self.probe(outputs).status, "healthy")
        self.assertEqual(self.calls[-1], ["ip", "-j", "-4", "route", "show", "default", "dev", "wlan0"])
        outputs[3] = json.dumps([{"dst": "default", "gateway": "192.168.31.1", "dev": "eth0"}])
        self.assertEqual(self.probe(outputs).status, "unknown")

    def test_absent_disabled_and_unmanaged_are_skipped(self):
        self.assertEqual(self.probe([], exists=False).status, "skipped")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.probe(["enabled:disabled"]).status, "skipped")
        self.assertEqual(self.probe(["disabled:enabled"]).status, "skipped")
        self.assertEqual(self.probe(["enabled:enabled", "10 (unmanaged)"]).status, "skipped")

    def test_confirmed_disconnected_no_ipv4_or_default_route(self):
        self.assertEqual(self.probe(["enabled:enabled", "30 (disconnected)"]).status, "outage")
        outputs = self.outputs()
        outputs[2] = '[{"ifname":"wlan0","addr_info":[]}]'
        self.assertEqual(self.probe(outputs).status, "outage")
        outputs = self.outputs()
        outputs[3] = "[]"
        self.assertEqual(self.probe(outputs).status, "outage")

    def test_malformed_or_timed_out_probes_are_unknown(self):
        for index, value in ((0, "unknown:enabled"), (1, "100 broken"), (2, "{}"),
                             (2, '[{"ifname":"wlan0"}]'), (3, '[{"dst":"192.168.31.0/24"}]'),
                             (0, subprocess.TimeoutExpired("nmcli", 5))):
            with self.subTest(index=index, value=value):
                outputs = self.outputs()
                outputs[index] = value
                self.assertEqual(self.probe(outputs).status, "unknown")


class StorageTests(unittest.TestCase):
    def test_missing_is_initial_install_corruption_latches_reboot(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            self.assertFalse(w.load_state(path).reboot_latched)
            for data in ("{broken", "{}", "null", json.dumps({**asdict(w.State()), "outage_since": float("nan")})):
                path.write_text(data)
                self.assertTrue(w.load_state(path).reboot_latched)

    def test_atomic_save_roundtrip_and_fsync_precede_return(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            state = w.State(boot_id=BOOT_A, last_observed=600, outage_since=0,
                            last_attempt=600, reboot_latched=True)
            # Windows cannot open directories with os.open; keep real file writes
            # and replacement, and model just the POSIX directory descriptor.
            original_open = w.os.open
            def open_fd(target, flags, *args, **kwargs):
                if Path(target) == path.parent:
                    return 98765
                return original_open(target, flags, *args, **kwargs)
            original_close = w.os.close
            with patch.object(w.os, "open", side_effect=open_fd), \
                    patch.object(w.os, "fsync") as sync, \
                    patch.object(w.os, "close", side_effect=lambda fd: None if fd == 98765 else original_close(fd)):
                w.save_state(path, state)
                self.assertEqual(sync.call_count, 2)
                self.assertEqual(sync.call_args.args, (98765,))
            self.assertEqual(w.load_state(path), state)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["state.json"])

    def test_check_only_does_not_create_state_or_execute_actions(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "must-not-exist"
            with patch.object(w, "probe_wifi", return_value=HEALTHY):
                self.assertEqual(w.main(["--check-only", "--state-dir", str(path)]), 0)
            self.assertFalse(path.exists())

    def test_locked_invocation_skips_before_probe_or_recovery(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            flock = SimpleNamespace(LOCK_EX=2, LOCK_NB=4,
                                    flock=lambda *args: (_ for _ in ()).throw(BlockingIOError()))
            with patch.dict(sys.modules, {"fcntl": flock}), patch.object(w, "probe_wifi") as probe:
                self.assertEqual(w.main(["--state-dir", directory]), 0)
                probe.assert_not_called()
            self.assertFalse((Path(directory) / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
