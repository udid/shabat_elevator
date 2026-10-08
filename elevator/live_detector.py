"""Live compact-profile chime detection; audio stays on the device.

NumPy and sounddevice are imported only when detection starts. The streaming
detector uses the calibration's normalized PCM/Hann/p90 level units; this small
profile intentionally does not reproduce its offline fingerprint clustering.
Event times identify the first matching window, *not* callback delivery time.
"""

from __future__ import annotations

import importlib
import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .runtime_config import validate_runtime_config


class CaptureError(RuntimeError):
    """No observation may be inferred across this failed capture session."""


def _numpy():
    try:
        return importlib.import_module("numpy")
    except ImportError as error:
        raise CaptureError("Live audio detection requires NumPy") from error


def _diagnostic(diagnostics, method, *args, **fields):
    """Optional storage must never determine whether microphone capture works."""
    if diagnostics is not None:
        try:
            getattr(diagnostics, method)(*args, **fields)
        except Exception:
            # The normal sink reports its own failures. Protect detection also
            # when an injected or future implementation violates that contract.
            pass


class StreamingDetector:
    """Consume normalized mono PCM and return confirmed onset sample indices.

    ``start_sample`` identifies the first sample in each block. A discontinuity
    discards partial FFT windows and pending events, then requires quiet before
    arming again. Neither a gap nor shutdown finalizes an unfinished sound.
    The initial session also waits for quiet to avoid timestamping a sound that
    was already in progress when the microphone opened.
    """

    def __init__(self, config, sample_rate=44100, *, on_candidate=None):
        self.settings = validate_runtime_config(config)["detector"]
        self.on_candidate = on_candidate
        if type(sample_rate) is not int or not 1000 <= sample_rate <= 192000:
            raise ValueError("sample_rate must be an integer between 1000 and 192000")
        self.sample_rate = sample_rate
        self.np = np = _numpy()
        self.nfft = 2 ** math.ceil(math.log2(sample_rate * .064))
        self.hop = self.nfft // 2
        self.window = np.hanning(self.nfft).astype(np.float32)
        self.normalization = self.nfft * float(np.sum(self.window.astype(np.float64) ** 2))
        if self.settings["frequencyHighHz"] > sample_rate / 2:
            raise ValueError("Detector frequency band exceeds the microphone's Nyquist frequency")
        frequencies = np.fft.rfftfreq(self.nfft, 1 / sample_rate)
        self.low, self.high = map(int, np.searchsorted(frequencies, [
            self.settings["frequencyLowHz"], self.settings["frequencyHighHz"],
        ]))
        if self.low == self.high:
            raise ValueError("Detector frequency band contains no FFT bins")
        self.minimum_power = 10 ** (self.settings["minimumRmsDbfs"] / 10)
        # Compare in dB to avoid overflowing on valid but extremely high SNR.
        self.merge_samples = self.settings["mergeGapSeconds"] * sample_rate
        self.rearm_samples = self.settings["rearmSeconds"] * sample_rate
        self.last_event = None
        self.reset()

    def reset(self, start_sample=0):
        """Discard unconfirmed audio, preserving the last accepted-event rearm."""
        if type(start_sample) is not int or start_sample < 0:
            raise ValueError("start_sample must be a nonnegative integer")
        self.discard("discontinuity")
        self.pending = self.np.empty(0, self.np.float32)
        self.pending_start = self.expected_sample = start_sample
        self.candidate = None
        self.armed = False
        self.quiet_start = None

    def _report_candidate(self, candidate, reason):
        if self.on_candidate is None:
            return
        power = float(self.np.quantile(candidate["powers"], .9)) if candidate["powers"] else None
        level = 10 * math.log10(max(power, 1e-20)) if power is not None else None
        details = {
            "startSample": candidate["start"], "endSample": candidate["end"],
            "durationSeconds": (candidate["end"] - candidate["start"]) / self.sample_rate,
            "activeFrames": candidate["active_frames"], "powerFrames": len(candidate["powers"]),
            "bandPowerP90": power, "bandDbfsP90": level,
            "snrDbP90": level - 10 * math.log10(self.settings["noiseBandPower"]) if level is not None else None,
            "powerStatisticsTruncated": candidate["too_long"],
            "accepted": reason == "accepted", "reason": reason,
        }
        try:
            self.on_candidate(details)
        except Exception:
            pass

    def discard(self, reason):
        """Report an incomplete candidate without turning it into a detection."""
        candidate, self.candidate = getattr(self, "candidate", None), None
        if candidate is not None:
            self._report_candidate(candidate, reason)

    def _frame(self, samples):
        """Match calibration: symmetric float32 Hann, one-sided mean power."""
        np = self.np
        # scipy.fft.rfft(float32) produces complex64 in calibration. NumPy FFT
        # historically promotes to complex128; restore the same power dtype.
        spectrum = np.abs(np.fft.rfft(samples * self.window).astype(np.complex64)) ** 2 * (2 / self.normalization)
        band = float(np.sum(spectrum[self.low:self.high], dtype=np.float32))
        rms = float(np.mean(samples * samples))
        clipped = float(np.mean(np.abs(samples) >= .999))
        snr = 10 * math.log10(max(band, 1e-20) / self.settings["noiseBandPower"])
        active = (clipped < .02 and rms >= self.minimum_power and
                  band >= self.minimum_power and snr >= self.settings["snrThresholdDb"])
        return active, band

    def _finish(self):
        candidate, self.candidate = self.candidate, None
        if candidate is None:
            return None
        duration = (candidate["end"] - candidate["start"]) / self.sample_rate
        level = 10 * math.log10(max(float(self.np.quantile(candidate["powers"], .9)), 1e-20))
        threshold = self.settings["minimumEventBandDbfs"]
        onset = candidate["start"]
        if candidate["too_long"]:
            reason = "too_long"
        elif duration < self.settings["minimumEventSeconds"]:
            reason = "too_short"
        elif threshold is not None and level < threshold:
            reason = "weak_band"
        elif self.last_event is not None and onset - self.last_event < self.rearm_samples:
            reason = "rearm"
        else:
            reason = "accepted"
        self._report_candidate(candidate, reason)
        if reason != "accepted":
            return None
        self.last_event = onset
        return onset

    def feed(self, samples, *, start_sample=None):
        np = self.np
        values = np.asarray(samples, dtype=np.float32)
        if values.ndim != 1 or not np.isfinite(values).all() or (np.abs(values) > 1).any():
            raise ValueError("Expected finite normalized mono PCM samples in [-1, 1]")
        start = self.expected_sample if start_sample is None else start_sample
        if type(start) is not int or start < 0:
            raise ValueError("start_sample must be a nonnegative integer")
        if start != self.expected_sample:
            self.reset(start)
        self.expected_sample = start + len(values)
        self.pending = np.concatenate((self.pending, values))
        events = []
        used = 0
        while len(self.pending) - used >= self.nfft:
            position = self.pending_start + used
            active, band = self._frame(self.pending[used:used + self.nfft])
            used += self.hop
            if not self.armed:
                if active:
                    self.quiet_start = None
                else:
                    if self.quiet_start is None:
                        self.quiet_start = position
                    if position + self.nfft - self.quiet_start >= self.merge_samples:
                        self.armed = True
                continue
            if self.candidate is not None and position - self.candidate["end"] > self.merge_samples:
                event = self._finish()
                if event is not None:
                    events.append(event)
            if active:
                if self.candidate is None:
                    self.candidate = {"start": position, "end": position + self.nfft,
                                      "powers": [], "too_long": False, "active_frames": 0}
                item = self.candidate
                item["end"] = position + self.nfft
                item["active_frames"] += 1
                # Keep only the bounded initial interval for rejected long
                # sounds; it remains useful diagnostically without growing RAM.
                if not item["too_long"]:
                    item["powers"].append(band)
                if (item["end"] - item["start"]) / self.sample_rate > self.settings["maximumEventSeconds"]:
                    item["too_long"] = True
        if used:
            self.pending = self.pending[used:].copy()
            self.pending_start += used
        return events


