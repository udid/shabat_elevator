"""Automatically separate two periodic trains of the same chime without labels."""
import json
import tempfile
import unittest
import wave
import numpy as np
from test_audio_levels import write_level_fixture, BASE_MS, RATE
from run_calibrate_audio import CalibrationConfig, CalibrationInputError, calibrate, inspect_recordings
from elevator.runtime_config import build_runtime_config, validate_runtime_config


class AutomaticCalibrationTests(unittest.TestCase):
    def test_five_db_gap_selects_stronger_periodic_train_without_a_reference_time(self):
        strong = [(30+570*i, .06) for i in range(4)]
        other = [(150+570*i, .032) for i in range(3)]
        with tempfile.TemporaryDirectory() as folder:
            path = write_level_fixture(folder, strong+other)
            result = calibrate([path])
        self.assertEqual(result['status'], 'ready', (result['reason'],result.get('alternatives')))
        self.assertAlmostEqual(result['periodSeconds'],570,delta=.2)
        self.assertEqual(len(result['matchingEvents']),4)
        for event,(expected,_) in zip(result['matchingEvents'],strong):
            self.assertAlmostEqual((event['epochMs']-BASE_MS)/1000,expected,delta=.5)
        runtime=build_runtime_config(result)
        self.assertEqual(runtime['cycleTolerancePercent'],2)
        self.assertAlmostEqual(runtime['detector']['minimumFrameBandDbfs'],runtime['detector']['minimumEventBandDbfs']-6)
        self.assertGreater(runtime['detector']['minimumTonalFraction'],0)
        self.assertEqual(validate_runtime_config(runtime),runtime)

    def test_equal_level_competing_trains_require_review(self):
        events=[(30+570*i,.06) for i in range(4)]+[(150+570*i,.06) for i in range(3)]
        with tempfile.TemporaryDirectory() as folder:
            path=write_level_fixture(folder,events)
            result=calibrate([path])
        self.assertNotEqual(result['status'],'ready')

    def test_faint_tone_around_departures_does_not_erase_the_periodic_train(self):
        strong = [(30 + 570 * i, .06) for i in range(4)]
        other = [(150 + 570 * i, .032) for i in range(3)]
        with tempfile.TemporaryDirectory() as folder:
            path = write_level_fixture(folder, strong + other)
            with wave.open(str(path), 'rb') as source:
                values = np.frombuffer(source.readframes(source.getnframes()), '<i2').astype(np.float32) / 32768
            times = np.arange(len(values)) / RATE
            for onset, _ in strong:
                nearby = (times >= onset - 15) & (times < onset + 25)
                values[nearby] += .0015 * np.sin(2 * np.pi * 1000 * times[nearby])
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(RATE)
                output.writeframes(np.rint(values * 32767).astype('<i2').tobytes())
            result = calibrate([path])
        self.assertEqual(result['status'], 'ready', result['reason'])
        self.assertAlmostEqual(result['periodSeconds'], 570, delta=.2)
        self.assertEqual(len(result['matchingEvents']), 4)
        self.assertIsNotNone(result['parameters']['minimumFrameBandDbfs'])

    def test_changing_low_frequency_hum_does_not_change_chime_identity(self):
        strong = [(30 + 570 * i, .06) for i in range(4)]
        other = [(150 + 570 * i, .032) for i in range(3)]
        with tempfile.TemporaryDirectory() as folder:
            path = write_level_fixture(folder, strong + other)
            with wave.open(str(path), 'rb') as source:
                values = np.frombuffer(source.readframes(source.getnframes()), '<i2').astype(np.float32) / 32768
            times = np.arange(len(values)) / RATE
            for index, (onset, _) in enumerate(strong + other):
                nearby = (times >= onset) & (times < onset + .7)
                values[nearby] += .2 * np.sin(2 * np.pi * (70 + 15 * index) * times[nearby])
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(RATE)
                output.writeframes(np.rint(values * 32767).astype('<i2').tobytes())
            result = calibrate([path])
        self.assertEqual(result['status'], 'ready', result['reason'])
        self.assertAlmostEqual(result['periodSeconds'], 570, delta=.2)
        self.assertEqual(len(result['matchingEvents']), 4)

    def test_unfinished_recordings_are_not_calibration_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path=write_level_fixture(folder,[],duration=2)
            path.with_suffix('.json').write_text(json.dumps({'managedBy':'shabat-elevator-diagnostics-v1','closed':False}))
            with self.assertRaises(CalibrationInputError):inspect_recordings([path])
    def test_long_timelines_get_automatic_analysis_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            first=write_level_fixture(folder,[],duration=2,seed=1)
            second=write_level_fixture(folder,[],duration=2,start=7201,seed=2)
            recordings,_=inspect_recordings([first,second],config=CalibrationConfig(session_gap_seconds=10000))
        self.assertNotEqual(recordings[0].session,recordings[1].session)
