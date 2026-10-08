import json
import struct
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from elevator import diagnostics
from elevator.diagnostics import Diagnostics, OWNER


EPOCH_NS = 1_800_000_000_000_000_000


def pcm(values):
    return struct.pack(f"<{len(values)}h", *values)


def events(directory):
    return [json.loads(line) for path in sorted((Path(directory) / "logs").glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


def managed_recording(directory, epoch_ms, size):
    folder = Path(directory) / "recordings"
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / f"{epoch_ms}.wav"
    wav.write_bytes(b"x" * size)
    wav.with_suffix(".json").write_text(json.dumps({"managedBy": OWNER}), encoding="utf-8")
    return wav


class FakeDisk:
    """Capacity accounts for real fixture writes/deletions, without a full disk."""

    def __init__(self, folder, *, total=1_000_000, external_used=0):
        self.folder = Path(folder)
        self.total = total
        self.external_used = external_used

    def __call__(self, _path):
        used = self.external_used + sum(path.stat().st_size for path in self.folder.rglob("*")
                                        if path.is_file() and not path.is_symlink())
        return SimpleNamespace(total=self.total, free=self.total - used, used=used)


class SynchronousDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "diagnostics"
        self.headroom = patch.object(diagnostics, "HEADROOM_BYTES", 1024)
        self.headroom.start()
        self.addCleanup(self.headroom.stop)
        self.start_patch = patch.object(threading.Thread, "start")
        self.start_patch.start()
        self.addCleanup(self.start_patch.stop)

    def storage(self, **kwargs):
        result = Diagnostics(self.folder, **kwargs)
        result._initialize()
        self.addCleanup(lambda: result._close_wave(emit=False))
        return result

    def test_oldest_managed_recording_reclaimed_before_write_and_reserve_preserved(self):
        oldest = managed_recording(self.folder, 1700000000000, 20000)
        newest = managed_recording(self.folder, 1700000100000, 20000)
        foreign = self.folder / "recordings" / "1700000200000.wav"
        foreign.write_bytes(b"foreign recording")
        disk = FakeDisk(self.folder)
        with patch.object(diagnostics.shutil, "disk_usage", disk):
            store = self.storage()
            # Available allowance is insufficient for 16k PCM plus safety slack.
            disk.external_used = disk(self.folder).free - (200000 + 1024 + 14000)
            store._consume_audio(pcm([123] * 8000), 8000, EPOCH_NS, "session")
            self.assertFalse(oldest.exists())
            self.assertFalse(oldest.with_suffix(".json").exists())
            self.assertTrue(newest.exists())
            self.assertEqual(foreign.read_bytes(), b"foreign recording")
            self.assertGreaterEqual(disk(self.folder).free, disk.total * .2 + 1024)
            self.assertTrue(list(store.recordings.glob("1800000000000.wav")))

    def test_insufficient_unreclaimable_space_pauses_and_recovers_with_explicit_gap(self):
        disk = FakeDisk(self.folder)
        with patch.object(diagnostics.shutil, "disk_usage", disk):
            store = self.storage()
            disk.external_used = disk(self.folder).free - (200000 + 1024 + 1000)
            store._consume_audio(pcm([5] * 1000), 1000, EPOCH_NS, "a")
            self.assertTrue(store.status()["storagePaused"])
            self.assertEqual(list(store.recordings.glob("*.wav")), [])
            self.assertGreaterEqual(disk(self.folder).free, disk.total * .2)
            disk.external_used = 0
            store._consume_audio(pcm([6] * 1000), 1000, EPOCH_NS + diagnostics.NS, "a")
            store._close_wave()
            self.assertFalse(store.status()["storagePaused"])
            gaps = [row for row in events(self.folder) if row["type"] == "audio_storage_gap"]
            self.assertEqual(gaps[0]["droppedFrames"], 1000)
            path = store.recordings / "1800000001000.wav"
            with wave.open(str(path), "rb") as recording:
                self.assertEqual(recording.readframes(1000), pcm([6] * 1000))

    def test_quota_finalizes_and_deletes_active_file_before_reusing_capacity(self):
        store = self.storage(max_recording_bytes=12000)
        store._consume_audio(pcm([1] * 1000), 1000, EPOCH_NS, "s")
        first = store._wave_path
        self.assertTrue(first.exists())
        store._consume_audio(pcm([2] * 1000), 1000, EPOCH_NS + diagnostics.NS, "s")
        self.assertFalse(first.exists())
        self.assertEqual(store._wave_path.stem, "1800000001000")
        self.assertLessEqual(store.status()["recordingBytes"], 12000)

    def test_pcm_amplitude_header_and_chunk_timestamps_remain_accurate(self):
        store = self.storage()
        values = [-32768, -100, 0, 123, 32767, 42, 75, -22, 9, 11, 15, -15]
        with patch.object(diagnostics, "CHUNK_SECONDS", 1):
            store._consume_audio(pcm(values), 10, EPOCH_NS, "s")
            # The final 2 samples are active yet already readable, including header.
            with wave.open(str(store._wave_path), "rb") as active:
                self.assertEqual(active.getnframes(), 2)
                self.assertEqual(active.readframes(2), pcm(values[10:]))
            store._close_wave()
        paths = sorted(store.recordings.glob("*.wav"))
        self.assertEqual([path.stem for path in paths], ["1800000000000", "1800000001000"])
        joined = b""
        for path in paths:
            with wave.open(str(path), "rb") as recording:
                self.assertEqual(recording.getparams()[:3], (1, 2, 10))
                joined += recording.readframes(recording.getnframes())
            meta = json.loads(path.with_suffix(".json").read_text())
            self.assertTrue(meta["closed"])
            self.assertEqual(meta["runId"], store.run_id)
        self.assertEqual(joined, pcm(values))

    def test_timestamp_gap_creates_separate_files_instead_of_filling_or_mislabeling(self):
        store = self.storage()
        store._consume_audio(pcm([1] * 5), 10, EPOCH_NS, "s")
        store._consume_audio(pcm([2] * 5), 10, EPOCH_NS + 2 * diagnostics.NS, "s")
        store._flush_audio()
        store._close_wave()
        paths = sorted(store.recordings.glob("*.wav"))
        self.assertEqual([p.stem for p in paths], ["1800000000000", "1800000002000"])
        for path in paths:
            with wave.open(str(path), "rb") as recording:
                self.assertEqual(recording.getnframes(), 5)
        gaps = [row for row in events(self.folder) if row["type"] == "audio_gap"]
        self.assertEqual(gaps[0]["gapSeconds"], 1.5)

    def test_same_timestamp_collision_cannot_overwrite_existing_audio(self):
        store = self.storage()
        path = store.recordings / "1800000000000.wav"
        path.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            store._consume_audio(pcm([0] * 10), 10, EPOCH_NS, "s")
        self.assertEqual(path.read_bytes(), b"do not overwrite")
        self.assertFalse(path.with_suffix(".json").exists())

    def test_interrupted_metadata_replace_keeps_original_ownership_and_can_reclaim(self):
        store = self.storage()
        store._consume_audio(pcm([1] * 10), 10, EPOCH_NS, "s")
        path, meta = store._wave_path, store._meta_path
        original_metadata = meta.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("power interrupted before rename")):
            store._close_wave()
        self.assertEqual(meta.read_bytes(), original_metadata)
        self.assertFalse(json.loads(meta.read_text())["closed"])
        self.assertTrue(meta.with_suffix(".json.tmp").exists())
        store._scan()
        self.assertFalse(meta.with_suffix(".json.tmp").exists())
        self.assertTrue(store._delete_recording())
        self.assertFalse(path.exists())

    def test_metadata_sync_failure_does_not_hide_orphan_from_reclamation(self):
        store = self.storage()
        with patch.object(diagnostics.os, "fsync", side_effect=OSError("sync failed")):
            with self.assertRaises(OSError):
                store._consume_audio(pcm([1] * 10), 10, EPOCH_NS, "s")
        store._close_wave(emit=False)
        self.assertIsNone(store._wave_path)
        self.assertTrue(store._delete_recording())
        self.assertEqual(list(store.recordings.iterdir()), [])

    def test_nonobject_metadata_does_not_block_scan_or_cleanup(self):
        for index, content in enumerate(("null", "[]", "42", "{invalid")):
            record = managed_recording(self.folder, 1700000000000 + index, 10)
            record.with_suffix(".json").write_text(content)
        owned = managed_recording(self.folder, 1700000001000, 10)
        store = self.storage()
        self.assertTrue(store._delete_recording())
        self.assertFalse(owned.exists())
        self.assertEqual(len(list(store.recordings.glob("*.wav"))), 4)

    def test_parent_reparse_point_recheck_prevents_deleting_outside_managed_tree(self):
        recording = managed_recording(self.folder, 1700000000000, 20)
        store = self.storage()
        real_plain = diagnostics._plain

        def reject_replaced_parent(path, **kwargs):
            if path == self.folder:
                raise OSError("Diagnostic parent was replaced with a junction")
            return real_plain(path, **kwargs)

        with patch.object(diagnostics, "_plain", reject_replaced_parent):
            self.assertFalse(store._delete_recording())
        self.assertTrue(recording.exists())

    def test_reparse_file_is_rejected_without_following_it(self):
        path = self.folder / "example"
        fake = SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=fake):
            with self.assertRaises(OSError):
                diagnostics._plain(path)

    def test_housekeeping_reclaims_space_even_without_audio(self):
        recording = managed_recording(self.folder, 1700000000000, 20000)
        disk = FakeDisk(self.folder)
        with patch.object(diagnostics.shutil, "disk_usage", disk):
            store = self.storage(recording_enabled=False)
            disk.external_used = disk(self.folder).free - 195000
            store._housekeeping()
            self.assertFalse(recording.exists())
            self.assertGreaterEqual(disk(self.folder).free, disk.total * .2 + 1024)

    def test_log_expiration_and_cap_keep_nonmanaged_files(self):
        store = self.storage()
        own = store.logs / "events-20000101-1000000000000-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jsonl"
        own.write_text("old", encoding="utf-8")
        import os
        os.utime(own, (1, 1))
        foreign = store.logs / "keep.jsonl"
        foreign.write_text("user's log", encoding="utf-8")
        store._scan()
        store._housekeeping()
        self.assertFalse(own.exists())
        self.assertTrue(foreign.exists())
        with patch.object(diagnostics, "MAX_LOG_BYTES", 2500), patch.object(diagnostics, "LOG_CHUNK_BYTES", 600):
            for _ in range(20):
                store._write_event("measurement", content="a" * 250)
            self.assertLessEqual(sum(path.stat().st_size for path in store.logs.glob("events-*.jsonl")), 2500)

    def test_symlink_and_foreign_metadata_are_not_reclaimed(self):
        store = self.storage()
        outside = Path(self.temp.name) / "outside.wav"
        outside.write_bytes(b"private recording")
        link = store.recordings / "1700000000000.wav"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Creating symlinks requires additional Windows privileges")
        link.with_suffix(".json").write_text(json.dumps({"managedBy": OWNER}), encoding="utf-8")
        foreign = managed_recording(self.folder, 1700000001000, 100)
        foreign.with_suffix(".json").write_text('{"managedBy":"someone-else"}')
        store._scan()
        self.assertFalse(store._delete_recording())
        self.assertEqual(outside.read_bytes(), b"private recording")
        self.assertTrue(link.is_symlink())
        self.assertTrue(foreign.exists())

    def test_recording_directory_link_is_rejected(self):
        self.folder.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            (self.folder / "recordings").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires additional Windows privileges")
        store = Diagnostics(self.folder)
        with self.assertRaises(OSError):
            store._initialize()
        self.assertEqual(list(outside.iterdir()), [])

    def test_recording_disabled_still_logs_events_with_same_run_id(self):
        store = self.storage(recording_enabled=False)
        store.audio(pcm([1] * 10), sample_rate=10, start_ns=EPOCH_NS, session_id="x")
        self.assertTrue(store._queue.empty())
        store._write_event("detector_start", calibration="a")
        self.assertTrue(all(row["runId"] == store.run_id for row in events(self.folder)))

    def test_wav_metadata_keeps_calibration_when_original_log_is_gone(self):
        store = self.storage()
        config = {"floor": 7, "detector": {"frequencyLowHz": 3450, "minimumEventBandDbfs": -63.6}}
        store._write_event("detector_start", config=config, configHash="abc", codeHash="def", device="USB mic")
        # A metadata snapshot must not change if the caller changes its object.
        config["floor"] = 99
        for log in store.logs.glob("*.jsonl"):
            log.unlink()
        store._log_path = None
        store._scan()
        store._consume_audio(pcm([100] * 10), 10, EPOCH_NS, "s")
        store._close_wave()
        meta = json.loads((store.recordings / "1800000000000.json").read_text())
        self.assertEqual(meta["detector"]["config"]["floor"], 7)
        self.assertEqual(meta["detector"]["configHash"], "abc")
        self.assertEqual(meta["detector"]["codeHash"], "def")
        self.assertEqual(meta["detector"]["device"], "USB mic")


