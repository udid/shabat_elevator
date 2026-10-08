"""Validate real floor-seven departures and keep prediction confidence explicit."""

from __future__ import annotations

import json
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock


MIN_CYCLE_SECONDS = 300
MAX_CYCLE_SECONDS = 1800
MAX_CANDIDATES = 1024
MAX_DETECTION_AGE_SECONDS = 2 * 60 * 60


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
                 cycle_tolerance_percent=15,
                 event_kind="departure"):
        if event_kind != "departure":
            raise ValueError("This detector reports departure events only")
        if default_cycle_seconds is not None and not valid_cycle(default_cycle_seconds):
            raise ValueError("default_cycle_seconds must be greater than 300 and less than 1800")
        if (type(cycle_tolerance_percent) not in (int, float)
                or not 0 <= cycle_tolerance_percent < 100 or not math.isfinite(cycle_tolerance_percent)):
            raise ValueError("cycle_tolerance_percent must be at least 0 and less than 100")
        if type(stale_after) not in (int, float) or not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale_after must be positive")
        self.path = path
        self.device_id = device_id
        self.clock = clock
        self.stale_after = stale_after
        self.arrival_grace = arrival_grace  # Retained for compatibility with callers.
        self.default_cycle_seconds = default_cycle_seconds
        self.cycle_tolerance_percent = cycle_tolerance_percent
        tolerance = cycle_tolerance_percent / 100
        # timedelta rounds to the same microsecond precision as observations,
        # keeping exact percentage boundaries inclusive despite float rounding.
        self._cycle_windows = tuple(
            (timedelta(seconds=default_cycle_seconds * laps * (1 - tolerance)),
             timedelta(seconds=default_cycle_seconds * laps * (1 + tolerance)))
            for laps in (1, 2)
        ) if default_cycle_seconds is not None else ()
        self.event_kind = event_kind
        self.lock = RLock()
        self.last_departure = None
        self.last_seen = None
        # Only acoustic-pass candidates enter here. They are kept even when the
        # timing gate rejects them, but never reused across capture interruptions.
        self.candidates: deque[tuple[datetime, str]] = deque(maxlen=MAX_CANDIDATES)
        self._seen_candidate_ids: deque[str] = deque(maxlen=MAX_CANDIDATES)
        self._candidate_since = self.clock()
        self.events: list[str] = []
        self.note = None
        self._anchor_current = False
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
            events = [x for x in saved.get("events", [])
                      if isinstance(x, str) and x.strip() and len(x) <= 128][-64:]
            self.last_departure = departure
            self.events = events
            self._expire_history(self.clock())
            # Old measured cycles are ignored. Disk history never proves this
            # anchor or a candidate pair belongs to the current capture session.
        except (ValueError, TypeError, KeyError, OSError, OverflowError):
            self.last_departure = None
            self.events = []

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"schemaVersion": 3, "deviceId": self.device_id,
                                    "eventKind": self.event_kind,
                                    "lastDepartureAt": iso(self.last_departure),
                                    "events": self.events}), encoding="utf-8")
        temp.replace(self.path)

    def _expire_history(self, now):
        """Forget an expired departure without disrupting fresh audio candidates."""
        if (self.last_departure is not None
                and now - self.last_departure > timedelta(seconds=MAX_DETECTION_AGE_SECONDS)):
            self.last_departure = None
            self.events = []
            self._anchor_current = False
            # Write once at expiry, not on every heartbeat or state request.
            self._save()

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
            self.candidates.clear()
            if self.last_seen is not None:
                disconnected_at = min(now, self.last_seen + timedelta(seconds=self.stale_after))
                self._candidate_since = max(self._candidate_since, disconnected_at)

    def heartbeat(self, payload):
        with self.lock:
            observed, now = self._validate(payload, self.stale_after)
            self._expire_history(now)
            self._invalidate_disconnected_anchor(now)
            self.last_seen = max(self.last_seen or observed, observed)
            return {"accepted": True}

    def sensor_unavailable(self, message=None):
        """Invalidate confidence immediately when microphone capture fails or stops."""
        with self.lock:
            self.last_seen = None
            self._anchor_current = False
            self.candidates.clear()
            self._candidate_since = self.clock()
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
            # Legacy client hints are validated, but cannot override calibration.
            reported = payload.get("cycleSeconds")
            if reported is not None and not valid_cycle(reported):
                raise ValueError("cycleSeconds must be greater than 300 and less than 1800")
            self._expire_history(now)
            if event_id in self.events or event_id in self._seen_candidate_ids:
                return {"accepted": False, "reason": "duplicate"}
            if self.last_departure and observed <= self.last_departure:
                raise ValueError("Observation predates the last accepted departure")
            self._invalidate_disconnected_anchor(now)
            if observed < self._candidate_since:
                return {"accepted": False, "reason": "previous_capture"}
            if self.candidates and observed <= self.candidates[-1][0]:
                raise ValueError("Observation predates the latest candidate")
            self._seen_candidate_ids.append(event_id)
            # A genuine acoustic candidate proves capture is alive, but does not
            # advance lastDepartureAt, lastDetectionAt, or the two-hour deadline.
            self.last_seen = max(self.last_seen or observed, observed)
            if not self._cycle_windows:
                return {"accepted": False, "reason": "missing_cycle_config"}

            horizon = self._cycle_windows[-1][1]
            cutoff = now - timedelta(seconds=MAX_DETECTION_AGE_SECONDS)
            while self.candidates and (observed - self.candidates[0][0] > horizon
                                       or self.candidates[0][0] < cutoff):
                self.candidates.popleft()
            matches = any(lower <= observed - previous <= upper
                          for previous, _ in self.candidates
                          for lower, upper in self._cycle_windows)
            self.candidates.append((observed, event_id))
            interval = observed - self.last_departure if self.last_departure else None
            if interval is not None and interval < self._cycle_windows[0][0]:
                return {"accepted": False, "reason": "same_stop" if interval.total_seconds() < 30 else "too_soon"}
            if not matches:
                return {"accepted": False, "reason": "awaiting_cycle_match"}

            self.note = None
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
            self._expire_history(now)
            connected = self._connected(now)
            self._invalidate_disconnected_anchor(now)
            cycle = self.default_cycle_seconds
            cycle_source = "configured" if cycle is not None else None
            if not self.last_departure or cycle is None:
                status = "waiting"
            elif not connected:
                status = "stale"
            elif (not self._anchor_current
                  or (now - self.last_departure).total_seconds() >= MAX_DETECTION_AGE_SECONDS):
                status = "uncertain"
            else:
                # Forecast further laps with the configured cycle, without
                # synthesizing a detection or adapting calibration from sounds.
                status = "tracking"
            return {"mode": "live", "sourceConnected": connected,
                    "anchorKind": self.event_kind,
                    "lastDepartureAt": iso(self.last_departure),
                    "lastDetectionAt": iso(self.last_departure),
                    "lastArrivalAt": None, "lastSeenAt": iso(self.last_seen),
                    "cycleSeconds": cycle, "cycleSource": cycle_source,
                    "latestCycleSeconds": None,
                    "measurementStatus": status, "message": self.note,
                    "serverTime": iso(now), "calibrated": False}
