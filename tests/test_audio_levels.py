"""Independent regression cases for the same chime heard at different distances.

All events deliberately have the same spectral shape.  These tests cannot pass
by finding a frequency unique to floor seven or by using signal-to-noise alone.
The fixtures are streamed to local PCM files and never open an audio device.
"""

import json
import math
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from elevator.audio_calibration import (
    CalibrationConfig, _is_demonstrably_weaker, _spectral_batches, calibrate, inspect_recordings,
)


BASE_MS = 1_700_000_000_000
RATE = 4000
STRONG = [(30, .060), (600, .055), (1170, .070), (1740, .050)]
WEAK = [(time + phase, amplitude)
        for time, _ in STRONG
        for phase, amplitude in ((110, .008), (260, .010), (430, .009))
        if time + phase < 1780]


def write_level_fixture(folder, events, *, duration=1780, start=0, gain=1, seed=314):
    """Identical .7-second 900→1150 Hz chirps; only absolute level changes."""
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{BASE_MS + round(start * 1000)}.wav"
    rng = np.random.default_rng(seed)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        for offset in range(0, round(duration * RATE), RATE * 10):
            count = min(RATE * 10, round(duration * RATE) - offset)
            times = start + (offset + np.arange(count)) / RATE
            # Even the distant chimes have high SNR in this quiet recording.
            signal = rng.normal(0, .00008, count)
            for event_time, amplitude in events:
                local = times - event_time
                active = (local >= 0) & (local < .7)
                phase = local[active]
                signal[active] += amplitude * np.sin(np.pi * phase / .7) ** 2 * np.sin(
                    2 * np.pi * (900 * phase + .5 * (250 / .7) * phase ** 2)
                )
            output.writeframesraw(np.rint(np.clip(signal * gain, -.999, .999) * 32767).astype("<i2").tobytes())
    return path


class SameChimeLevelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temporary.name)
        cls.baseline_path = write_level_fixture(cls.folder / "baseline", STRONG + WEAK)
        cls.baseline = calibrate([cls.baseline_path])

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def assert_strong_departures(self, result):
        self.assertEqual(result["status"], "ready", (result["reason"], result.get("intensitySeparation"), result.get("alternatives")))
        self.assertAlmostEqual(result["periodSeconds"], 570, delta=.2)
        self.assertEqual(len(result["matchingEvents"]), len(STRONG))
        for event, (expected, _) in zip(result["matchingEvents"], STRONG):
            self.assertAlmostEqual((event["epochMs"] - BASE_MS) / 1000, expected, delta=.45)

    def test_high_snr_distant_identical_chimes_are_excluded_by_learned_absolute_level(self):
        result = self.baseline
        self.assert_strong_departures(result)
        separation = result["intensitySeparation"]
        self.assertTrue(separation["clear"])
        self.assertGreaterEqual(separation["gapDb"], 6)
        self.assertGreaterEqual(separation["weakExcludedCount"], 6)
        self.assertEqual(separation["strongMatchedCount"], 4)
        gate = result["parameters"]["minimumEventBandDbfs"]
        self.assertTrue(math.isfinite(gate))
        self.assertLess(gate, 0)
        self.assertEqual(gate, separation["thresholdDbfs"])
        self.assertTrue(result["parameters"]["eventBandLevelMetric"])
        for event in result["matchingEvents"]:
            self.assertGreaterEqual(event["eventBandDbfs"], gate)
        json.dumps(result, allow_nan=False)

    def test_one_much_louder_identical_chime_does_not_replace_recurring_strong_sequence(self):
        file = write_level_fixture(self.folder / "outlier", STRONG + WEAK + [(900, .24)])
        result = calibrate([file])
        self.assert_strong_departures(result)
        self.assertTrue(result["intensitySeparation"]["clear"])
        self.assertEqual(result["intensitySeparation"]["strongMatchedCount"], 4)
        self.assertTrue(all(abs((event["epochMs"] - BASE_MS) / 1000 - 900) > 1
                            for event in result["matchingEvents"]))

    def test_two_similar_level_periodic_phases_remain_ambiguous(self):
        # Each phase is a plausible 570-second sequence, with overlapping levels.
        peers = [(time + 180, amplitude) for (time, _), amplitude in zip(STRONG, (.062, .050, .064, .055))]
        file = write_level_fixture(self.folder / "ambiguous", STRONG + peers, duration=1950)
        result = calibrate([file])
        self.assertEqual(result["status"], "review", (result["reason"], result.get("support")))
        self.assertFalse(result["intensitySeparation"]["clear"])
        self.assertIsNone(result["parameters"]["minimumEventBandDbfs"])

    def test_global_gain_scales_absolute_gate_without_changing_selected_events(self):
        gain = .25
        file = write_level_fixture(self.folder / "quieter", STRONG + WEAK, gain=gain)
        quieter = calibrate([file])
        self.assert_strong_departures(self.baseline)
        self.assert_strong_departures(quieter)
        expected_shift = 20 * math.log10(gain)
        actual_shift = quieter["parameters"]["minimumEventBandDbfs"] - self.baseline["parameters"]["minimumEventBandDbfs"]
        # A per-file peak normalization would incorrectly erase this ~12 dB shift.
        self.assertAlmostEqual(actual_shift, expected_shift, delta=1.5)
        for quiet, original in zip(quieter["matchingEvents"], self.baseline["matchingEvents"]):
            self.assertAlmostEqual(quiet["eventBandDbfs"] - original["eventBandDbfs"], expected_shift, delta=1.5)

    def test_gain_change_between_files_does_not_validate_a_single_absolute_gate(self):
        # Later nearby chimes overlap the earlier distant-chime levels. Separate
        # per-file normalization would hide this genuine deployment ambiguity.
        folder = self.folder / "gain-change"
        first = write_level_fixture(folder, STRONG + WEAK, duration=900, gain=1)
        second = write_level_fixture(folder, STRONG + WEAK, start=900, duration=880, gain=.2, seed=315)
        result = calibrate([first, second])
        self.assertEqual(result["status"], "review", (result["reason"], result.get("support")))
        self.assertFalse(result["intensitySeparation"]["clear"])
        self.assertIsNone(result["parameters"]["minimumEventBandDbfs"])
        self.assertIn("gain", " ".join(result["limitations"]).lower())


