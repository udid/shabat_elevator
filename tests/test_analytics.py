import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from elevator.analytics import AnalyticsUnavailable, VisitorAnalytics


A = "a" * 32
B = "b" * 32
C = "c" * 32


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.clock = Mock(return_value=self.start)

    def create(self, path=None, **kwargs):
        kwargs.setdefault("monotonic_clock", lambda: self.clock.return_value.timestamp())
        analytics = VisitorAnalytics(path, clock=self.clock, **kwargs)
        self.addCleanup(analytics.close)
        return analytics

    def at(self, seconds):
        self.clock.return_value = self.start + timedelta(seconds=seconds)

    def collect_until(self, analytics, seconds):
        current = int((self.clock.return_value - self.start).total_seconds())
        for second in range(current + 30, seconds, 30):
            self.at(second)
            analytics.tick()
        self.at(seconds)
        analytics.tick()

    def test_distinct_browsers_duplicate_tabs_expiry_and_weighted_average(self):
        analytics = self.create()
        self.at(10)
        analytics.record(A)
        self.at(40)
        analytics.record(A)
        analytics.record(B)
        self.assertEqual(analytics.snapshot()["activeNow"], 2)
        self.at(129)
        self.assertEqual(analytics.snapshot()["activeNow"], 2)
        self.at(130)
        self.assertEqual(analytics.snapshot()["activeNow"], 0)
        self.at(200)
        hour = analytics.snapshot()["hours"][-1]
        self.assertEqual(hour["uniqueVisitors"], 2)
        self.assertEqual(hour["peakConcurrent"], 2)
        self.assertEqual(hour["coverageSeconds"], 200)
        self.assertEqual(hour["averageConcurrent"], 1.05)
        self.assertTrue(hour["partial"])

    def test_concurrency_crosses_hour_boundary_but_uniques_require_a_report(self):
        self.at(3570)
        analytics = self.create()
        analytics.record(A)
        self.at(3630)
        analytics.record(B)
        self.at(3720)
        previous, current = analytics.snapshot()["hours"][-2:]
        self.assertEqual(previous["uniqueVisitors"], 1)
        self.assertEqual(previous["averageConcurrent"], 1)
        self.assertEqual(previous["coverageSeconds"], 30)
        self.assertEqual(current["uniqueVisitors"], 1)
        self.assertEqual(current["peakConcurrent"], 2)
        self.assertEqual(current["averageConcurrent"], 1.25)
        self.assertEqual(current["coverageSeconds"], 120)

    def test_known_zero_and_unknown_intervals_are_different(self):
        analytics = self.create()
        self.collect_until(analytics, 3600)
        hours = analytics.snapshot()["hours"]
        self.assertIsNone(hours[-3]["uniqueVisitors"])
        self.assertIsNone(hours[-3]["averageConcurrent"])
        self.assertEqual(hours[-3]["coverageSeconds"], 0)
        self.assertEqual(hours[-2]["uniqueVisitors"], 0)
        self.assertEqual(hours[-2]["averageConcurrent"], 0)
        self.assertEqual(hours[-2]["coverageSeconds"], 3600)
        self.assertFalse(hours[-2]["partial"])

    def test_instantaneous_report_has_count_but_no_average_yet(self):
        analytics = self.create()
        analytics.record(A)
        hour = analytics.snapshot()["hours"][-1]
        self.assertEqual(hour["uniqueVisitors"], 1)
        self.assertEqual(hour["peakConcurrent"], 1)
        self.assertIsNone(hour["averageConcurrent"])
        self.assertEqual(hour["coverageSeconds"], 0)

    def test_restart_preserves_counts_and_dedup_but_does_not_fill_downtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analytics.sqlite3"
            first = VisitorAnalytics(path, clock=self.clock, monotonic_clock=lambda: self.clock.return_value.timestamp())
            first.record(A)
            self.at(60)
            first.close()
            self.at(120)
            second = VisitorAnalytics(path, clock=self.clock, monotonic_clock=lambda: self.clock.return_value.timestamp())
            try:
                self.assertEqual(second.snapshot()["activeNow"], 0)
                second.record(A)
                self.at(180)
                result = second.snapshot()
                hour = result["hours"][-1]
                self.assertEqual(result["collectionStartedAt"], "2026-10-24T00:00:00Z")
                self.assertEqual(hour["uniqueVisitors"], 1)
                self.assertEqual(hour["coverageSeconds"], 120)
                self.assertEqual(hour["averageConcurrent"], 1)
                self.assertTrue(hour["partial"])
                self.at(3601)
                second.tick()
                with closing(sqlite3.connect(path)) as reader:
                    self.assertEqual(reader.execute("SELECT count(*) FROM current_visitors").fetchone()[0], 0)
            finally:
                second.close()

    def test_periodic_checkpoint_without_statistics_page_access(self):
        analytics = self.create()
        analytics.record(A)
        self.collect_until(analytics, 3600)
        row = analytics._db.execute("SELECT visitor_seconds, coverage_seconds FROM hours").fetchone()
        self.assertEqual(row, (90, 3600))

    def test_live_updates_do_not_write_until_the_hour_boundary(self):
        analytics = self.create()
        baseline_changes = analytics._db.total_changes
        analytics.record(A)
        for second in range(30, 3600, 30):
            self.at(second)
            analytics.tick()
            snapshot = analytics.snapshot()
            self.assertEqual(snapshot["activeNow"], int(second < 90))
            self.assertEqual(snapshot["hours"][-1]["coverageSeconds"], second)
            self.assertEqual(analytics._db.total_changes, baseline_changes)
        self.assertEqual(analytics._db.execute("SELECT count(*) FROM hours").fetchone()[0], 0)
        self.at(3600)
        analytics.tick()
        saved = analytics._db.execute(
            "SELECT unique_visitors, peak, visitor_seconds, coverage_seconds FROM hours"
        ).fetchone()
        self.assertEqual(saved, (1, 1, 90, 3600))
        saved_changes = analytics._db.total_changes
        analytics.record(B)
        analytics.snapshot()
        self.assertEqual(analytics._db.total_changes, saved_changes)

    def test_partial_first_hour_and_shutdown_preserve_hourly_dedup(self):
        self.at(3570)
        analytics = self.create()
        analytics.record(A)
        self.at(3600)
        analytics.record(A)
        rows = analytics._db.execute(
            "SELECT unique_visitors, coverage_seconds FROM hours ORDER BY start"
        ).fetchall()
        self.assertEqual(rows, [(1, 30), (1, 0)])
        self.assertEqual(analytics._db.execute(
            "SELECT visitor, hour FROM current_visitors"
        ).fetchall(), [(A, int(self.start.timestamp()) + 3600)])
        # Saving a partial hour on graceful shutdown is intentional, even when
        # the next scheduled hourly checkpoint is not due yet.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analytics.sqlite3"
            persistent = self.create(path)
            persistent.record(B)
            self.at(3630)
            persistent.close()
            with closing(sqlite3.connect(path)) as reader:
                self.assertEqual(reader.execute(
                    "SELECT unique_visitors, coverage_seconds FROM hours"
                ).fetchone(), (1, 30))

    def test_old_aggregates_are_pruned(self):
        analytics = self.create()
        analytics.record(A)
        self.at(91 * 86400)
        analytics.record(B)
        analytics.tick()
        cutoff = analytics._retention_start(self.clock.return_value.timestamp())
        self.assertTrue(all(hour >= cutoff for hour in analytics._hours))
        self.assertGreaterEqual(analytics._db.execute("SELECT min(start) FROM hours").fetchone()[0], cutoff)
        self.assertLessEqual(len(analytics._hours), 2161)

    def test_identifiers_and_memory_are_bounded_and_limits_are_visible(self):
        analytics = self.create(max_active=1, max_hour_visitors=2)
        for invalid in (None, "", "x" * 32, "A" * 32, "a" * 33, A + "\n", "../secret"):
            self.assertFalse(analytics.record(invalid))
        self.assertTrue(analytics.record(A))
        self.assertFalse(analytics.record(B))
        self.assertFalse(analytics.record(C))
        analytics.tick()
        self.assertEqual(len(analytics._active), 1)
        self.assertEqual(len(analytics._visitors), 2)
        hour = analytics.snapshot()["hours"][-1]
        self.assertEqual(hour["uniqueVisitors"], 2)
        self.assertEqual(hour["peakConcurrent"], 1)
        self.assertTrue(hour["limited"])
        self.assertEqual(analytics._db.execute("SELECT count(*) FROM current_visitors").fetchone()[0], 0)

    def test_local_date_ranges_follow_spring_and_autumn_dst(self):
        for now, day, count, first, last in (
            (datetime(2026, 3, 28, 12, tzinfo=timezone.utc), "2026-03-27", 23,
             "2026-03-26T22:00:00Z", "2026-03-27T21:00:00Z"),
            (datetime(2026, 10, 26, 12, tzinfo=timezone.utc), "2026-10-25", 25,
             "2026-10-24T21:00:00Z", "2026-10-25T22:00:00Z"),
        ):
            with self.subTest(day=day):
                self.clock.return_value = now
                analytics = self.create()
                result = analytics.snapshot(start=day, end=day)
                self.assertEqual(len(result["hours"]), count)
                self.assertEqual(result["rangeStart"], first)
                self.assertEqual(result["rangeEnd"], last)
                self.assertEqual(len({hour["start"] for hour in result["hours"]}), count)

    def test_invalid_ranges_and_default_range(self):
        analytics = self.create()
        self.assertEqual(len(analytics.snapshot()["hours"]), 24)
        for start, end in (("2026-10-24", None), (None, "2026-10-24"), ("", ""),
                           ("20261024", "20261024"), ("2026-02-30", "2026-10-24"),
                           ("2026-10-24", "2026-10-23"), ("2026-07-01", "2026-10-24"),
                           ("2026-07-25", "2026-07-25"), ("2026-10-25", "2026-10-25")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                analytics.snapshot(start=start, end=end)

    def test_backward_clock_does_not_duplicate_coverage(self):
        analytics = self.create()
        self.at(60)
        analytics.tick()
        self.at(30)
        self.assertFalse(analytics.record(A))
        with self.assertRaises(AnalyticsUnavailable):
            analytics.snapshot()
        self.at(90)
        hour = analytics.snapshot()["hours"][-1]
        self.assertEqual(hour["coverageSeconds"], 90)
        self.assertEqual(hour["averageConcurrent"], 0)

    def test_forward_clock_jump_and_stalled_collection_leave_unknown_hours(self):
        steady = Mock(return_value=0)
        analytics = self.create(monotonic_clock=steady)
        analytics.record(A)
        self.at(30)
        steady.return_value = 30
        analytics.tick()
        self.at(7230)
        steady.return_value = 60
        analytics.tick()
        result = analytics.snapshot()
        self.assertEqual(result["activeNow"], 0)
        self.assertEqual(result["hours"][-3]["coverageSeconds"], 30)
        self.assertIsNone(result["hours"][-2]["averageConcurrent"])
        self.assertEqual(result["hours"][-1]["coverageSeconds"], 0)
        self.at(7530)
        steady.return_value = 360
        analytics.tick()
        self.assertEqual(analytics.snapshot()["hours"][-1]["coverageSeconds"], 0)

    def test_restart_with_backward_clock_never_overlaps_saved_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analytics.sqlite3"
            first = VisitorAnalytics(path, clock=self.clock,
                                     monotonic_clock=lambda: self.clock.return_value.timestamp())
            first.record(A)
            self.at(60)
            first.close()
            self.at(30)
            second = VisitorAnalytics(path, clock=self.clock,
                                      monotonic_clock=lambda: self.clock.return_value.timestamp())
            try:
                self.assertFalse(second.record(A))
                with self.assertRaises(AnalyticsUnavailable):
                    second.snapshot()
                self.at(90)
                result = second.snapshot()
                self.assertEqual(result["hours"][-1]["coverageSeconds"], 90)
                self.assertEqual(result["hours"][-1]["averageConcurrent"], 0.667)
                self.assertEqual(result["hours"][-1]["uniqueVisitors"], 1)
            finally:
                second.close()

    def test_storage_failure_stops_collection_and_close_is_idempotent(self):
        analytics = self.create()
        analytics._db.close()
        self.at(3600)
        with self.assertRaises(sqlite3.ProgrammingError):
            analytics.tick()
        with self.assertRaises(AnalyticsUnavailable):
            analytics.snapshot()
        analytics.close()
        analytics.close()

    def test_background_worker_stops_and_persists_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            analytics = VisitorAnalytics(Path(directory) / "analytics.sqlite3", clock=self.clock, background=True,
                                         monotonic_clock=lambda: self.clock.return_value.timestamp())
            worker = analytics._thread
            self.assertTrue(worker.is_alive())
            analytics.record(A)
            self.at(15)
            analytics.close()
            self.assertFalse(worker.is_alive())
            with self.assertRaises(AnalyticsUnavailable):
                analytics.record(A)


if __name__ == "__main__":
    unittest.main()
