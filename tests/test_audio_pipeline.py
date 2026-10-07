"""Exercise the recorder's actual WAV format through the calibration CLI."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from elevator.audio_recorder import AudioBlock, Manifest, RecorderConfig, WaveChunkWriter


class AudioPipelineTests(unittest.TestCase):
    def test_recorded_chunks_calibrate_with_a_sound_across_a_file_boundary(self):
        sample_rate = 8000
        epoch_ns = 1_700_000_000_000_000_000
        departures = (299.5, 869.5, 1439.5, 2009.5)
        rng = np.random.default_rng(17)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = Manifest(directory / "manifest.jsonl", "synthetic-pipeline")
            config = RecorderConfig(output=directory, sample_rate=sample_rate)
            writer = WaveChunkWriter(config, manifest, epoch_ns, 0)
            try:
                for second in range(2015):
                    times = second + np.arange(sample_rate) / sample_rate
                    signal = rng.normal(0, 0.001, sample_rate)
                    for departure in departures:
                        relative = times - departure
                        active = (relative >= 0) & (relative < 1.5)
                        phase = relative[active]
                        envelope = np.sin(np.pi * phase / 1.5) ** 2
                        signal[active] += envelope * (
                            0.06 * np.sin(2 * np.pi * 2800 * phase)
                            + 0.015 * np.sin(2 * np.pi * 1400 * phase)
                        )
                    pcm = np.rint(signal * 32767).astype("<i2").tobytes()
                    writer.consume(AudioBlock(pcm, sample_rate, second, second, second * 1_000_000_000))
            finally:
                writer.close()
                manifest.close()
            output = directory / "calibration.json"
            result = subprocess.run(
                [sys.executable, "run_calibrate_audio.py", str(directory), "--output", str(output)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["eventKind"], "departure")
            self.assertAlmostEqual(report["periodSeconds"], 570, delta=1)
            self.assertEqual(writer.total_frames, 2015 * sample_rate)
            self.assertEqual(writer.files, 7)


if __name__ == "__main__":
    unittest.main()