@dataclass(frozen=True)
class _AudioBlock:
    pcm: bytes
    frames: int
    adc: float
    current: float
    received_ns: int


class _CaptureBridge:
    """Bounded callback: copy PCM and timings only; never process audio here."""

    def __init__(self, channels, abort_exception, *, queue_blocks=64):
        self.channels = channels
        self.abort_exception = abort_exception
        self.blocks = queue.Queue(maxsize=queue_blocks)
        self.finished = threading.Event()
        self.failure = None

    def callback(self, indata, frames, timing, status):
        try:
            if status:
                raise CaptureError(f"PortAudio input error ({status}); discarding the capture session")
            pcm = bytes(indata)
            if frames <= 0 or len(pcm) != frames * self.channels * 2:
                raise CaptureError("Invalid PCM16 microphone block")
            adc, current = float(timing.inputBufferAdcTime), float(timing.currentTime)
            if not math.isfinite(adc) or not math.isfinite(current) or not -.01 <= current - adc <= 2:
                raise CaptureError("Unavailable or stale microphone capture timestamps")
            try:
                self.blocks.put_nowait(_AudioBlock(pcm, frames, adc, current, time.monotonic_ns()))
            except queue.Full:
                raise CaptureError("Microphone queue overflow; audio continuity was lost") from None
        except Exception as error:
            self.failure = error if isinstance(error, CaptureError) else CaptureError(str(error))
            raise self.abort_exception from None