class AsynchronousDiagnosticsTests(unittest.TestCase):
    def test_capture_stop_finalizes_short_tail_without_stopping_diagnostic_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Diagnostics(folder)
            try:
                store.audio(pcm([8] * 5), sample_rate=10, start_ns=EPOCH_NS, session_id="a")
                store.event("capture_stop", sessionId="a")
                store._queue.join()
                self.assertTrue(store._worker.is_alive())
                self.assertIsNone(store._wave)
                path = Path(folder) / "recordings" / "1800000000000.wav"
                with wave.open(str(path), "rb") as recording:
                    self.assertEqual(recording.readframes(5), pcm([8] * 5))
                self.assertTrue(json.loads(path.with_suffix(".json").read_text())["closed"])
            finally:
                store.close()

    def test_slow_disk_never_blocks_enqueue_and_queue_drops_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            entered, release, returned = threading.Event(), threading.Event(), threading.Event()
            original = Diagnostics._initialize

            def blocked_initialize(store):
                entered.set()
                release.wait(5)
                original(store)

            with patch.object(Diagnostics, "_initialize", blocked_initialize):
                store = Diagnostics(folder, queue_blocks=2)
                self.assertTrue(entered.wait(1))

                def producer():
                    for index in range(100):
                        store.audio(pcm([index] * 10), sample_rate=10,
                                    start_ns=EPOCH_NS + index * diagnostics.NS, session_id="a")
                    returned.set()

                producer_thread = threading.Thread(target=producer)
                producer_thread.start()
                try:
                    self.assertTrue(returned.wait(1), "Audio producer waited for filesystem work")
                    self.assertEqual(store.status()["queueDepth"], 2)
                    self.assertEqual(store.status()["queueDrops"], 98)
                finally:
                    release.set()
                    producer_thread.join(2)
                    store.close()
                self.assertFalse(store._worker.is_alive())
                self.assertTrue(any(row["type"] == "diagnostic_queue_gap" for row in events(folder)))

    def test_write_failure_does_not_escape_to_detector_and_next_data_can_recover(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Diagnostics._open_wave
            failed = threading.Event()

            def fail_once(store, start_ns):
                if not failed.is_set():
                    failed.set()
                    raise OSError("simulated disk write failure")
                original(store, start_ns)

            with patch.object(Diagnostics, "_open_wave", fail_once):
                store = Diagnostics(folder)
                try:
                    store.audio(pcm([1] * 10), sample_rate=10, start_ns=EPOCH_NS, session_id="a")
                    self.assertTrue(failed.wait(2))
                    store.audio(pcm([2] * 10), sample_rate=10,
                                start_ns=EPOCH_NS + diagnostics.NS, session_id="a")
                finally:
                    store.close()
            self.assertFalse(store._worker.is_alive())
            paths = list((Path(folder) / "recordings").glob("*.wav"))
            self.assertEqual([path.stem for path in paths], ["1800000001000"])
            with wave.open(str(paths[0]), "rb") as recording:
                self.assertEqual(recording.readframes(10), pcm([2] * 10))


if __name__ == "__main__":
    unittest.main()
