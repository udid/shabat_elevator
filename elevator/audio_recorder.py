"""Continuous local PCM recording for detector calibration; no audio is uploaded.

Only ``record_audio`` opens a microphone.  The PortAudio callback copies blocks
into a bounded queue; WAV and manifest writes happen on the calling thread.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import queue
import sys
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class RecorderError(RuntimeError):
    """Recording stopped because its continuity or storage could not be trusted."""

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class RecorderConfig:
    output: Path = Path("data/recordings")
    sample_rate: int = 44100
    chunk_seconds: float = 300
    duration_minutes: float | None = None
    device: str | int | None = None
    block_frames: int = 1024
    queue_blocks: int = 64
    timestamp_tolerance_ms: float = 10
    input_timeout_seconds: float = 10

    def validate(self):
        if type(self.sample_rate) is not int or self.sample_rate < 1:
            raise ValueError("sample_rate must be a positive integer")
        for name in ("chunk_seconds", "input_timeout_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.duration_minutes is not None and (
            not math.isfinite(self.duration_minutes) or self.duration_minutes <= 0
        ):
            raise ValueError("duration_minutes must be positive and finite")
        if self.chunk_frames < 1 or (self.target_frames is not None and self.target_frames < 1):
            raise ValueError("The requested recording or chunk must contain at least one sample")
        if not math.isfinite(self.timestamp_tolerance_ms) or self.timestamp_tolerance_ms < 0:
            raise ValueError("timestamp_tolerance_ms must be nonnegative and finite")
        if self.block_frames < 1 or self.queue_blocks < 1:
            raise ValueError("block_frames and queue_blocks must be positive")
        if sys.byteorder != "little":
            raise ValueError("This PCM16 recorder requires a little-endian host")

    @property
    def chunk_frames(self):
        return round(self.chunk_seconds * self.sample_rate)

    @property
    def target_frames(self):
        return None if self.duration_minutes is None else round(self.duration_minutes * 60 * self.sample_rate)


@dataclass(frozen=True)
class AudioBlock:
    pcm: bytes
    frames: int
    adc_time: float
    stream_time: float
    callback_monotonic_ns: int


def iso_ns(epoch_ns):
    return datetime.fromtimestamp(epoch_ns / 1_000_000_000, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Manifest:
    def __init__(self, path, session_id):
        self.file = Path(path).open("a", encoding="utf-8", newline="\n")
        self.session_id = session_id

    def emit(self, kind, **values):
        self.file.write(json.dumps({"type": kind, "sessionId": self.session_id, **values}, allow_nan=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


class CaptureBridge:
    """Small bounded callback; no filesystem, filters, or audio classification."""

    def __init__(self, config, abort_exception, monotonic_ns=time.monotonic_ns):
        self.config = config
        self.abort_exception = abort_exception
        self.monotonic_ns = monotonic_ns
        self.blocks = queue.Queue(maxsize=config.queue_blocks)
        self.finished = threading.Event()
        self.failure = None

    def callback(self, indata, frames, time_info, status):
        try:
            if status:
                raise RecorderError("PortAudio reported an input error; audio continuity is not guaranteed",
                                    stage="capture", portAudioStatus=str(status), droppedFramesUnknown=True)
            pcm = bytes(indata)
            if frames <= 0 or len(pcm) != frames * 2:
                raise RecorderError("Invalid mono PCM16 input block", stage="capture", frames=frames, byteCount=len(pcm))
            adc = float(time_info.inputBufferAdcTime)
            current = float(time_info.currentTime)
            if not math.isfinite(adc) or not math.isfinite(current) or adc > current + .01:
                raise RecorderError("Invalid PortAudio capture timestamps", stage="capture")
            block = AudioBlock(pcm, frames, adc, current, self.monotonic_ns())
            try:
                self.blocks.put_nowait(block)
            except queue.Full:
                raise RecorderError("Recording queue overflow; recording stopped instead of hiding an audio gap",
                                    stage="queue", droppedFrames=frames) from None
        except Exception as error:
            self.failure = error if isinstance(error, RecorderError) else RecorderError(str(error), stage="capture")
            raise self.abort_exception from None


class WaveChunkWriter:
    """Validate sample continuity and rotate WAVs at exact sample boundaries."""

    def __init__(self, config, manifest, wall_anchor_ns, monotonic_anchor_ns):
        self.config = config
        self.manifest = manifest
        self.wall_anchor_ns = wall_anchor_ns
        self.monotonic_anchor_ns = monotonic_anchor_ns
        self.audio_epoch_ns = None
        self.next_adc_time = None
        self.total_frames = 0
        self.file_frames = 0
        self.file_offset = 0
        self.files = 0
        self.wav = None
        self.raw_file = None
        self.path = None

    def epoch_at(self, frame_offset):
        return self.audio_epoch_ns + round(frame_offset * 1_000_000_000 / self.config.sample_rate)

    def _open(self):
        self.file_offset = self.total_frames
        self.file_frames = 0
        start_ns = self.epoch_at(self.file_offset)
        self.path = Path(self.config.output) / f"{start_ns // 1_000_000}.wav"
        # A collision must never overwrite either an old recording or this session.
        self.raw_file = self.path.open("xb")
        try:
            self.wav = wave.open(self.raw_file, "wb")
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(self.config.sample_rate)
        except Exception:
            self.raw_file.close()
            self.raw_file = None
            self.wav = None
            raise

    def _close_chunk(self):
        if self.wav is None:
            return
        try:
            self.wav.close()  # Patches the header, including a final partial chunk.
        finally:
            self.wav = None
            self.raw_file.close()
            self.raw_file = None
        self.files += 1
        start_ns = self.epoch_at(self.file_offset)
        end_ns = self.epoch_at(self.file_offset + self.file_frames)
        self.manifest.emit(
            "recording", file=self.path.name, startEpochMs=start_ns // 1_000_000,
            startUtc=iso_ns(start_ns), endUtc=iso_ns(end_ns), endExclusive=True,
            frames=self.file_frames, sampleRate=self.config.sample_rate, channels=1,
            sampleWidth=2, format="PCM_16", durationSeconds=self.file_frames / self.config.sample_rate,
            complete=self.file_frames == self.config.chunk_frames,
            timestampSource="first_adc_utc_mapping_then_sample_count",
        )

    def consume(self, block, max_frames=None):
        if self.next_adc_time is not None:
            difference = block.adc_time - self.next_adc_time
            tolerance = max(self.config.timestamp_tolerance_ms / 1000, 2 / self.config.sample_rate)
            if abs(difference) > tolerance:
                raise RecorderError("ADC timestamp discontinuity; recording stopped before the discontinuous block",
                                    stage="timestamps", gapSeconds=difference,
                                    expectedAdcTime=self.next_adc_time, actualAdcTime=block.adc_time,
                                    toleranceSeconds=tolerance)
        if self.audio_epoch_ns is None:
            callback_utc = self.wall_anchor_ns + block.callback_monotonic_ns - self.monotonic_anchor_ns
            self.audio_epoch_ns = callback_utc + round((block.adc_time - block.stream_time) * 1_000_000_000)
            self.manifest.emit("audio_start", startUtc=iso_ns(self.audio_epoch_ns),
                               startEpochMs=self.audio_epoch_ns // 1_000_000,
                               adcTime=block.adc_time, streamTime=block.stream_time)
        self.next_adc_time = block.adc_time + block.frames / self.config.sample_rate
        count = block.frames if max_frames is None else min(block.frames, max_frames)
        consumed = 0
        while consumed < count:
            if self.wav is None:
                self._open()
            take = min(count - consumed, self.config.chunk_frames - self.file_frames)
            self.wav.writeframesraw(block.pcm[consumed * 2:(consumed + take) * 2])
            self.total_frames += take
            self.file_frames += take
            consumed += take
            if self.file_frames == self.config.chunk_frames:
                self._close_chunk()
        return consumed

    def close(self):
        self._close_chunk()


def load_sounddevice():
    try:
        return importlib.import_module("sounddevice")
    except (ImportError, OSError) as error:
        raise RecorderError("Audio support is unavailable. Run 'uv sync --group audio'; on Linux install PortAudio too.") from error


def record_audio(config, *, sounddevice_module=None, wall_ns=time.time_ns, monotonic_ns=time.monotonic_ns):
    """Record until duration/Ctrl+C. Raise RecorderError on any capture/storage gap.

    Dependency and clocks are injectable so tests never activate a microphone.
    A duration counts captured samples, not wall-clock delay between callbacks.
    """
    config.validate()
    sd = sounddevice_module if sounddevice_module is not None else load_sounddevice()
    output = Path(config.output)
    output.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    # One wall-clock anchor only. A clock correction later cannot move filenames.
    before = monotonic_ns()
    epoch_ns = wall_ns()
    anchor_ns = (before + monotonic_ns()) // 2
    manifest = Manifest(output / "manifest.jsonl", session_id)
    writer = WaveChunkWriter(config, manifest, epoch_ns, anchor_ns)
    bridge = CaptureBridge(config, sd.CallbackAbort, monotonic_ns)
    outcome = "complete"
    error = None
    interrupted = False
    try:
        manifest.emit("session_start", startedUtc=iso_ns(epoch_ns), sampleRate=config.sample_rate,
                      channels=1, sampleWidth=2, format="PCM_16", chunkSeconds=config.chunk_seconds,
                      chunkFrames=config.chunk_frames, durationMinutes=config.duration_minutes,
                      device=config.device, blockFrames=config.block_frames, queueBlocks=config.queue_blocks,
                      timestampToleranceMs=config.timestamp_tolerance_ms,
                      timestampMeaning="estimated UTC of first captured sample; ADC clock mapped at first callback",
                      eventKind="raw_audio_for_calibration")
        sd.check_input_settings(device=config.device, channels=1, dtype="int16", samplerate=config.sample_rate)
        with sd.RawInputStream(device=config.device, channels=1, dtype="int16", samplerate=config.sample_rate,
                               blocksize=config.block_frames, callback=bridge.callback,
                               finished_callback=bridge.finished.set) as stream:
            last_block_ns = monotonic_ns()
            while config.target_frames is None or writer.total_frames < config.target_frames:
                try:
                    block = bridge.blocks.get(timeout=.2)
                except queue.Empty:
                    if bridge.failure:
                        raise bridge.failure
                    if bridge.finished.is_set() or not stream.active:
                        raise RecorderError("Audio input stopped unexpectedly", stage="capture")
                    if (monotonic_ns() - last_block_ns) / 1e9 > config.input_timeout_seconds:
                        raise RecorderError("No microphone samples received within the input timeout", stage="capture")
                    continue
                remaining = None if config.target_frames is None else config.target_frames - writer.total_frames
                writer.consume(block, remaining)
                last_block_ns = monotonic_ns()
    except KeyboardInterrupt:
        outcome = "interrupted"
        interrupted = True
    except Exception as caught:
        error = caught if isinstance(caught, RecorderError) else RecorderError(str(caught), stage="recording", errorType=type(caught).__name__)
    finally:
        # The stream has now stopped. Keep already captured blocks on Ctrl+C;
        # capture failures drain trustworthy blocks before reporting the failure.
        if interrupted and error is None:
            try:
                while not bridge.blocks.empty():
                    remaining = None if config.target_frames is None else config.target_frames - writer.total_frames
                    if remaining is not None and remaining <= 0:
                        break
                    writer.consume(bridge.blocks.get_nowait(), remaining)
            except Exception as caught:
                error = caught if isinstance(caught, RecorderError) else RecorderError(str(caught), stage="finalize")
        target_complete = config.target_frames is not None and writer.total_frames >= config.target_frames
        if bridge.failure and error is None and not target_complete:
            error = bridge.failure
        try:
            writer.close()
        except Exception as caught:
            error = error or RecorderError(str(caught), stage="finalize", errorType=type(caught).__name__)
        if error:
            outcome = "error"
        summary = {"outcome": outcome, "frames": writer.total_frames, "files": writer.files,
                   "durationSeconds": writer.total_frames / config.sample_rate,
                   "endedUtc": iso_ns(writer.epoch_at(writer.total_frames)) if writer.audio_epoch_ns is not None else None}
        try:
            if error:
                manifest.emit("error", message=str(error), **error.details)
            manifest.emit("session_end", **summary)
        finally:
            manifest.close()
    if error:
        raise error
    return summary


def device_argument(value):
    try:
        return int(value)
    except ValueError:
        return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record continuous local microphone WAVs for elevator sound calibration.")
    parser.add_argument("--output", type=Path, default=Path("data/recordings"), help="Recording directory (default: data/recordings)")
    parser.add_argument("--duration-minutes", type=float, help="Stop after this many captured minutes; default: until Ctrl+C")
    parser.add_argument("--chunk-seconds", type=float, default=300, help="WAV duration (default: 300 seconds)")
    parser.add_argument("--sample-rate", type=int, default=44100, help="Input sample rate (default: 44100 Hz)")
    parser.add_argument("--device", type=device_argument, help="Input device index or name")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices without opening a microphone stream")
    parser.add_argument("--timestamp-tolerance-ms", type=float, default=10, help="Maximum ADC timestamp discontinuity (default: 10 ms)")
    args = parser.parse_args(argv)
    try:
        sd = load_sounddevice()
        if args.list_devices:
            print(sd.query_devices())
            return 0
        config = RecorderConfig(output=args.output, sample_rate=args.sample_rate, chunk_seconds=args.chunk_seconds,
                                duration_minutes=args.duration_minutes, device=args.device,
                                timestamp_tolerance_ms=args.timestamp_tolerance_ms)
        config.validate()
        print(f"Recording mono PCM16 at {config.sample_rate} Hz to {config.output}. Press Ctrl+C to finish.", flush=True)
        summary = record_audio(config, sounddevice_module=sd)
        print(f"Recording {summary['outcome']}: {summary['files']} WAV files, {summary['durationSeconds']:.2f} seconds.")
        return 0
    except (RecorderError, ValueError, OSError) as error:
        print(f"Recording failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