class SuppressionEvidenceTests(unittest.TestCase):
    def test_different_sound_at_the_same_weak_chime_times_is_not_suppressed(self):
        times = [{"time": time} for time in (100, 670, 1240)]
        strong = {"sessionId": 0, "prototype": np.array([1.0, 0.0]),
                  "intensitySeparation": {"clear": True}, "excludedWeakEvents": times}
        candidate = {"sessionId": 0, "prototype": np.array([0.0, 1.0]),
                     "intensitySeparation": {"clear": False}, "matchingEvents": times}
        self.assertFalse(_is_demonstrably_weaker(candidate, strong, 3))
        candidate["prototype"] = strong["prototype"].copy()
        self.assertTrue(_is_demonstrably_weaker(candidate, strong, 3))

    def test_conflicting_strong_tiers_cannot_suppress_each_other(self):
        first_times = [{"time": time} for time in (0, 570, 1140)]
        second_times = [{"time": time} for time in (200, 770, 1340)]
        first_spectrum = np.array([1.0, .5])
        first_spectrum /= np.linalg.norm(first_spectrum)
        second_spectrum = np.array([.7, 1.0])
        second_spectrum /= np.linalg.norm(second_spectrum)
        self.assertGreater(float(first_spectrum @ second_spectrum), .82)
        first = {"sessionId": 0, "prototype": first_spectrum,
                 "intensitySeparation": {"clear": True}, "matchingEvents": first_times,
                 "excludedWeakEvents": second_times}
        second = {"sessionId": 0, "prototype": second_spectrum,
                  "intensitySeparation": {"clear": True}, "matchingEvents": second_times,
                  "excludedWeakEvents": first_times}
        self.assertFalse(_is_demonstrably_weaker(first, second, 3))
        self.assertFalse(_is_demonstrably_weaker(second, first, 3))


class AbsoluteLevelUnitsTests(unittest.TestCase):
    def test_integrated_hann_spectrum_reports_true_pcm_rms_dbfs(self):
        # A bin-centered sine with peak 0.1 has RMS 0.1/sqrt(2), independent
        # of FFT window choice. This catches Hann equivalent-noise-bandwidth bias.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{BASE_MS}.wav"
            samples = .1 * np.sin(2 * np.pi * 1000 * np.arange(RATE) / RATE)
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(RATE)
                output.writeframes(np.rint(samples * 32767).astype("<i2").tobytes())
            recordings, _ = inspect_recordings([path])
            powers = [block[4].sum(axis=1)
                      for block in _spectral_batches(recordings, CalibrationConfig(), np.arange(0, 2001, 50))]
            measured_dbfs = 10 * math.log10(float(np.mean(np.concatenate(powers))))
            self.assertAlmostEqual(measured_dbfs, 20 * math.log10(.1 / math.sqrt(2)), delta=.03)


if __name__ == "__main__":
    unittest.main()
