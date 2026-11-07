"""Bounded, anonymous browser presence and durable hourly usage aggregates."""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from zoneinfo import ZoneInfo

from .state import iso, utc_now

LOGGER = logging.getLogger(__name__)
VISITOR_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
HOUR = 3600
FLOORS = tuple(range(-1, 13))
UNSELECTED_FLOOR = "none"
UNREPORTED_FLOOR = "unknown"


def floor_key(floor):
    """Keep absent/invalid old-client data distinct from an empty selection."""
    if floor is None:
        return UNSELECTED_FLOOR
    if type(floor) is int and floor in FLOORS:
        return str(floor)
    return UNREPORTED_FLOOR


class AnalyticsUnavailable(RuntimeError):
    """Collection has stopped or storage cannot safely be used."""


class VisitorAnalytics:
    """Integrate concurrency over known collection time, never over restarts.

    Saved visitor IDs deduplicate the most recently checkpointed hour. Hourly
    aggregates contain no IDs. Collection runs every 30 seconds; completed
    hours are checkpointed even with no dashboard open. Selected-floor usage
    stores only accumulated browser-seconds; a browser's latest reported floor
    receives its active time, including when several tabs are open.
    """

    active_window_seconds = 90
    retention_days = 90
    collection_seconds = 30
    flush_seconds = HOUR
    max_active = 10_000
    max_hour_visitors = 50_000

    def __init__(self, path: Path | str | None = None, *, clock=utc_now, monotonic_clock=monotonic,
                 background=False, max_active=None, max_hour_visitors=None):
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self.lock = RLock()
        self._stop = Event()
        self._thread = None
        self._closed = False
        self._failed = False
        self.max_active = max_active if max_active is not None else type(self).max_active
        self.max_hour_visitors = (max_hour_visitors if max_hour_visitors is not None
                                  else type(self).max_hour_visitors)
        if self.max_active < 1 or self.max_hour_visitors < 1:
            raise ValueError("Analytics limits must be positive")
        self.timezone = ZoneInfo("Asia/Jerusalem")
        now = self._now()
        if path is not None and str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path is not None else ":memory:",
                                   check_same_thread=False, timeout=2)
        try:
            self._db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS hours (
                    start INTEGER PRIMARY KEY,
                    unique_visitors INTEGER NOT NULL,
                    peak INTEGER NOT NULL,
                    visitor_seconds REAL NOT NULL,
                    coverage_seconds REAL NOT NULL,
                    limited INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS current_visitors (
                    visitor TEXT PRIMARY KEY, hour INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS floor_hours (
                    start INTEGER PRIMARY KEY,
                    coverage_seconds REAL NOT NULL,
                    reported INTEGER NOT NULL,
                    usage_seconds TEXT NOT NULL
                );
            """)
            stored = self._db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()
            self.started_at = float(stored[0]) if stored else now
            floor_started = self._db.execute("SELECT value FROM metadata WHERE key='floors_started_at'").fetchone()
            self.floors_started_at = float(floor_started[0]) if floor_started else now
            checkpoint = self._db.execute("SELECT value FROM metadata WHERE key='checkpoint_at'").fetchone()
            self._last = max(now, float(checkpoint[0])) if checkpoint else now
            self._last_observed_wall = now
            self._last_monotonic = self.monotonic_clock()
            self._clock_blocked = now < self._last
            self._last_flush = now
            self._visitor_hour = self._hour_start(self._last)
            self._active = {}
            self._active_floors = {}
            self._hours = {
                row[0]: dict(unique=row[1], peak=row[2], visitor_seconds=row[3],
                             coverage=row[4], limited=bool(row[5]))
                for row in self._db.execute("SELECT * FROM hours WHERE start >= ?",
                                            (self._retention_start(now),))
            }
            self._visitors = {
                row[0] for row in self._db.execute(
                    "SELECT visitor FROM current_visitors WHERE hour=? LIMIT ?",
                    (self._visitor_hour, self.max_hour_visitors))
            }
            self._floor_hours = {
                row[0]: dict(coverage=row[1], reported=bool(row[2]), seconds=json.loads(row[3]))
                for row in self._db.execute("SELECT * FROM floor_hours WHERE start >= ?",
                                            (self._retention_start(now),))
            }
            self._dirty_hours = set()
            self._dirty_floor_hours = set()
            self._new_visitors = set()
            self._flush(now)
            if background:
                self._thread = Thread(target=self._run, name="elevator-analytics", daemon=True)
                self._thread.start()
        except Exception:
            self._db.close()
            raise

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Analytics clock must have a timezone")
        return value.timestamp()

    @staticmethod
    def _hour_start(timestamp):
        return math.floor(timestamp / HOUR) * HOUR

    def _retention_start(self, now):
        return self._hour_start(now - self.retention_days * 24 * HOUR)

    @staticmethod
    def _iso(timestamp):
        return iso(datetime.fromtimestamp(timestamp, timezone.utc))

    def _check_available(self):
        if self._closed or self._failed:
            raise AnalyticsUnavailable("Visitor analytics is unavailable")

    def _bucket(self, hour):
        self._dirty_hours.add(hour)
        return self._hours.setdefault(hour, dict(unique=0, peak=0, visitor_seconds=0.0,
                                                 coverage=0.0, limited=False))

    def _floor_bucket(self, hour):
        self._dirty_floor_hours.add(hour)
        return self._floor_hours.setdefault(hour, dict(coverage=0.0, reported=False, seconds={}))

    def _advance(self, now):
        steady = self.monotonic_clock()
        elapsed = steady - self._last_monotonic
        wall_elapsed = now - self._last_observed_wall
        self._last_monotonic, self._last_observed_wall = steady, now
        self._clock_blocked = now < self._last
        if self._clock_blocked:
            # Never overlap already committed coverage after NTP corrections,
            # including a restart while the wall clock is behind its checkpoint.
            self._active.clear()
            self._active_floors.clear()
            return now
        if (now - self._last > 3 * self.collection_seconds
                or abs(wall_elapsed - elapsed) > 5 or elapsed < 0):
            # Missed multiple collector ticks, suspend, or a clock adjustment:
            # the elapsed interval cannot be claimed as measured uptime.
            self._last = now
            self._active.clear()
            self._active_floors.clear()
        cursor = self._last
        expiries = sorted((expiry, visitor) for visitor, expiry in self._active.items() if expiry <= now)
        expired = 0
        active_count = len(self._active)
        floor_counts = {}
        for visitor in self._active:
            key = self._active_floors.get(visitor, UNREPORTED_FLOOR)
            floor_counts[key] = floor_counts.get(key, 0) + 1
        while cursor < now:
            while expired < len(expiries) and expiries[expired][0] <= cursor:
                active_count -= 1
                key = self._active_floors.get(expiries[expired][1], UNREPORTED_FLOOR)
                floor_counts[key] -= 1
                expired += 1
            hour = self._hour_start(cursor)
            next_expiry = expiries[expired][0] if expired < len(expiries) else now
            boundary = min(now, hour + HOUR, next_expiry)
            elapsed = boundary - cursor
            bucket = self._bucket(hour)
            bucket["coverage"] += elapsed
            bucket["visitor_seconds"] += elapsed * active_count
            bucket["peak"] = max(bucket["peak"], active_count)
            floor_bucket = self._floor_bucket(hour)
            floor_bucket["coverage"] += elapsed
            for key, count in floor_counts.items():
                if count:
                    floor_bucket["seconds"][key] = floor_bucket["seconds"].get(key, 0.0) + elapsed * count
            cursor = boundary
        self._active = {visitor: expiry for visitor, expiry in self._active.items() if expiry > now}
        self._active_floors = {visitor: floor for visitor, floor in self._active_floors.items()
                               if visitor in self._active}
        self._last = now
        hour = self._hour_start(now)
        if hour != self._visitor_hour:
            self._visitor_hour = hour
            self._visitors.clear()
            self._new_visitors.clear()
        return now

    def record(self, visitor: str, floor=UNREPORTED_FLOOR) -> bool:
        """Record one visible browser's heartbeat; invalid IDs never count."""
        if not isinstance(visitor, str) or not VISITOR_PATTERN.fullmatch(visitor):
            return False
        with self.lock:
            self._check_available()
            now = self._advance(self._now())
            if self._clock_blocked:
                return False
            bucket = self._bucket(self._visitor_hour)
            accepted = True
            if visitor in self._active or len(self._active) < self.max_active:
                self._active[visitor] = now + self.active_window_seconds
                key = floor_key(floor)
                self._active_floors[visitor] = key
                if key != UNREPORTED_FLOOR:
                    self._floor_bucket(self._visitor_hour)["reported"] = True
            else:
                bucket["limited"] = True
                accepted = False
            if visitor not in self._visitors:
                if len(self._visitors) < self.max_hour_visitors:
                    self._visitors.add(visitor)
                    self._new_visitors.add(visitor)
                    bucket["unique"] += 1
                else:
                    bucket["limited"] = True
                    accepted = False
            bucket["peak"] = max(bucket["peak"], len(self._active))
            self._maybe_flush(now)
            return accepted

    def _flush(self, now):
        cutoff = self._retention_start(now)
        try:
            with self._db:
                self._db.executemany(
                    "INSERT OR REPLACE INTO hours VALUES (?, ?, ?, ?, ?, ?)",
                    [(hour, bucket["unique"], bucket["peak"], bucket["visitor_seconds"],
                      bucket["coverage"], int(bucket["limited"]))
                     for hour in self._dirty_hours if hour >= cutoff
                     for bucket in (self._hours[hour],)])
                self._db.execute("DELETE FROM hours WHERE start < ?", (cutoff,))
                self._db.executemany(
                    "INSERT OR REPLACE INTO floor_hours VALUES (?, ?, ?, ?)",
                    [(hour, bucket["coverage"], int(bucket["reported"]), json.dumps(bucket["seconds"]))
                     for hour in self._dirty_floor_hours if hour >= cutoff
                     for bucket in (self._floor_hours[hour],)])
                self._db.execute("DELETE FROM floor_hours WHERE start < ?", (cutoff,))
                self._db.execute("DELETE FROM current_visitors WHERE hour != ?", (self._visitor_hour,))
                self._db.executemany("INSERT OR IGNORE INTO current_visitors VALUES (?, ?)",
                                     [(visitor, self._visitor_hour) for visitor in self._new_visitors])
                self._db.execute("INSERT OR REPLACE INTO metadata VALUES ('started_at', ?)",
                                 (self.started_at,))
                self._db.execute("INSERT OR REPLACE INTO metadata VALUES ('floors_started_at', ?)",
                                 (self.floors_started_at,))
                self._db.execute("INSERT OR REPLACE INTO metadata VALUES ('checkpoint_at', ?)",
                                 (self._last,))
            self._hours = {hour: bucket for hour, bucket in self._hours.items() if hour >= cutoff}
            self._floor_hours = {hour: bucket for hour, bucket in self._floor_hours.items() if hour >= cutoff}
            self._dirty_hours.clear()
            self._dirty_floor_hours.clear()
            self._new_visitors.clear()
            self._last_flush = now
        except Exception:
            self._failed = True
            raise

    def _maybe_flush(self, now):
        # Persist at hour boundaries, independently of frequent live reads and
        # collection ticks. This also expires the previous hour's saved IDs.
        if now // self.flush_seconds > self._last_flush // self.flush_seconds:
            self._flush(now)

    def tick(self):
        with self.lock:
            self._check_available()
            self._maybe_flush(self._advance(self._now()))

    def _run(self):
        while not self._stop.wait(self.collection_seconds):
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Visitor analytics collection stopped")
                with self.lock:
                    self._failed = True
                return

    def _range(self, now, start, end):
        current_hour = self._hour_start(now)
        if start is None and end is None:
            return current_hour - 23 * HOUR, current_hour + HOUR
        if start is None or end is None:
            raise ValueError("Both start and end are required (YYYY-MM-DD)")
        if not DATE_PATTERN.fullmatch(start) or not DATE_PATTERN.fullmatch(end):
            raise ValueError("Dates must use YYYY-MM-DD")
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as error:
            raise ValueError("Invalid calendar date") from error
        today = datetime.fromtimestamp(now, self.timezone).date()
        if first > last or (last - first).days >= self.retention_days:
            raise ValueError("Date range must include 1 to 90 days")
        if first < today - timedelta(days=self.retention_days - 1) or last > today:
            raise ValueError("Dates must be within the last 90 days, including today")
        beginning = datetime.combine(first, time.min, self.timezone).timestamp()
        ending = datetime.combine(last + timedelta(days=1), time.min, self.timezone).timestamp()
        return int(beginning), int(min(ending, current_hour + HOUR))

    def snapshot(self, *, start=None, end=None):
        with self.lock:
            self._check_available()
            raw_now = self._now()
            beginning, ending = self._range(raw_now, start, end)
            now = self._advance(raw_now)
            if self._clock_blocked:
                raise AnalyticsUnavailable("Visitor analytics is waiting for the clock to recover")
            self._maybe_flush(now)
            hours = []
            for hour in range(beginning, ending, HOUR):
                bucket = self._hours.get(hour)
                coverage = bucket["coverage"] if bucket else 0.0
                # A just-arrived heartbeat is meaningful before any time elapses.
                measured = bucket is not None and (coverage > 0 or bucket["unique"] > 0)
                hours.append({
                    "start": self._iso(hour), "end": self._iso(hour + HOUR),
                    "uniqueVisitors": bucket["unique"] if measured else None,
                    "peakConcurrent": bucket["peak"] if measured else None,
                    "averageConcurrent": (round(bucket["visitor_seconds"] / coverage, 3)
                                          if coverage > 0 else None),
                    "coverageSeconds": round(coverage, 3),
                    "partial": coverage < HOUR - 0.001 or hour + HOUR > now,
                    "limited": bool(bucket and bucket["limited"]),
                })
            return {
                "available": True, "generatedAt": self._iso(now), "timezone": "Asia/Jerusalem",
                "retentionDays": self.retention_days, "activeWindowSeconds": self.active_window_seconds,
                "activeNow": len(self._active), "collectionStartedAt": self._iso(self.started_at),
                "rangeStart": self._iso(beginning), "rangeEnd": self._iso(ending), "hours": hours,
                "floorUsage": self._floor_snapshot(beginning, ending, now),
            }

    def _floor_snapshot(self, beginning, ending, now):
        buckets = [bucket for hour, bucket in self._floor_hours.items() if beginning <= hour < ending]
        coverage = sum(bucket["coverage"] for bucket in buckets)
        available = coverage > 0 or any(bucket["reported"] for bucket in buckets)
        totals = {}
        for bucket in buckets:
            for key, seconds in bucket["seconds"].items():
                totals[key] = totals.get(key, 0.0) + seconds
        active = {}
        for visitor in self._active:
            key = self._active_floors.get(visitor, UNREPORTED_FLOOR)
            active[key] = active.get(key, 0) + 1
        elapsed = max(0, min(now, ending) - beginning)
        limited = any(bucket["limited"] for hour, bucket in self._hours.items() if beginning <= hour < ending)
        unreported = totals.get(UNREPORTED_FLOOR, 0.0)
        return {
            "collectionStartedAt": self._iso(self.floors_started_at),
            "available": available,
            "partial": not available or coverage < elapsed - 0.001 or limited or unreported > 0,
            "coverageSeconds": round(coverage, 3),
            "floors": [
                {"floor": floor, "usageSeconds": round(totals.get(floor_key(floor), 0.0), 3) if available else None,
                 "activeNow": active.get(floor_key(floor), 0)}
                for floor in (*FLOORS, None)
            ],
            "unreportedActiveNow": active.get(UNREPORTED_FLOOR, 0),
            "unreportedUsageSeconds": round(unreported, 3) if available else None,
        }

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not current_thread():
            self._thread.join()
        with self.lock:
            if self._closed:
                return
            try:
                if not self._failed:
                    self._flush(self._advance(self._now()))
            except Exception:
                LOGGER.exception("Could not save visitor analytics during shutdown")
            finally:
                self._closed = True
                self._db.close()