class _SampleClock:
    def __init__(self, sample_rate, wall_ns, monotonic_ns):
        self.sample_rate = sample_rate
        self.wall_ns = wall_ns
        self.monotonic_ns = monotonic_ns
        self.epoch_ns = None
        self.next_adc = None
        self.frames = 0

    def consume(self, block, now_ns):
        if now_ns - block.received_ns > 2_000_000_000:
            raise CaptureError("Microphone samples became stale before processing")
        if self.next_adc is not None and abs(block.adc - self.next_adc) > max(.002, 2 / self.sample_rate):
            raise CaptureError("Microphone ADC timestamp discontinuity")
        if self.epoch_ns is None:
            callback_ns = self.wall_ns + block.received_ns - self.monotonic_ns
            self.epoch_ns = callback_ns + round((block.adc - block.current) * 1e9)
        self.next_adc = block.adc + block.frames / self.sample_rate
        self.frames += block.frames

    def at(self, sample):
        return datetime.fromtimestamp((self.epoch_ns + round(sample * 1e9 / self.sample_rate)) / 1e9, timezone.utc)


def listen(config, on_event, on_heartbeat, stop_event, *, device=None, sample_rate=44100,
           diagnostics=None):
    """Capture until stopped; callbacks receive aware UTC onset/heartbeat times.

    Heartbeats occur only after fresh successfully processed audio, initially
    and then every five seconds. Capture faults raise ``CaptureError``; callers
    should mark the source disconnected and reopen with a fresh detector.
    Optional diagnostics receive mono PCM16 from this same capture stream.
    Their nonblocking sink controls local storage and retention independently.
    """
    detector = StreamingDetector(config, sample_rate)
    if stop_event.is_set():
        return
    np = detector.np
    channels = detector.settings["channel"] + 1
    session_id = uuid.uuid4().hex

    def emit(kind, **fields):
        _diagnostic(diagnostics, "event", kind, sessionId=session_id, **fields)

    before = time.monotonic_ns()
    epoch = time.time_ns()
    clock = _SampleClock(sample_rate, epoch, (before + time.monotonic_ns()) // 2)

    def candidate(details):
        start_ns = clock.epoch_ns + round(details["startSample"] * 1e9 / sample_rate)
        emit("acoustic_candidate", onsetNs=start_ns,
             onsetUtc=clock.at(details["startSample"]).isoformat(), **details)

    if diagnostics is not None:
        detector.on_candidate = candidate
    summary_start = summary_frames = summary_clipped = 0
    summary_squares = 0.0

    def summary():
        nonlocal summary_start, summary_frames, summary_clipped, summary_squares
        if summary_frames:
            emit("audio_summary", startNs=clock.epoch_ns + round(summary_start * 1e9 / sample_rate),
                 sampleRate=sample_rate, frames=summary_frames,
                 durationSeconds=summary_frames / sample_rate,
                 rmsDbfs=10 * math.log10(max(summary_squares / summary_frames, 1e-20)),
                 clippingFraction=summary_clipped / summary_frames,
                 lastAudioUtc=clock.at(clock.frames).isoformat())
            summary_start = clock.frames
            summary_frames = summary_clipped = 0
            summary_squares = 0.0

    last_heartbeat = None
    failed = False
    emit("capture_start", device=device, sampleRate=sample_rate,
         channel=detector.settings["channel"])
    try:
        try:
            sd = importlib.import_module("sounddevice")
        except (ImportError, OSError) as error:
            raise CaptureError("Live capture requires sounddevice and PortAudio") from error
        bridge = _CaptureBridge(channels, sd.CallbackAbort)
        sd.check_input_settings(device=device, samplerate=sample_rate, channels=channels, dtype="int16")
        with sd.RawInputStream(device=device, samplerate=sample_rate, channels=channels,
                               dtype="int16", blocksize=1024, callback=bridge.callback,
                               finished_callback=bridge.finished.set) as stream:
            emit("capture_open")
            last_received = time.monotonic_ns()
            while not stop_event.is_set():
                if bridge.failure:
                    raise bridge.failure
                if bridge.finished.is_set() or not stream.active:
                    raise CaptureError("Microphone input stopped unexpectedly")
                try:
                    block = bridge.blocks.get(timeout=.2)
                except queue.Empty:
                    if time.monotonic_ns() - last_received > 5_000_000_000:
                        raise CaptureError("Microphone has not delivered samples for five seconds")
                    continue
                now = time.monotonic_ns()
                # Restart after a wall-clock correction, rather than retaining
                # an obsolete mapping and advertising incorrectly dated data.
                if abs(time.time_ns() - (clock.wall_ns + now - clock.monotonic_ns)) > 2_000_000_000:
                    raise CaptureError("System UTC clock changed during audio capture")
                offset = clock.frames
                clock.consume(block, now)
                samples = np.frombuffer(block.pcm, dtype=np.int16).reshape(-1, channels)[:, detector.settings["channel"]]
                normalized = samples.astype(np.float32) / 32768
                if diagnostics is not None:
                    # This stays outside PortAudio's callback and only enqueues
                    # an immutable copy. Never open a second input device.
                    _diagnostic(diagnostics, "audio", samples.astype("<i2", copy=False).tobytes(),
                                sample_rate=sample_rate,
                                start_ns=clock.epoch_ns + round(offset * 1e9 / sample_rate),
                                session_id=session_id)
                    summary_frames += block.frames
                    summary_squares += float(np.sum(normalized.astype(np.float64) ** 2))
                    summary_clipped += int(np.count_nonzero(np.abs(normalized) >= .999))
                events = detector.feed(normalized, start_sample=offset)
                # A callback can fail while FFT work is running. Do not publish
                # detections or heartbeat from that now-failed capture session.
                if bridge.failure:
                    raise bridge.failure
                if last_heartbeat is None:
                    emit("capture_ready", firstAudioUtc=clock.at(0).isoformat())
                for onset in events:
                    on_event(clock.at(onset))
                if last_heartbeat is None or clock.frames - last_heartbeat >= 5 * sample_rate:
                    on_heartbeat(clock.at(clock.frames))
                    last_heartbeat = clock.frames
                if summary_frames >= 60 * sample_rate:
                    summary()
                last_received = block.received_ns
    except KeyboardInterrupt:
        raise
    except Exception as error:
        failed = True
        emit("capture_failure", errorType=type(error).__name__, message=str(error))
        if isinstance(error, CaptureError):
            raise
        raise CaptureError(f"Live audio capture failed: {error}") from error
    finally:
        detector.discard("capture_failure" if failed else "capture_stopped")
        summary()
        emit("capture_stop", reason="failure" if failed else "stopped", frames=clock.frames)
