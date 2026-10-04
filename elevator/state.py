"""Validate floor-seven timing events. No microphone/audio handling is implemented here."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from threading import RLock


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("observedAt must be an ISO 8601 timestamp with a timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("observedAt must include a timezone")
    return result.astimezone(timezone.utc)


class ObservationStore:
    def __init__(self, path: Path | None = None, *, device_id="begin17-floor7", clock=utc_now,
                 stale_after=90, arrival_grace=60):
        self.path = path
        self.device_id = device_id
        self.clock = clock
        self.stale_after = stale_after
        self.arrival_grace = arrival_grace
        self.lock = RLock()
        self.last_arrival = None
        self.last_seen = None
        self.cycles: list[float] = []
        self.events: list[str] = []
        self.note = None
        self._load()

    def _load(self):
        if not self.path or not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("deviceId") != self.device_id:
                return
            self.last_arrival = parse_time(saved["lastArrivalAt"]) if saved.get("lastArrivalAt") else None
            self.cycles = [float(x) for x in saved.get("cycles", []) if 120 <= float(x) <= 3600][-5:]
            self.events = [x for x in saved.get("events", []) if isinstance(x, str)][-64:]
            self.last_seen = None  # Restarting a server never proves the source is connected.
        except (ValueError, TypeError, KeyError, OSError):
            self.last_arrival = None
            self.cycles = []
            self.events = []

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"deviceId": self.device_id, "lastArrivalAt": iso(self.last_arrival),
                                    "cycles": self.cycles, "events": self.events}), encoding="utf-8")
        temp.replace(self.path)

    def _validate(self, payload, max_age):
        if not isinstance(payload, dict) or payload.get("deviceId") != self.device_id:
            raise ValueError("Unknown deviceId")
        observed = parse_time(payload.get("observedAt"))
        now = self.clock()
        if observed > now + timedelta(seconds=5):
            raise ValueError("Device timestamp is in the future; synchronize the device clock")
        if observed < now - timedelta(seconds=max_age):
            raise ValueError("Observation is too old")
        return observed, now

    def heartbeat(self, payload):
        with self.lock:
            observed, now = self._validate(payload, self.stale_after)
            seen = min(observed, now)
            self.last_seen = max(self.last_seen or seen, seen)
            return {"accepted": True}

    def observe(self, payload):
        with self.lock:
            observed, now = self._validate(payload, 7200)
            if type(payload.get("floor")) is not int or payload["floor"] != 7:
                raise ValueError("This source must report a stop at floor 7")
            event_id = payload.get("eventId")
            if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
                raise ValueError("A unique eventId of at most 128 characters is required")
            reported = payload.get("cycleSeconds")
            if reported is not None:
                if type(reported) not in (int, float) or not math.isfinite(reported) or not 120 <= reported <= 3600:
                    raise ValueError("cycleSeconds must be between 120 and 3600")
            if event_id in self.events:
                return {"accepted": False, "reason": "duplicate"}
            if self.last_arrival and observed <= self.last_arrival:
                raise ValueError("Observation predates the last accepted arrival")
            interval = (observed - self.last_arrival).total_seconds() if self.last_arrival else None
            if interval is not None and interval < 120:
                return {"accepted": False, "reason": "same_stop"}
            baseline = median(self.cycles) if self.cycles else None
            if baseline is not None and interval is not None and interval < 0.65 * baseline:
                return {"accepted": False, "reason": "too_soon"}
            self.note = None
            if interval is not None:
                if 120 <= interval <= 3600 and (baseline is None or 0.65 * baseline <= interval <= 1.5 * baseline):
                    self.cycles = (self.cycles + [interval])[-5:]
                else:
                    self.note = "חריגה בין זיהויים; זמן המחזור הקודם נשמר"
            elif reported is not None:
                self.cycles = [float(reported)]
            self.last_arrival = observed
            if (now - observed).total_seconds() <= self.stale_after:
                seen = min(observed, now)
                self.last_seen = max(self.last_seen or seen, seen)
            self.events = (self.events + [event_id])[-64:]
            self._save()
            return {"accepted": True, "lastArrivalAt": iso(observed)}

    def snapshot(self):
        with self.lock:
            now = self.clock()
            connected = bool(self.last_seen and 0 <= (now - self.last_seen).total_seconds() <= self.stale_after)
            cycle = median(self.cycles) if self.cycles else None
            if not self.last_arrival or not cycle:
                status = "waiting"
            elif not connected:
                status = "stale"
            elif (now - self.last_arrival).total_seconds() >= cycle:
                status = "uncertain"  # Never wrap a real event into a fictional new arrival.
            else:
                status = "tracking"
            return {"mode": "live", "sourceConnected": connected,
                    "lastArrivalAt": iso(self.last_arrival), "lastSeenAt": iso(self.last_seen),
                    "cycleSeconds": cycle, "latestCycleSeconds": self.cycles[-1] if self.cycles else None,
                    "measurementStatus": status, "message": self.note,
                    "serverTime": iso(now), "calibrated": False}
