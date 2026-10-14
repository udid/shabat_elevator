#!/usr/bin/env python3
"""Local Wi-Fi recovery; no Internet or DNS probes and no wall-clock timers."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

RECOVER_AFTER = 120
RECOVER_INTERVAL = 120
REBOOT_AFTER = 600
HEALTHY_RESET_AFTER = 600
MAX_OBSERVATION_GAP = 180
DEFAULT_UUID = "35fad423-f46a-49e3-8f9f-32f4a0edaac1"


@dataclass(frozen=True)
class Probe:
    status: str  # healthy, outage, skipped, unknown
    reason: str


@dataclass
class State:
    version: int = 1
    boot_id: str = ""
    last_observed: float | None = None
    outage_since: float | None = None
    healthy_since: float | None = None
    last_attempt: float | None = None
    reboot_latched: bool = False

    def reset_timers(self):
        self.outage_since = self.healthy_since = self.last_attempt = None


def log(message):
    print(message, flush=True)


def command(args, timeout=5):
    return subprocess.run(args, timeout=timeout, capture_output=True, text=True,
                          env={**os.environ, "LC_ALL": "C", "LANG": "C"}, check=False)


def probe_wifi(interface, *, run=command, interface_exists=None):
    """Unknown data fails closed. Disabled or unmanaged Wi-Fi is left alone."""
    try:
        exists = interface_exists if interface_exists is not None else (
            lambda: (Path("/sys/class/net") / interface).is_dir())
        if not exists():
            return Probe("skipped", "interface absent")
        radio = run(["nmcli", "-t", "-f", "WIFI-HW,WIFI", "general"])
        if radio.returncode:
            return Probe("unknown", "cannot read Wi-Fi radio state")
        radios = radio.stdout.strip().split(":")
        if len(radios) != 2 or any(value not in ("enabled", "disabled") for value in radios):
            return Probe("unknown", "malformed Wi-Fi radio state")
        if "disabled" in radios:
            return Probe("skipped", "Wi-Fi radio deliberately disabled or hardware blocked")
        device = run(["nmcli", "-g", "GENERAL.STATE", "device", "show", interface])
        if device.returncode:
            return Probe("unknown", "cannot read NetworkManager device state")
        match = re.fullmatch(r"(\d+) \([^\r\n]+\)", device.stdout.strip())
        if not match or int(match[1]) not in range(10, 121, 10):
            return Probe("unknown", "malformed NetworkManager device state")
        state = int(match[1])
        if state == 10:
            return Probe("skipped", "interface unmanaged")
        if state != 100:
            return Probe("outage", f"NetworkManager device state {state}")
        addresses = run(["ip", "-j", "-4", "address", "show", "dev", interface])
        if addresses.returncode:
            return Probe("unknown", "cannot read interface IPv4 addresses")
        data = json.loads(addresses.stdout)
        if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
                or data[0].get("ifname") != interface or not isinstance(data[0].get("addr_info"), list)):
            return Probe("unknown", "malformed IPv4 address response")
        has_ipv4 = False
        for entry in data[0]["addr_info"]:
            if not isinstance(entry, dict) or entry.get("family") != "inet":
                return Probe("unknown", "malformed IPv4 address entry")
            if not isinstance(entry.get("local"), str):
                return Probe("unknown", "invalid IPv4 address")
            address = ipaddress.IPv4Address(entry["local"])
            prefix = entry["prefixlen"]
            if type(prefix) is not int or not 0 <= prefix <= 32:
                return Probe("unknown", "invalid IPv4 prefix")
            if not (address.is_loopback or address.is_unspecified or address.is_link_local):
                has_ipv4 = True
        if not has_ipv4:
            return Probe("outage", "connected without usable IPv4 address")
        routes = run(["ip", "-j", "-4", "route", "show", "default", "dev", interface])
        if routes.returncode:
            return Probe("unknown", "cannot read local default route")
        data = json.loads(routes.stdout)
        if not isinstance(data, list):
            return Probe("unknown", "malformed route response")
        for entry in data:
            # iproute2 can omit dev when the command already filters by device.
            # An explicit different device is still an invalid probe response.
            if (not isinstance(entry, dict) or entry.get("dst") != "default"
                    or entry.get("dev", interface) != interface):
                return Probe("unknown", "malformed default route entry")
            if "gateway" in entry:
                if not isinstance(entry["gateway"], str):
                    return Probe("unknown", "invalid route gateway")
                ipaddress.IPv4Address(entry["gateway"])
        if not data:
            return Probe("outage", "connected without local default route")
        return Probe("healthy", "connected with IPv4 and local default route")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return Probe("unknown", "local probe failed, timed out, or returned malformed data")


def load_state(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != set(State.__dataclass_fields__):
            raise ValueError("invalid state schema")
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise ValueError("unsupported state version")
        if not isinstance(raw["boot_id"], str) or type(raw["reboot_latched"]) is not bool:
            raise ValueError("invalid state identity")
        if raw["boot_id"]:
            uuid.UUID(raw["boot_id"])
        for name in ("last_observed", "outage_since", "healthy_since", "last_attempt"):
            value = raw[name]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError("invalid monotonic timer")
        if raw["outage_since"] is not None and raw["healthy_since"] is not None:
            raise ValueError("inconsistent continuity")
        for name in ("outage_since", "healthy_since", "last_attempt"):
            if raw[name] is not None and (raw["last_observed"] is None or raw[name] > raw["last_observed"]):
                raise ValueError("inconsistent monotonic timer")
        return State(**raw)
    except FileNotFoundError:
        return State()
    except (OSError, ValueError, TypeError):
        log("State unreadable or corrupt; reboot disabled until 600 seconds of healthy Wi-Fi")
        return State(reboot_latched=True)


def save_state(path, state):
    """Persist the latch before any reboot, including fsync of the directory."""
    payload = (json.dumps(asdict(state), allow_nan=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Watchdog:
    def __init__(self, *, boot_id, clock, probe, reconnect, reboot, save, logger=log):
        self.boot_id, self.clock, self.probe = boot_id, clock, probe
        self.reconnect, self.reboot, self.save, self.log = reconnect, reboot, save, logger

    def observe(self, state, result, now):
        if (state.boot_id != self.boot_id or state.last_observed is None
                or not 0 <= now - state.last_observed <= MAX_OBSERVATION_GAP):
            state.reset_timers()
        state.boot_id, state.last_observed = self.boot_id, now
        if result.status == "healthy":
            state.outage_since = state.last_attempt = None
            if state.healthy_since is None:
                state.healthy_since = now
            if state.reboot_latched and now - state.healthy_since >= HEALTHY_RESET_AFTER:
                state.reboot_latched = False
                self.log("Reboot latch reset after 600 seconds of healthy local Wi-Fi")
        elif result.status == "outage":
            state.healthy_since = None
            if state.outage_since is None:
                state.outage_since = now
        else:
            state.reset_timers()
            self.log(f"Recovery skipped ({result.status}): {result.reason}")

    def tick(self, state):
        result = self.probe()
        now = self.clock()
        self.observe(state, result, now)
        if (result.status != "outage" or now - state.outage_since < RECOVER_AFTER
                or (state.last_attempt is not None and now - state.last_attempt < RECOVER_INTERVAL)):
            self.save(state)
            return
        state.last_attempt = now
        self.save(state)  # A crash cannot cause immediate repeated reconnects.
        self.log(f"Local Wi-Fi outage for {now - state.outage_since:.0f}s; reconnecting configured profile")
        try:
            outcome = self.reconnect()
        except (OSError, subprocess.SubprocessError):
            outcome = None
            self.log("Reconnect command failed or timed out; checking actual local Wi-Fi state")
        # A successful command is not proof of usable Wi-Fi; always probe again.
        result = self.probe()
        now = self.clock()
        self.observe(state, result, now)
        if result.status == "healthy":
            self.log("Local Wi-Fi recovered after reconnect")
        elif result.status == "outage":
            outcome_text = f"exit {outcome.returncode}" if outcome is not None else "command failed or timed out"
            self.log(f"Reconnect attempt ({outcome_text}); local Wi-Fi still unavailable")
            if not state.reboot_latched and now - state.outage_since >= REBOOT_AFTER:
                state.reboot_latched = True
                self.save(state)  # Must succeed and be durable before reboot.
                self.log("Confirmed local outage for 600s; reboot latch saved; requesting orderly reboot")
                try:
                    reboot_result = self.reboot()
                    if reboot_result.returncode:
                        self.log("Reboot request failed; latch retained to prevent repeated attempts")
                except (OSError, subprocess.SubprocessError):
                    self.log("Reboot request failed or timed out; latch retained")
                return
        self.save(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="wlan0")
    parser.add_argument("--connection-uuid", default=DEFAULT_UUID)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/shabat-wifi-recovery"))
    parser.add_argument("--check-only", action="store_true", help="Probe only; do not create state, reconnect, or reboot")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", args.interface):
        parser.error("invalid interface name")
    try:
        connection_uuid = str(uuid.UUID(args.connection_uuid))
    except ValueError:
        parser.error("invalid connection UUID")
    probe = lambda: probe_wifi(args.interface)
    if args.check_only:
        result = probe()
        log(json.dumps(asdict(result)))
        return 0 if result.status == "healthy" else 1
    import fcntl  # Linux runtime only; pure logic/tests also run on Windows.
    try:
        args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (args.state_dir / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("Another recovery check is running; skipping")
                return 0
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            uuid.UUID(boot_id)  # An invalid boot identity must never authorize actions.
            path = args.state_dir / "state.json"
            engine = Watchdog(boot_id=boot_id, clock=time.monotonic, probe=probe,
                              reconnect=lambda: command(["nmcli", "--wait", "30", "connection", "up", "uuid",
                                                         connection_uuid, "ifname", args.interface], timeout=35),
                              reboot=lambda: command(["systemctl", "reboot"], timeout=10),
                              save=lambda state: save_state(path, state))
            engine.tick(load_state(path))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        log(f"Recovery check failed safely: {type(error).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
