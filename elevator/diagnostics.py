"""Private, bounded live diagnostics; capture never waits for filesystem work.

Only files created by this module are reclaimed. WAVs use the timestamp naming
and unmodified mono PCM16 format understood by ``run_calibrate_audio.py``.
Deletion is oldest first, before writes, keeping 20% of the filesystem free plus
headroom. Other processes can consume that reserve independently; housekeeping
reacts within five seconds and pauses recording if reclamation is insufficient.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import shutil
import stat
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path


LOG = logging.getLogger(__name__)
OWNER = "shabat-elevator-diagnostics-v1"
NS = 1_000_000_000
HEADROOM_BYTES = 32 * 1024 * 1024
MAX_LOG_BYTES = 100 * 1024 * 1024
LOG_CHUNK_BYTES = 1024 * 1024
CHUNK_SECONDS = 300
HOUSEKEEPING_SECONDS = 5
_META_NAME = re.compile(r"\d{13}\.json\Z")
_LOG_NAME = re.compile(r"events-\d{8}-\d{13}-[0-9a-f]{32}\.jsonl\Z")


def _utc(ns):
    return datetime.fromtimestamp(ns / NS, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _plain(path, *, directory=False):
    """Reject links/reparse points rather than following them during cleanup."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise OSError(f"Diagnostic path is a link: {path}")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise OSError(f"Unexpected diagnostic file type: {path}")
    return info


def _safe_directory(path):
    _check_ancestors(path)
    path.mkdir(parents=True, exist_ok=True)
    _plain(path, directory=True)
    return path


def _check_ancestors(path):
    # Recheck the whole parent chain before mutations, including after startup.
    for candidate in reversed((path, *path.parents)):
        if candidate.exists() or candidate.is_symlink():
            _plain(candidate, directory=True)


