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


class StreamingDetector:
    """Consume normalized mono PCM and return confirmed onset sample indices.

    ``start_sample`` identifies the first sample in each block. A discontinuity
    discards partial FFT windows and pending events, then requires quiet before
    arming again. Neither a gap nor shutdown finalizes an unfinished sound.
    The initial session also waits for quiet to avoid timestamping a sound that
    was already in progress when the microphone opened.
    """

    def __init__(self, config, sample_rate=44100):
        self.settings = validate_runtime_config(config)["detector"]
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
        self.pending = self.np.empty(0, self.np.float32)
        self.pending_start = self.expected_sample = start_sample
        self.candidate = None
        self.armed = False
        self.quiet_start = None

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
        if candidate is None or candidate["too_long"]:
            return None
        duration = (candidate["end"] - candidate["start"]) / self.sample_rate
        if duration < self.settings["minimumEventSeconds"]:
            return None
        level = 10 * math.log10(max(float(self.np.quantile(candidate["powers"], .9)), 1e-20))
        threshold = self.settings["minimumEventBandDbfs"]
        if threshold is not None and level < threshold:
            return None
        onset = candidate["start"]
        if self.last_event is not None and onset - self.last_event < self.rearm_samples:
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
                                      "powers": [], "too_long": False}
                item = self.candidate
                item["end"] = position + self.nfft
                if (item["end"] - item["start"]) / self.sample_rate > self.settings["maximumEventSeconds"]:
                    item["too_long"] = True
                    item["powers"].clear()
                elif not item["too_long"]:
                    item["powers"].append(band)
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
                raise CaptureError("PortAudio input error; discarding the capture session")
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


def listen(config, on_event, on_heartbeat, stop_event, *, device=None, sample_rate=44100):
    """Capture until stopped; callbacks receive aware UTC onset/heartbeat times.

    Heartbeats occur only after fresh successfully processed audio, initially
    and then every five seconds. Capture faults raise ``CaptureError``; callers
    should mark the source disconnected and reopen with a fresh detector.
    No recording is saved or sent over the network.
    """
    detector = StreamingDetector(config, sample_rate)
    if stop_event.is_set():
        return
    try:
        sd = importlib.import_module("sounddevice")
    except (ImportError, OSError) as error:
        raise CaptureError("Live capture requires sounddevice and PortAudio") from error
    np = detector.np
    channels = detector.settings["channel"] + 1
    bridge = _CaptureBridge(channels, sd.CallbackAbort)
    before = time.monotonic_ns()
    epoch = time.time_ns()
    clock = _SampleClock(sample_rate, epoch, (before + time.monotonic_ns()) // 2)
    last_heartbeat = None
    try:
        sd.check_input_settings(device=device, samplerate=sample_rate, channels=channels, dtype="int16")
        with sd.RawInputStream(device=device, samplerate=sample_rate, channels=channels,
                               dtype="int16", blocksize=1024, callback=bridge.callback,
                               finished_callback=bridge.finished.set) as stream:
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
                events = detector.feed(samples.astype(np.float32) / 32768, start_sample=offset)
                # A callback can fail while FFT work is running. Do not publish
                # detections or heartbeat from that now-failed capture session.
                if bridge.failure:
                    raise bridge.failure
                for onset in events:
                    on_event(clock.at(onset))
                if last_heartbeat is None or clock.frames - last_heartbeat >= 5 * sample_rate:
                    on_heartbeat(clock.at(clock.frames))
                    last_heartbeat = clock.frames
                last_received = block.received_ns
    except (CaptureError, KeyboardInterrupt):
        raise
    except Exception as error:
        raise CaptureError(f"Live audio capture failed: {error}") from error
