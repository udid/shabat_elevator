"""Streaming detections and capture failures without opening a microphone."""

import importlib.util
import math
import threading
import unittest
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import patch

from elevator.live_detector import (
    CaptureError, StreamingDetector, _AudioBlock, _CaptureBridge, _SampleClock, listen,
)
from elevator.runtime_config import validate_runtime_config


HAS_NUMPY = importlib.util.find_spec("numpy") is not None
RATE = 8000


def config():
    return {
        "schemaVersion": 1, "eventKind": "departure", "floor": 7, "defaultCycleSeconds": 570,
        "detector": {
            "profile": "band_level_snr_v1", "channel": 0,
            "frequencyLowHz": 900, "frequencyHighHz": 1100,
            "snrThresholdDb": 8, "noiseBandPower": 1e-10,
            "minimumRmsDbfs": -80, "minimumEventBandDbfs": -36,
            "minimumEventSeconds": .3, "maximumEventSeconds": 1.8,
            "mergeGapSeconds": .15, "rearmSeconds": 2,
        },
    }


class CompactValidationTests(unittest.TestCase):
    def test_runtime_requires_explicit_supported_identity_and_finite_values(self):
        for key, value in (("schemaVersion", True), ("schemaVersion", 2), ("eventKind", "arrival"),
                           ("floor", True), ("floor", 8), ("defaultCycleSeconds", 300),
                           ("defaultCycleSeconds", 1800), ("defaultCycleSeconds", math.nan)):
            document = config()
            document[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_runtime_config(document)
        for key, value in (("profile", "unknown"), ("channel", True), ("noiseBandPower", 0),
                           ("rearmSeconds", -1), ("frequencyHighHz", math.inf)):
            document = config()
            document["detector"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_runtime_config(document)

    def test_returns_clean_detached_copy_without_private_report_fields(self):
        document = config()
        document["observedAt"] = "old recording time"
        document["detector"]["sourcePath"] = "private path"
        result = validate_runtime_config(document)
        self.assertEqual(result, config())
        result["detector"]["channel"] = 1
        self.assertEqual(document["detector"]["channel"], 0)


@unittest.skipUnless(HAS_NUMPY, "NumPy audio dependency is optional")
class StreamingTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np

    def audio(self, duration, tones):
        samples = self.np.zeros(round(duration * RATE), self.np.float32)
        for start, end, amplitude, frequency in tones:
            left, right = round(start * RATE), round(end * RATE)
            times = self.np.arange(right - left) / RATE
            samples[left:right] += amplitude * self.np.sin(2 * self.np.pi * frequency * times)
        return samples

    def detect(self, samples, document=None, chunk_sizes=(137, 1024, 51, 791)):
        detector = StreamingDetector(document or config(), RATE)
        found, offset, index = [], 0, 0
        while offset < len(samples):
            size = chunk_sizes[index % len(chunk_sizes)]
            block = samples[offset:offset + size]
            found.extend(detector.feed(block, start_sample=offset))
            offset += len(block)
            index += 1
        return found, detector

    def test_positive_chime_keeps_exact_onset_across_arbitrary_chunk_boundaries(self):
        samples = self.audio(3, [(1, 1.8, .1, 1000)])
        fragmented, detector = self.detect(samples)
        whole, _ = self.detect(samples, chunk_sizes=(len(samples),))
        self.assertEqual(fragmented, whole)
        self.assertEqual(len(fragmented), 1)
        self.assertLess(abs(fragmented[0] / RATE - 1), detector.nfft / RATE)

    def test_weaker_same_frequency_chime_from_another_floor_is_rejected(self):
        samples = self.audio(7, [(1, 1.8, .001, 1000), (4, 4.8, .1, 1000)])
        found, _ = self.detect(samples)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0] / RATE, 4, delta=.064)

    def test_power_units_match_full_scale_rms_and_calibration_hann_window(self):
        detector = StreamingDetector(config(), RATE)
        tone = (.1 * self.np.sin(2 * self.np.pi * 1000 * self.np.arange(detector.nfft) / RATE)).astype(self.np.float32)
        active, band = detector._frame(tone)
        self.assertTrue(active)
        self.assertAlmostEqual(10 * math.log10(band), 20 * math.log10(.1 / math.sqrt(2)), delta=.01)
        if importlib.util.find_spec("scipy") is not None:
            from scipy.fft import rfft
            spectrum = self.np.abs(rfft(tone * detector.window)) ** 2 * (2 / detector.normalization)
            reference = float(spectrum[detector.low:detector.high].sum())
            self.assertAlmostEqual(band, reference, delta=reference * 1e-5)

    def test_p90_level_uses_active_windows_not_quiet_gap_or_peak_only(self):
        # Most of the chime is weak, but the loud third brings p90 above gate.
        samples = self.audio(3, [(1, 1.6, .001, 1000), (1.6, 1.9, .1, 1000)])
        found, _ = self.detect(samples)
        self.assertEqual(len(found), 1)
        # A single short spike is below 10% of matching windows, so reject it.
        samples = self.audio(3, [(1, 1.9, .001, 1000), (1.4, 1.401, .1, 1000)])
        found, _ = self.detect(samples)
        self.assertEqual(found, [])

    def test_long_noise_cannot_be_split_into_valid_repeating_chimes(self):
        samples = self.audio(9, [(1, 6, .1, 1000), (7, 7.8, .1, 1000)])
        found, detector = self.detect(samples)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0] / RATE, 7, delta=.064)
        self.assertIsNone(detector.candidate)

    def test_wrong_frequency_short_burst_and_clipped_input_are_rejected(self):
        for tones in ([(1, 1.8, .1, 2200)], [(1, 1.03, .1, 1000)], [(1, 1.8, 1, 1000)]):
            with self.subTest(tones=tones):
                samples = self.audio(3, tones)
                found, _ = self.detect(samples)
                self.assertEqual(found, [])

    def test_merge_gap_accepts_pulsed_chime_once_and_rearm_suppresses_echo(self):
        samples = self.audio(5, [(1, 1.3, .1, 1000), (1.38, 1.8, .1, 1000), (2.3, 2.9, .1, 1000)])
        found, _ = self.detect(samples)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0] / RATE, 1, delta=.064)

    def test_gap_discards_candidate_and_requires_quiet_before_new_onset(self):
        detector = StreamingDetector(config(), RATE)
        before = self.audio(1.6, [(1, 1.6, .1, 1000)])
        self.assertEqual(detector.feed(before, start_sample=0), [])
        # A gap occurs mid-chime. Its remaining .8 s must not become a new event.
        after = self.audio(4, [(0, .8, .1, 1000), (2, 2.8, .1, 1000)])
        found = detector.feed(after, start_sample=3 * RATE)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0] / RATE, 5, delta=.064)

    def test_startup_mid_chime_and_unfinished_shutdown_do_not_invent_onsets(self):
        found, _ = self.detect(self.audio(2, [(0, 1, .1, 1000)]))
        self.assertEqual(found, [])
        found, detector = self.detect(self.audio(1.8, [(1, 1.8, .1, 1000)]))
        self.assertEqual(found, [])
        detector.reset()
        self.assertIsNone(detector.candidate)

    def test_invalid_band_or_non_pcm_samples_fail_explicitly(self):
        document = config()
        document["detector"]["frequencyHighHz"] = RATE
        with self.assertRaises(ValueError):
            StreamingDetector(document, RATE)
        for values in ([math.nan], [1.1], [[0, 0]]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                StreamingDetector(config(), RATE).feed(values)


class FakeAbort(Exception):
    pass


class CaptureContinuityTests(unittest.TestCase):
    def test_portaudio_error_and_bounded_queue_overflow_abort_capture(self):
        timing = SimpleNamespace(inputBufferAdcTime=10, currentTime=10.1)
        bridge = _CaptureBridge(1, FakeAbort, queue_blocks=1)
        bridge.callback(b"\x00\x00" * 4, 4, timing, False)
        with self.assertRaises(FakeAbort):
            bridge.callback(b"\x00\x00" * 4, 4, timing, False)
        self.assertIsInstance(bridge.failure, CaptureError)
        self.assertIn("overflow", str(bridge.failure))
        bridge = _CaptureBridge(1, FakeAbort)
        with self.assertRaises(FakeAbort):
            bridge.callback(b"\x00\x00", 1, timing, True)
        self.assertTrue(bridge.blocks.empty())

    def test_invalid_adc_time_aborts_instead_of_fabricating_a_wall_timestamp(self):
        for adc, current in ((math.nan, 10), (10, 9), (0, 10)):
            bridge = _CaptureBridge(1, FakeAbort)
            with self.subTest(adc=adc, current=current), self.assertRaises(FakeAbort):
                bridge.callback(b"\x00\x00", 1, SimpleNamespace(inputBufferAdcTime=adc, currentTime=current), False)
            self.assertTrue(bridge.blocks.empty())

    def test_sample_clock_maps_adc_once_and_ignores_callback_jitter(self):
        epoch = 1_800_000_000_000_000_000
        clock = _SampleClock(1000, epoch, 1_000_000_000)
        clock.consume(_AudioBlock(b"", 100, 10, 10.1, 1_100_000_000), 1_100_000_000)
        clock.consume(_AudioBlock(b"", 100, 10.1, 10.2, 1_240_000_000), 1_300_000_000)
        self.assertEqual(clock.at(0).timestamp(), epoch / 1e9)
        self.assertEqual(clock.at(200).timestamp(), epoch / 1e9 + .2)
        self.assertEqual(clock.at(0).tzinfo, timezone.utc)

    def test_adc_gap_and_stale_queued_audio_are_rejected_before_counting_frames(self):
        clock = _SampleClock(1000, 1_800_000_000_000_000_000, 1_000_000_000)
        clock.consume(_AudioBlock(b"", 100, 10, 10.1, 1_100_000_000), 1_100_000_000)
        with self.assertRaisesRegex(CaptureError, "discontinuity"):
            clock.consume(_AudioBlock(b"", 100, 11, 11.1, 1_200_000_000), 1_200_000_000)
        self.assertEqual(clock.frames, 100)
        with self.assertRaisesRegex(CaptureError, "stale"):
            clock.consume(_AudioBlock(b"", 100, 10.1, 10.2, 1_200_000_000), 4_000_000_000)
        self.assertEqual(clock.frames, 100)


@unittest.skipUnless(HAS_NUMPY, "NumPy audio dependency is optional")
class ListenerTests(unittest.TestCase):
    def test_heartbeat_follows_actual_samples_and_stop_closes_stream(self):
        stop = threading.Event()
        heartbeats, events = [], []
        state = SimpleNamespace(closed=False)

        class FakeStream:
            active = True

            def __init__(self, **options):
                self.options = options

            def __enter__(self):
                self.options["callback"](b"\x00\x00" * 1024, 1024,
                                         SimpleNamespace(inputBufferAdcTime=10, currentTime=10.128), False)
                return self

            def __exit__(self, *args):
                state.closed = True

        sd = SimpleNamespace(CallbackAbort=FakeAbort, RawInputStream=FakeStream,
                             check_input_settings=lambda **kwargs: None)

        def heartbeat(timestamp):
            heartbeats.append(timestamp)
            stop.set()

        real_import = __import__("importlib").import_module
        with patch("elevator.live_detector.importlib.import_module",
                   side_effect=lambda name: sd if name == "sounddevice" else real_import(name)):
            listen(config(), events.append, heartbeat, stop, sample_rate=RATE)
        self.assertEqual(len(heartbeats), 1)
        self.assertEqual(heartbeats[0].tzinfo, timezone.utc)
        self.assertEqual(events, [])
        self.assertTrue(state.closed)

    def test_failed_or_disconnected_input_never_sends_heartbeat(self):
        for broken_callback in (False, True):
            with self.subTest(callback_failure=broken_callback):
                heartbeats = []

                class FakeStream:
                    active = False

                    def __init__(self, **options):
                        self.options = options

                    def __enter__(self):
                        if broken_callback:
                            try:
                                self.options["callback"](b"\x00\x00", 1,
                                                         SimpleNamespace(inputBufferAdcTime=1, currentTime=1), True)
                            except FakeAbort:
                                pass
                        return self

                    def __exit__(self, *args):
                        pass

                sd = SimpleNamespace(CallbackAbort=FakeAbort, RawInputStream=FakeStream,
                                     check_input_settings=lambda **kwargs: None)
                real_import = __import__("importlib").import_module
                with patch("elevator.live_detector.importlib.import_module",
                           side_effect=lambda name: sd if name == "sounddevice" else real_import(name)):
                    with self.assertRaises(CaptureError):
                        listen(config(), lambda value: None, heartbeats.append, threading.Event(), sample_rate=RATE)
                self.assertEqual(heartbeats, [])


if __name__ == "__main__":
    unittest.main()
