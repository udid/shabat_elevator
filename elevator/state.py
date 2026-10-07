"""Validate real floor-seven departures and keep prediction confidence explicit."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from threading import RLock


MIN_CYCLE_SECONDS = 300
MAX_CYCLE_SECONDS = 1800


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


def valid_cycle(value: object) -> bool:
    return (type(value) in (int, float) and MIN_CYCLE_SECONDS < value < MAX_CYCLE_SECONDS
            and math.isfinite(value))


class ObservationStore:
    def __init__(self, path: Path | None = None, *, device_id="begin17-floor7", clock=utc_now,
                 stale_after=90, arrival_grace=60, default_cycle_seconds=None,
                 event_kind="departure"):
        if event_kind != "departure":
            raise ValueError("This detector reports departure events only")
        if default_cycle_seconds is not None and not valid_cycle(default_cycle_seconds):
            raise ValueError("default_cycle_seconds must be greater than 300 and less than 1800")
        if type(stale_after) not in (int, float) or not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale_after must be positive")
        self.path = path
        self.device_id = device_id
        self.clock = clock
        self.stale_after = stale_after
        self.arrival_grace = arrival_grace  # Retained for callers; no fictional extra lap.
        self.default_cycle_seconds = default_cycle_seconds
        self.event_kind = event_kind
        self.lock = RLock()
        self.last_departure = None
        self.last_seen = None
        self.cycles: list[float] = []
        self.events: list[str] = []
        self.note = None
        self._anchor_current = False
        self._interval_anomaly = False
        self._load()

    def _load(self):
        if not self.path or not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            # An old arrival timestamp cannot safely become a departure timestamp.
            if (not isinstance(saved, dict) or saved.get("deviceId") != self.device_id
                    or saved.get("eventKind") != self.event_kind):
                return
            departure = parse_time(saved["lastDepartureAt"]) if saved.get("lastDepartureAt") else None
            if departure and departure > self.clock() + timedelta(seconds=5):
                return
            cycles = [float(x) for x in saved.get("cycles", []) if valid_cycle(x)][-5:]
            events = [x for x in saved.get("events", [])
                      if isinstance(x, str) and x.strip() and len(x) <= 128][-64:]
            self.last_departure = departure
            self.cycles = cycles if departure else []
            self.events = events
            # Disk history never proves this anchor belongs to the current session.
        except (ValueError, TypeError, KeyError, OSError, OverflowError):
            self.last_departure = None
            self.cycles = []
            self.events = []

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"schemaVersion": 2, "deviceId": self.device_id,
                                    "eventKind": self.event_kind,
                                    "lastDepartureAt": iso(self.last_departure),
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
        return min(observed, now), now

    def _connected(self, now):
        return bool(self.last_seen and 0 <= (now - self.last_seen).total_seconds() <= self.stale_after)

    def _invalidate_disconnected_anchor(self, now):
        if not self._connected(now):
            self._anchor_current = False

    def heartbeat(self, payload):
        with self.lock:
            observed, now = self._validate(payload, self.stale_after)
            self._invalidate_disconnected_anchor(now)
            self.last_seen = max(self.last_seen or observed, observed)
            return {"accepted": True}

    def sensor_unavailable(self, message=None):
        """Invalidate confidence immediately when microphone capture fails or stops."""
        with self.lock:
            self.last_seen = None
            self._anchor_current = False
            self.note = message or "החיישן אינו זמין; ממתינים לחיבור ולזיהוי חדש"

    def observe(self, payload):
        with self.lock:
            observed, now = self._validate(payload, self.stale_after)
            if payload.get("eventKind") != self.event_kind:
                raise ValueError("eventKind must be departure")
            if type(payload.get("floor")) is not int or payload["floor"] != 7:
                raise ValueError("This source must report a departure from floor 7")
            event_id = payload.get("eventId")
            if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
                raise ValueError("A unique eventId of at most 128 characters is required")
            # This legacy hint is validated, but only observed intervals are measured.
            reported = payload.get("cycleSeconds")
            if reported is not None and not valid_cycle(reported):
                raise ValueError("cycleSeconds must be greater than 300 and less than 1800")
            if event_id in self.events:
                return {"accepted": False, "reason": "duplicate"}
            if self.last_departure and observed <= self.last_departure:
                raise ValueError("Observation predates the last accepted departure")
            interval = (observed - self.last_departure).total_seconds() if self.last_departure else None
            if interval is not None and interval <= MIN_CYCLE_SECONDS:
                return {"accepted": False, "reason": "same_stop" if interval < 30 else "too_soon"}
            baseline = median(self.cycles) if self.cycles else None
            if baseline is not None and interval is not None and interval < 0.65 * baseline:
                return {"accepted": False, "reason": "too_soon"}
            self._invalidate_disconnected_anchor(now)
            continuous = self._anchor_current
            self.note = None
            self._interval_anomaly = False
            if interval is not None and continuous:
                if valid_cycle(interval) and (baseline is None or interval <= 1.5 * baseline):
                    # The configured default is never added to measurement history.
                    self.cycles = (self.cycles + [interval])[-5:]
                else:
                    self._interval_anomaly = True
                    self.note = "חריגה בין זיהויים; זמן המחזור הקודם נשמר עד למדידה תקינה"
            self.last_departure = observed
            self.last_seen = max(self.last_seen or observed, observed)
            self._anchor_current = True
            self.events = (self.events + [event_id])[-64:]
            self._save()
            return {"accepted": True, "eventKind": self.event_kind,
                    "lastDepartureAt": iso(observed)}

    def snapshot(self):
        with self.lock:
            now = self.clock()
            connected = self._connected(now)
            self._invalidate_disconnected_anchor(now)
            cycle = (median(self.cycles) if self.cycles else self.default_cycle_seconds) if self.last_departure else None
            cycle_source = ("measured" if self.cycles else "default") if cycle is not None else None
            if not self.last_departure or cycle is None:
                status = "waiting"
            elif not connected:
                status = "stale"
            elif (not self._anchor_current or self._interval_anomaly
                  or (now - self.last_departure).total_seconds() >= cycle):
                status = "uncertain"  # Never wrap a real event into a fictional new departure.
            else:
                status = "tracking"
            return {"mode": "live", "sourceConnected": connected,
                    "anchorKind": self.event_kind,
                    "lastDepartureAt": iso(self.last_departure),
                    "lastDetectionAt": iso(self.last_departure),
                    "lastArrivalAt": None, "lastSeenAt": iso(self.last_seen),
                    "cycleSeconds": cycle, "cycleSource": cycle_source,
                    "latestCycleSeconds": self.cycles[-1] if self.cycles else None,
                    "measurementStatus": status, "message": self.note,
                    "serverTime": iso(now), "calibrated": False}
