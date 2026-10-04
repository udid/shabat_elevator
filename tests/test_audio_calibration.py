import json
import math
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from elevator.audio_calibration import (
    CalibrationConfig, CalibrationInputError, _EventBuilder, _evaluate_group,
    _spectral_batches, calibrate, decode_pcm, inspect_recordings,
)


BASE_MS = 1_700_000_000_000
RATE = 4000


def write_fixture(folder, duration, departures=(), *, start=0, seed=10, distractors=(), mode='chirp'):
    path = Path(folder) / f'{BASE_MS + round(start * 1000)}.wav'
    rng = np.random.default_rng(seed)
    with wave.open(str(path), 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(RATE)
        for sample in range(0, round(duration * RATE), RATE * 10):
            count = min(RATE * 10, round(duration * RATE) - sample)
            times = start + (sample + np.arange(count)) / RATE
            data = rng.normal(0, 0.00015, count) if mode != 'silence' else np.zeros(count)
            if mode == 'hum':
                data += .04 * np.sin(2 * np.pi * 1100 * times)
            for arrival in departures:
                local = times - arrival
                active = (local >= 0) & (local < .7)
                offset = local[active]
                if mode == 'broadband':
                    data[active] += rng.normal(0, .06, active.sum())
                else:
                    # Fixed acoustic shape, frequency sweep 900 -> 1150 Hz over 0.7 s.
                    data[active] += .07 * np.sin(np.pi * offset / .7) ** 2 * np.sin(2 * np.pi * (900 * offset + .5 * (250 / .7) * offset ** 2))
            for arrival, frequency in distractors:
                local = times - arrival
                active = (local >= 0) & (local < .4)
                offset = local[active]
                data[active] += .03 * np.sin(np.pi * offset / .4) ** 2 * np.sin(2 * np.pi * frequency * offset)
            target.writeframesraw(np.clip(data * 32768, -32768, 32767).astype('<i2').tobytes())
    return path


class AudioCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_repeated_chirp_recovers_cycle_with_unrelated_distractors(self):
        departures = [30, 600, 1170, 1740]
        file = write_fixture(self.folder, 1780, departures,
                             distractors=[(250, 1650), (920, 1650), (1490, 1500)])
        result = calibrate([file])
        self.assertEqual(result['status'], 'ready', (result['reason'], result.get('support'), result['alternatives']))
        self.assertAlmostEqual(result['periodSeconds'], 570, delta=.15)
        self.assertEqual(result['eventKind'], 'departure')
        self.assertEqual(result['floor'], 7)
        self.assertEqual(len(result['matchingEvents']), 4)
        self.assertLess(result['parameters']['frequencyLowHz'], 1150)
        self.assertGreater(result['parameters']['frequencyHighHz'], 900)
        for event, expected in zip(result['matchingEvents'], departures):
            self.assertAlmostEqual((event['epochMs'] - BASE_MS) / 1000, expected, delta=.4)
            self.assertIsNotNone(event['sourceSampleOffset'])
        json.dumps(result, allow_nan=False)

    def test_missing_cycle_keeps_fundamental_and_excludes_a_recording_gap(self):
        # The expected departure at1170 is not recorded, and cannot count as silence.
        first = write_fixture(self.folder, 1000, [30, 600], seed=1)
        second = write_fixture(self.folder, 1040, [1740, 2310], start=1300, seed=2)
        result = calibrate([first, second])
        self.assertEqual(result['status'], 'ready', (result['reason'], result.get('support')))
        self.assertAlmostEqual(result['periodSeconds'], 570, delta=.15)
        self.assertEqual(result['support']['cyclesInsideRecordingGaps'], 1)
        self.assertEqual(result['support']['missedEventsInCoveredAudio'], 0)
        self.assertAlmostEqual(result['source']['gaps'][0]['seconds'], 300)

    def test_silence_hum_and_periodic_broadband_noise_are_not_forced_matches(self):
        for index, mode in enumerate(('silence', 'hum', 'broadband')):
            with self.subTest(mode=mode):
                directory = self.folder / mode
                directory.mkdir()
                file = write_fixture(directory, 1260, [30, 630, 1230] if mode == 'broadband' else [], seed=index, mode=mode)
                result = calibrate([file])
                self.assertNotEqual(result['status'], 'ready', (mode, result['reason'], result.get('periodSeconds')))

    def test_competing_distinct_periodic_tones_require_review(self):
        file = write_fixture(self.folder, 1840, [30, 630, 1230, 1830],
                             distractors=[(100, 1650), (660, 1650), (1220, 1650), (1780, 1650)])
        result = calibrate([file])
        self.assertEqual(result['status'], 'review', (result['reason'], result['alternatives']))
        self.assertEqual(result['reason'], 'competing_periodic_sound_patterns')
        self.assertTrue(result['alternatives'])

    def test_separate_weeks_cannot_combine_two_events_each_as_three_repeats(self):
        first = write_fixture(self.folder, 640, [30, 600], seed=1)
        second = write_fixture(self.folder, 640, [604830, 605400], start=604800, seed=2)
        result = calibrate([first, second])
        self.assertEqual(result['source']['sessions'], 2)
        self.assertEqual(result['status'], 'insufficient_evidence')

    def test_duplicates_excluded_and_conflicting_timestamp_or_overlap_rejected(self):
        original = write_fixture(self.folder, 10, [1])
        copies = self.folder / 'copies'
        copies.mkdir()
        exact_copy = copies / original.name
        shutil.copyfile(original, exact_copy)
        renamed = copies / f'{BASE_MS + 100000}.wav'
        shutil.copyfile(original, renamed)
        recordings, excluded = inspect_recordings([original, original, exact_copy, renamed])
        self.assertEqual(len(recordings), 1)
        self.assertEqual(len(excluded), 3)
        write_fixture(copies, 10, [2], seed=22)
        with self.assertRaisesRegex(CalibrationInputError, 'Conflicting'):
            inspect_recordings([original, exact_copy])
        overlapping = write_fixture(copies, 10, [6], start=5, seed=12)
        with self.assertRaisesRegex(CalibrationInputError, 'Overlapping'):
            inspect_recordings([original, overlapping])

    def test_exact_and_outside_cycle_boundaries_cannot_be_accepted_as_multiples(self):
        settings = {'frequencyLowHz': 900, 'frequencyHighHz': 1200}
        for period in (240, 300, 1800):
            with self.subTest(period=period):
                fingerprint = np.array([1.0, 0.0])
                events = [{'time': float(BASE_MS / 1000 + n * period), 'duration': .7,
                           'fingerprint': fingerprint, 'session': 0} for n in range(7)]
                group = {'events': events, 'prototype': fingerprint, 'duration': .7, 'session': 0}
                # A lightweight coverage object exercises the actual cycle inference.
                recording = type('Coverage', (), {'session': 0, 'start': BASE_MS / 1000 - 10,
                                                  'end': BASE_MS / 1000 + period * 7})()
                results = _evaluate_group(group, _EventBuilder(CalibrationConfig(), settings), [recording], CalibrationConfig())
                self.assertFalse(any(item['ready'] for item in results), results)

    def test_only_missing_alternating_events_cannot_establish_an_unsupported_divisor(self):
        fingerprint = np.array([1.0, 0.0])
        events = [{'time': float(BASE_MS / 1000 + x), 'duration': .7,
                   'fingerprint': fingerprint, 'session': 0} for x in [30, 630, 1830, 2430]]
        group = {'events': events, 'prototype': fingerprint, 'duration': .7, 'session': 0}
        recording = type('Coverage', (), {'session': 0, 'start': BASE_MS / 1000,
                                          'end': BASE_MS / 1000 + 2500})()
        results = _evaluate_group(group, _EventBuilder(CalibrationConfig(), {}), [recording], CalibrationConfig())
        ready = [item for item in results if item['ready']]
        self.assertTrue(ready)
        self.assertAlmostEqual(max(ready, key=lambda item: item['score'])['periodSeconds'], 600)
        self.assertEqual(max(ready, key=lambda item: item['score'])['support']['missedEventsInCoveredAudio'], 1)

    def test_short_periodic_burst_does_not_explain_two_hours_of_recording(self):
        fingerprint = np.array([1.0, 0.0])
        events = [{'time': float(BASE_MS / 1000 + x), 'duration': .7,
                   'fingerprint': fingerprint, 'session': 0} for x in [1830, 2430, 3030, 3630]]
        group = {'events': events, 'prototype': fingerprint, 'duration': .7, 'session': 0}
        recording = type('Coverage', (), {'session': 0, 'start': BASE_MS / 1000,
                                          'end': BASE_MS / 1000 + 7200})()
        results = _evaluate_group(group, _EventBuilder(CalibrationConfig(), {}), [recording], CalibrationConfig())
        self.assertTrue(results)
        self.assertFalse(any(item['ready'] for item in results))
        best = max(results, key=lambda item: item['score'])
        self.assertEqual(best['support']['missedEventsInCoveredAudio'], 8)

    def test_nonfinite_configuration_is_rejected(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            for key in ('timing_tolerance_seconds', 'merge_gap_seconds'):
                with self.subTest(value=value, key=key), self.assertRaises(CalibrationInputError):
                    CalibrationConfig(**{key: value}).validate()

    def test_contiguous_files_keep_overlapping_fft_windows_at_the_seam(self):
        first = write_fixture(self.folder, 1, [.9], seed=1)
        second = write_fixture(self.folder, 1, [], start=1, seed=2)
        recordings, _ = inspect_recordings([first, second])
        frames = list(_spectral_batches(recordings, CalibrationConfig(), np.arange(0, 2001, 50)))
        segments = {block[0] for block in frames}
        times = np.concatenate([block[2] for block in frames]) - BASE_MS / 1000
        self.assertEqual(len(segments), 1)
        self.assertTrue(np.any((times < 1) & (times + .064 > 1)))

    def test_pcm_widths_have_the_same_full_scale_units(self):
        self.assertAlmostEqual(float(decode_pcm(bytes([192]), 1, 1)[0]), .5)
        self.assertAlmostEqual(float(decode_pcm(np.array([16384], '<i2').tobytes(), 2, 1)[0]), .5)
        self.assertAlmostEqual(float(decode_pcm(bytes([0, 0, 64]), 3, 1)[0]), .5)
        self.assertAlmostEqual(float(decode_pcm(np.array([1073741824], '<i4').tobytes(), 4, 1)[0]), .5)
        self.assertAlmostEqual(float(decode_pcm(bytes([0, 0, 192]), 3, 1)[0]), -.5)

    def test_too_short_recording_gives_actionable_insufficient_evidence(self):
        file = write_fixture(self.folder, 15, [1, 10])
        result = calibrate([file])
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIsNone(result['periodSeconds'])
        self.assertIn('record_longer', result['reason'])


if __name__ == '__main__':
    unittest.main()