def _sync_directory(path):
    """Persist ownership renames on Linux; directory fsync is not on Windows."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Diagnostics:
    """A bounded asynchronous event log and rolling recording store.

    ``audio`` takes little-endian mono PCM16 and the UTC nanosecond timestamp of
    its first sample. Blocks must use timestamps derived from the original ADC
    anchor plus sample count, not their delivery time. A gap starts a new WAV.
    Neither public enqueue method raises on storage/serialization failures.
    """

    def __init__(self, directory, *, max_recording_bytes=16_000_000_000,
                 retention_days=30, queue_blocks=512, recording_enabled=True):
        self.directory = Path(directory).absolute()
        self.recordings = self.directory / "recordings"
        self.logs = self.directory / "logs"
        self.max_recording_bytes = max(0, int(max_recording_bytes))
        self.retention_days = max(1, int(retention_days))
        self.recording_enabled = bool(recording_enabled)
        self.run_id = uuid.uuid4().hex
        self._queue = queue.Queue(maxsize=max(1, int(queue_blocks)))
        self._stop = threading.Event()
        self._dropped = 0
        self._reported_dropped = 0
        self._last_housekeeping = 0.0
        self._last_scan = 0.0
        self._last_error = 0.0
        self._ready = False
        self._paused = False
        self._recording_files = []
        self._log_files = []
        self._recording_bytes = 0
        self._log_bytes = 0
        self._log_path = None
        self._log_day = None
        self._log_size = 0
        self._session = None
        self._rate = None
        self._next_ns = None
        self._pending = bytearray()
        self._pending_ns = None
        self._wave = None
        self._raw = None
        self._wave_path = None
        self._meta_path = None
        self._wave_start_ns = None
        self._wave_frames = 0
        self._wave_accounted = 0
        self._meta_accounted = 0
        self._free_bytes = None
        self._total_bytes = None
        self._storage_gap = None
        self._detector_context = {}
        self._worker = threading.Thread(target=self._run, name="elevator-diagnostics", daemon=True)
        self._worker.start()

    def _enqueue(self, item):
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._dropped += 1

    def event(self, kind, **fields):
        self._enqueue(("event", time.time_ns(), str(kind), fields))

    def audio(self, pcm, *, sample_rate, start_ns, session_id):
        if not self.recording_enabled or self._stop.is_set():
            return
        try:
            # A malformed/oversized producer cannot make the queue unbounded.
            if not pcm or len(pcm) % 2 or len(pcm) > 65536 or not 1 <= sample_rate <= 192000:
                self._dropped += 1
                return
            self._enqueue(("audio", bytes(pcm), int(sample_rate), int(start_ns), str(session_id)))
        except (TypeError, ValueError, OverflowError):
            self._dropped += 1

    def status(self):
        """Snapshot only; no filesystem access or waiting on the writer."""
        return {"runId": self.run_id, "recordingEnabled": self.recording_enabled,
                "storageReady": self._ready, "storagePaused": self._paused,
                "queueDrops": self._dropped, "queueDepth": self._queue.qsize(),
                "recordingBytes": self._recording_bytes, "logBytes": self._log_bytes,
                "diskFreeBytes": self._free_bytes, "diskTotalBytes": self._total_bytes,
                "minimumFreeFraction": .2, "maxRecordingBytes": self.max_recording_bytes}

    def close(self):
        self._stop.set()
        self._worker.join(timeout=5)
        if self._worker.is_alive():
            LOG.warning("Diagnostic writer is still closing; capture has already stopped")

    def _error(self, error):
        now = time.monotonic()
        if now - self._last_error >= 30:
            LOG.warning("Diagnostic storage failure; detection continues: %s", error)
            self._last_error = now

    def _run(self):
        try:
            self._initialize()
        except Exception as error:
            self._error(error)
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=.25)
            except queue.Empty:
                item = None
            try:
                if not self._ready:
                    if time.monotonic() - self._last_housekeeping >= HOUSEKEEPING_SECONDS:
                        self._last_housekeeping = time.monotonic()
                        self._initialize()
                    if not self._ready:
                        continue
                if item:
                    if item[0] == "event":
                        if item[2] == "capture_stop":
                            self._flush_audio()
                            self._close_wave()
                            self._next_ns = None
                        self._write_event(item[2], at_ns=item[1], **item[3])
                    else:
                        self._consume_audio(*item[1:])
                now = time.monotonic()
                if now - self._last_housekeeping >= HOUSEKEEPING_SECONDS:
                    self._last_housekeeping = now
                    self._housekeeping()
            except Exception as error:
                self._error(error)
                # Never join two sides of a failed write into a continuous WAV.
                self._pending.clear()
                self._next_ns = None
                self._close_wave(emit=False)
                self._paused = True
            finally:
                if item:
                    self._queue.task_done()
        try:
            if self._ready:
                self._flush_audio()
                self._close_wave()
                self._write_event("diagnostics_stop", queueDrops=self._dropped)
        except Exception as error:
            self._error(error)
            self._close_wave(emit=False)

    def _initialize(self):
        _safe_directory(self.directory)
        _safe_directory(self.recordings)
        _safe_directory(self.logs)
        self._scan()
        self._ready = True
        self._housekeeping()
        self._write_event("diagnostics_start", recordingEnabled=self.recording_enabled,
                          maxRecordingBytes=self.max_recording_bytes,
                          minimumFreeFraction=.2, headroomBytes=HEADROOM_BYTES,
                          logRetentionDays=self.retention_days, maxLogBytes=MAX_LOG_BYTES)

    def _scan(self):
        _plain(self.recordings, directory=True)
        _plain(self.logs, directory=True)
        records = []
        total = 0
        for meta in self.recordings.iterdir():
            if not _META_NAME.fullmatch(meta.name):
                continue
            try:
                info = _plain(meta)
                if info.st_size > 16384:
                    continue
                content = json.loads(meta.read_text(encoding="utf-8"))
                if not isinstance(content, dict) or content.get("managedBy") != OWNER:
                    continue
                wav = meta.with_suffix(".wav")
                temporary = meta.with_suffix(".json.tmp")
                if wav != self._wave_path and (temporary.exists() or temporary.is_symlink()):
                    _check_ancestors(self.recordings)
                    _plain(temporary)
                    temporary.unlink()
                size = info.st_size
                if wav.exists() or wav.is_symlink():
                    size += _plain(wav).st_size
                if wav == self._wave_path:
                    continue
                records.append((int(meta.stem), wav, meta, size))
                total += size
            except (OSError, ValueError, TypeError):
                continue
        logs = []
        log_total = 0
        for path in self.logs.iterdir():
            if not _LOG_NAME.fullmatch(path.name):
                continue
            try:
                info = _plain(path)
                logs.append((info.st_mtime, path, info.st_size))
                log_total += info.st_size
            except OSError:
                continue
        self._recording_files = sorted(records)
        self._recording_bytes = total + self._wave_accounted + self._meta_accounted
        self._log_files = sorted(logs)
        self._log_bytes = log_total
        self._last_scan = time.monotonic()

    def _disk(self):
        usage = shutil.disk_usage(self.directory)
        self._free_bytes, self._total_bytes = usage.free, usage.total
        return usage.free - math.ceil(usage.total * .2) - HEADROOM_BYTES

    def _delete_recording(self):
        while self._recording_files:
            _, wav, meta, size = self._recording_files.pop(0)
            try:
                _check_ancestors(self.recordings)
                if _plain(meta).st_size > 16384:
                    continue
                # Recheck ownership and links at deletion, not only at inventory.
                content = json.loads(meta.read_text(encoding="utf-8"))
                if not isinstance(content, dict) or content.get("managedBy") != OWNER:
                    continue
                if wav.exists() or wav.is_symlink():
                    _plain(wav)
                    wav.unlink()
                temporary = meta.with_suffix(".json.tmp")
                if temporary.exists() or temporary.is_symlink():
                    _plain(temporary)
                    temporary.unlink()
                meta.unlink()
                self._recording_bytes = max(0, self._recording_bytes - size)
                return True
            except (OSError, ValueError) as error:
                self._error(error)
        return False

    def _delete_log(self, *, expired_before=None):
        for index, (modified, path, size) in enumerate(self._log_files):
            if path == self._log_path or (expired_before is not None and modified >= expired_before):
                continue
            self._log_files.pop(index)
            try:
                _check_ancestors(self.logs)
                _plain(path)
                path.unlink()
                self._log_bytes = max(0, self._log_bytes - size)
                return True
            except OSError as error:
                self._error(error)
                return self._delete_log(expired_before=expired_before)
        return False

    def _space(self, needed, *, recording=False):
        if recording:
            while self._recording_bytes + needed > self.max_recording_bytes:
                if not self._delete_recording():
                    # Finalize the active file before considering it for deletion.
                    if self._wave is not None:
                        self._close_wave(emit=False)
                        continue
                    return False
        while self._disk() < needed:
            if self._delete_recording() or self._delete_log():
                continue
            if self._wave is not None:
                self._close_wave(emit=False)
                continue
            if self._log_path is not None:
                # Logs are opened only for each append, so the current segment
                # can be retired and reclaimed just like any completed log.
                self._log_path = None
                self._log_size = 0
                continue
            return False
        return True

    def _storage_state(self, paused):
        if paused != self._paused:
            self._paused = paused
            LOG.warning("Diagnostic recording %s; oldest managed recordings reclaimed as needed",
                        "paused because storage is low" if paused else "resumed")

    def _housekeeping(self):
        if time.monotonic() - self._last_scan >= 60:
            self._scan()
        cutoff = time.time() - self.retention_days * 86400
        while self._delete_log(expired_before=cutoff):
            pass
        while self._log_bytes > MAX_LOG_BYTES and self._delete_log():
            pass
        can_write = self._space(4096, recording=self.recording_enabled)
        self._storage_state(not can_write)
        if self._reported_dropped != self._dropped:
            dropped = self._dropped
            LOG.warning("Diagnostic queue dropped %s blocks/events; capture continued", dropped - self._reported_dropped)
            self._write_event("diagnostic_queue_gap", droppedItems=dropped - self._reported_dropped,
                              totalDroppedItems=dropped)
            self._reported_dropped = dropped

    def _write_event(self, kind, *, at_ns=None, **fields):
        at_ns = time.time_ns() if at_ns is None else at_ns
        if kind == "detector_start":
            context = {name: fields[name] for name in ("config", "configHash", "codeHash", "device",
                       "deviceId", "sampleRate", "pythonVersion", "monitorOnly") if name in fields}
            snapshot = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(snapshot.encode("utf-8")) > 4096:
                raise ValueError("Diagnostic detector configuration exceeds 4 KiB")
            self._detector_context = json.loads(snapshot)
        record = {**fields, "type": kind, "atUtc": _utc(at_ns), "atEpochNs": at_ns,
                  "runId": self.run_id}
        encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("Diagnostic event exceeds 64 KiB")
        day = datetime.fromtimestamp(at_ns / NS, timezone.utc).strftime("%Y%m%d")
        if self._log_day != day or self._log_size + len(encoded) > LOG_CHUNK_BYTES:
            self._log_path = None
            self._log_size = 0
            self._log_day = day
        while self._log_bytes + len(encoded) > MAX_LOG_BYTES:
            if not self._delete_log():
                return
        if not self._space(len(encoded) + 4096):
            return
        _check_ancestors(self.logs)
        if self._log_path is None:
            self._log_path = self.logs / f"events-{day}-{time.time_ns() // 1_000_000:013d}-{uuid.uuid4().hex}.jsonl"
            with self._log_path.open("xb") as output:
                output.write(encoded)
            self._log_files.append((time.time(), self._log_path, len(encoded)))
        else:
            if _plain(self._log_path).st_nlink != 1:
                raise OSError("Refusing to append to a linked diagnostic log")
            with self._log_path.open("ab") as output:
                output.write(encoded)
            for index, (modified, path, size) in enumerate(self._log_files):
                if path == self._log_path:
                    self._log_files[index] = (modified, path, size + len(encoded))
                    break
        self._log_size += len(encoded)
        self._log_bytes += len(encoded)

    def _consume_audio(self, pcm, rate, start_ns, session):
        difference = None if self._next_ns is None else start_ns - self._next_ns
        gap = difference is not None and abs(difference) > max(2 * NS // rate, 2)
        changed = session != self._session or rate != self._rate
        if gap or changed:
            self._flush_audio()
            self._close_wave()
            if gap:
                self._write_event("audio_gap", sessionId=session, expectedEpochNs=self._next_ns,
                                  actualEpochNs=start_ns, gapSeconds=difference / NS)
            if changed:
                self._write_event("audio_session", sessionId=session, sampleRate=rate,
                                  channels=1, sampleWidth=2, format="PCM_16", startEpochNs=start_ns)
        self._session, self._rate = session, rate
        if not self._pending:
            self._pending_ns = start_ns
        self._pending.extend(pcm)
        self._next_ns = start_ns + round(len(pcm) // 2 * NS / rate)
        if len(self._pending) >= rate * 2:
            self._flush_audio()

    def _metadata(self, *, complete=False):
        return {"managedBy": OWNER, "runId": self.run_id, "sessionId": self._session,
                "detector": self._detector_context,
                "file": self._wave_path.name, "startEpochNs": self._wave_start_ns,
                "startEpochMs": self._wave_start_ns // 1_000_000,
                "startUtc": _utc(self._wave_start_ns),
                "endUtc": _utc(self._wave_start_ns + round(self._wave_frames * NS / self._rate)),
                "endExclusive": True, "frames": self._wave_frames,
                "sampleRate": self._rate, "channels": 1, "sampleWidth": 2,
                "format": "PCM_16", "closed": complete,
                "timestampSource": "original_capture_utc_anchor_plus_sample_count"}

    def _open_wave(self, start_ns):
        self._wave_path = self.recordings / f"{start_ns // 1_000_000:013d}.wav"
        self._meta_path = self._wave_path.with_suffix(".json")
        self._wave_start_ns = start_ns
        self._wave_frames = 0
        encoded = json.dumps(self._metadata(), separators=(",", ":")).encode("utf-8")
        _check_ancestors(self.recordings)
        # Exclusive creation: collisions never overwrite a distinct recording.
        if self._wave_path.exists() or self._wave_path.is_symlink():
            raise FileExistsError(f"Recording already exists: {self._wave_path}")
        with self._meta_path.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        _sync_directory(self.recordings)
        self._meta_accounted = len(encoded)
        self._recording_bytes += len(encoded)
        try:
            self._raw = self._wave_path.open("xb")
            self._wave = wave.open(self._raw, "wb")
            self._wave.setnchannels(1)
            self._wave.setsampwidth(2)
            self._wave.setframerate(self._rate)
        except Exception:
            if self._raw is not None:
                self._raw.close()
                self._raw = None
            # Our metadata is a valid ownership record for a partial file; the
            # next inventory can reclaim it, without touching colliding files.
            self._wave_path = None
            self._meta_path = None
            self._meta_accounted = 0
            self._scan()
            raise
        self._wave_accounted = 0

    def _flush_audio(self):
        if not self._pending:
            return
        data, start_ns = bytes(self._pending), self._pending_ns
        self._pending.clear()
        offset = 0
        while offset < len(data):
            room_frames = self._rate * CHUNK_SECONDS - self._wave_frames if self._wave is not None else self._rate * CHUNK_SECONDS
            count = min(len(data) - offset, room_frames * 2)
            # Reserve metadata/header/allocation slack too. Recheck every ~second.
            if not self._space(count + 8192, recording=True):
                self._storage_state(True)
                self._close_wave(emit=False)
                missed_start = start_ns + round(offset // 2 * NS / self._rate)
                missed_end = start_ns + round(len(data) // 2 * NS / self._rate)
                if self._storage_gap is None:
                    self._storage_gap = {"startEpochNs": missed_start, "endEpochNs": missed_end,
                                         "droppedFrames": (len(data) - offset) // 2,
                                         "sessionId": self._session}
                else:
                    self._storage_gap["endEpochNs"] = missed_end
                    self._storage_gap["droppedFrames"] += (len(data) - offset) // 2
                return
            self._storage_state(False)
            if self._storage_gap is not None:
                gap, self._storage_gap = self._storage_gap, None
                self._write_event("audio_storage_gap", **gap)
                # Logging itself may reclaim the active WAV. Reserve again.
                if not self._space(count + 8192, recording=True):
                    self._storage_gap = gap
                    self._storage_state(True)
                    return
            if self._wave is None:
                self._open_wave(start_ns + round(offset // 2 * NS / self._rate))
            self._wave.writeframes(data[offset:offset + count])
            self._raw.flush()  # writeframes also updates the WAV header each batch.
            self._wave_frames += count // 2
            current_size = 44 + self._wave_frames * 2
            self._recording_bytes += current_size - self._wave_accounted
            self._wave_accounted = current_size
            offset += count
            if self._wave_frames >= self._rate * CHUNK_SECONDS:
                self._close_wave()

    def _close_wave(self, *, emit=True):
        if self._wave is None:
            had_partial_file = self._wave_path is not None or self._raw is not None
            if self._raw is not None:
                try:
                    self._raw.close()
                except OSError:
                    pass
                self._raw = None
            self._wave_path = self._meta_path = None
            self._wave_frames = self._wave_accounted = self._meta_accounted = 0
            if had_partial_file:
                try:
                    self._scan()
                except Exception as error:
                    self._error(error)
            return
        path, meta = self._wave_path, self._meta_path
        metadata = None
        try:
            metadata = self._metadata(complete=True)
            self._wave.close()
            self._raw.flush()
            os.fsync(self._raw.fileno())
            self._raw.close()
            # Header stays readable after each write; metadata completion is best
            # effort and uses already-reserved slack, never recursive reclamation.
            encoded = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
            _check_ancestors(self.recordings)
            if _plain(meta).st_nlink != 1:
                raise OSError("Refusing to update linked recording metadata")
            if self._disk() >= len(encoded) + 4096:
                temporary = meta.with_suffix(".json.tmp")
                if temporary.exists() or temporary.is_symlink():
                    # This name is private to our already-verified ownership file.
                    _plain(temporary)
                    temporary.unlink()
                with temporary.open("xb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(meta)
                _sync_directory(self.recordings)
            size = _plain(path).st_size + _plain(meta).st_size
            self._recording_bytes += size - self._wave_accounted - self._meta_accounted
            self._recording_files.append((int(path.stem), path, meta, size))
            self._recording_files.sort()
        except Exception as error:
            self._error(error)
        finally:
            if self._raw is not None:
                try:
                    self._raw.close()
                except OSError:
                    pass
            self._wave = self._raw = self._wave_path = self._meta_path = None
            self._wave_frames = self._wave_accounted = self._meta_accounted = 0
        if emit and metadata is not None:
            try:
                self._write_event("audio_recording", **metadata)
            except Exception as error:
                self._error(error)
