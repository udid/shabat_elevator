import contextlib
import io
import json
import queue
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from elevator.audio_recorder import (
    AudioBlock, CaptureBridge, Manifest, RecorderConfig, RecorderError,
    WaveChunkWriter, main, record_audio,
)


EPOCH_NS = 1_800_000_000_000_000_000


def pcm(values):
    return struct.pack(f"<{len(values)}h", *values)


def read_manifest(folder):
    return [json.loads(line) for line in (Path(folder) / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]


class FakeAbort(Exception):
    pass


class FakeSoundDevice:
    CallbackAbort = FakeAbort

    def __init__(self, blocks=(), *, active=True):
        self.blocks = blocks
        self.active = active
        self.streams_opened = 0
        self.stopped = False
        self.checked = None

    def check_input_settings(self, **kwargs):
        self.checked = kwargs

    def query_devices(self):
        return "0 Fake microphone (1 in, 0 out)"

    def RawInputStream(self, **kwargs):
        owner = self

        class FakeStream:
            def __enter__(self):
                owner.streams_opened += 1
                self.active = owner.active
                for values, adc, status in owner.blocks:
                    try:
                        kwargs["callback"](pcm(values), len(values),
                                           SimpleNamespace(inputBufferAdcTime=adc, currentTime=adc + .1), status)
                    except FakeAbort:
                        self.active = False
                        kwargs["finished_callback"]()
                        break
                return self

            def __exit__(self, *args):
                owner.stopped = True
                self.active = False
                kwargs["finished_callback"]()

        return FakeStream()


class RecorderWriterTests(unittest.TestCase):
    def test_exact_rotation_crosses_input_blocks_preserves_pcm_and_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            config = RecorderConfig(output=Path(directory), sample_rate=10, chunk_seconds=.5)
            manifest = Manifest(Path(directory) / "manifest.jsonl", "test")
            writer = WaveChunkWriter(config, manifest, EPOCH_NS, 5_000_000_000)
            writer.consume(AudioBlock(pcm(list(range(7))), 7, 10, 10.1, 5_100_000_000))
            # Callback delivery is late, but ADC time is continuous: do not call this a gap.
            writer.consume(AudioBlock(pcm(list(range(7, 12))), 5, 10.7, 10.8, 99_000_000_000))
            writer.close()
            manifest.close()
            files = sorted(Path(directory).glob("*.wav"))
            self.assertEqual([file.name for file in files], ["1800000000000.wav", "1800000000500.wav", "1800000001000.wav"])
            recovered = b""
            frame_counts = []
            for file in files:
                with wave.open(str(file), "rb") as recording:
                    self.assertEqual((recording.getnchannels(), recording.getsampwidth(), recording.getframerate()), (1, 2, 10))
                    frame_counts.append(recording.getnframes())
                    recovered += recording.readframes(recording.getnframes())
            self.assertEqual(frame_counts, [5, 5, 2])
            self.assertEqual(recovered, pcm(list(range(12))))
            records = [entry for entry in read_manifest(directory) if entry["type"] == "recording"]
            self.assertEqual([entry["complete"] for entry in records], [True, True, False])
            self.assertEqual(records[0]["endUtc"], records[1]["startUtc"])

    def test_exact_multiple_creates_no_empty_trailing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Manifest(Path(directory) / "manifest.jsonl", "test")
            writer = WaveChunkWriter(RecorderConfig(output=Path(directory), sample_rate=10, chunk_seconds=.5), manifest, EPOCH_NS, 0)
            writer.consume(AudioBlock(pcm([1] * 10), 10, 0, 0, 0))
            writer.close()
            manifest.close()
            self.assertEqual(len(list(Path(directory).glob("*.wav"))), 2)

    def test_filename_collision_does_not_overwrite_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "1800000000000.wav"
            original.write_bytes(b"existing recording")
            manifest = Manifest(Path(directory) / "manifest.jsonl", "test")
            writer = WaveChunkWriter(RecorderConfig(output=Path(directory), sample_rate=10), manifest, EPOCH_NS, 0)
            with self.assertRaises(FileExistsError):
                writer.consume(AudioBlock(pcm([1]), 1, 0, 0, 0))
            writer.close()
            manifest.close()
            self.assertEqual(original.read_bytes(), b"existing recording")

    def test_discontinuous_adc_rejects_block_without_filling_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Manifest(Path(directory) / "manifest.jsonl", "test")
            config = RecorderConfig(output=Path(directory), sample_rate=1000)
            writer = WaveChunkWriter(config, manifest, EPOCH_NS, 0)
            writer.consume(AudioBlock(pcm([1] * 100), 100, 10, 10.1, 0))
            with self.assertRaisesRegex(RecorderError, "discontinuity"):
                writer.consume(AudioBlock(pcm([2] * 100), 100, 10.3, 10.4, 0))
            writer.close()
            manifest.close()
            self.assertEqual(writer.total_frames, 100)
            with wave.open(str(next(Path(directory).glob("*.wav"))), "rb") as recording:
                self.assertEqual(recording.getnframes(), 100)


class CaptureTests(unittest.TestCase):
    def record(self, directory, device, **kwargs):
        config = RecorderConfig(output=Path(directory), sample_rate=1000, chunk_seconds=1, **kwargs)
        return record_audio(config, sounddevice_module=device, wall_ns=lambda: EPOCH_NS, monotonic_ns=lambda: 1_000_000_000)

    def test_finite_capture_trims_last_block_and_uses_single_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            device = FakeSoundDevice([([1] * 700, 10, None), ([2] * 700, 10.7, None)])
            summary = self.record(directory, device, duration_minutes=1.2 / 60)
            self.assertEqual(summary["frames"], 1200)
            self.assertEqual(summary["outcome"], "complete")
            self.assertEqual(device.streams_opened, 1)
            self.assertTrue(device.stopped)
            self.assertEqual(device.checked["dtype"], "int16")
            entries = read_manifest(directory)
            self.assertEqual(entries[0]["type"], "session_start")
            self.assertEqual(entries[-1]["type"], "session_end")
            self.assertEqual([entry["frames"] for entry in entries if entry["type"] == "recording"], [1000, 200])

    def test_ctrl_c_finalizes_partial_file_and_succeeds(self):
        class InterruptWhenEmpty(queue.Queue):
            def get(self, *args, **kwargs):
                if self.empty():
                    raise KeyboardInterrupt
                return super().get(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            device = FakeSoundDevice([([4] * 100, 10, None)])
            with patch("elevator.audio_recorder.queue.Queue", InterruptWhenEmpty):
                result = self.record(directory, device)
            self.assertEqual(result["outcome"], "interrupted")
            self.assertEqual(result["frames"], 100)
            with wave.open(str(next(Path(directory).glob("*.wav"))), "rb") as recording:
                self.assertEqual(recording.getnframes(), 100)
            self.assertFalse(next(entry for entry in read_manifest(directory) if entry["type"] == "recording")["complete"])

    def test_ctrl_c_drain_never_exceeds_requested_duration(self):
        class InterruptFirstGet(queue.Queue):
            interrupted = False

            def get(self, *args, **kwargs):
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt
                return super().get(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            device = FakeSoundDevice([([1] * 100, 10, None), ([2] * 100, 10.1, None)])
            with patch("elevator.audio_recorder.queue.Queue", InterruptFirstGet):
                result = self.record(directory, device, duration_minutes=.15 / 60)
            self.assertEqual(result["frames"], 150)
            self.assertEqual(result["outcome"], "interrupted")

    def test_input_error_is_fatal_and_manifest_records_prior_good_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            device = FakeSoundDevice([([1] * 100, 10, None), ([2] * 100, 10.1, "input overflow")])
            with self.assertRaisesRegex(RecorderError, "PortAudio"):
                self.record(directory, device)
            entries = read_manifest(directory)
            self.assertEqual(entries[-1]["outcome"], "error")
            self.assertEqual(entries[-1]["frames"], 100)
            error = next(entry for entry in entries if entry["type"] == "error")
            self.assertTrue(error["droppedFramesUnknown"])

    def test_queue_overflow_is_fatal_and_not_a_successful_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            device = FakeSoundDevice([([1] * 100, 10, None), ([2] * 100, 10.1, None)])
            with self.assertRaisesRegex(RecorderError, "queue overflow"):
                self.record(directory, device, queue_blocks=1)
            error = next(entry for entry in read_manifest(directory) if entry["type"] == "error")
            self.assertEqual(error["stage"], "queue")
            self.assertEqual(error["droppedFrames"], 100)

    def test_failure_after_requested_samples_does_not_invalidate_finite_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            device = FakeSoundDevice([([1] * 100, 10, None), ([2] * 100, 10.1, "input overflow")])
            result = self.record(directory, device, duration_minutes=.1 / 60)
            self.assertEqual(result["frames"], 100)
            self.assertEqual(result["outcome"], "complete")
            self.assertFalse(any(entry["type"] == "error" for entry in read_manifest(directory)))

    def test_unexpected_stream_stop_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RecorderError, "stopped unexpectedly"):
                self.record(directory, FakeSoundDevice(active=False))
            self.assertEqual(read_manifest(directory)[-1]["outcome"], "error")
            self.assertEqual(list(Path(directory).glob("*.wav")), [])

    def test_live_stream_without_samples_times_out(self):
        ticks = iter([0, 0, 0, 11_000_000_000])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RecorderError, "No microphone samples"):
                record_audio(RecorderConfig(output=Path(directory)), sounddevice_module=FakeSoundDevice(),
                             wall_ns=lambda: EPOCH_NS, monotonic_ns=lambda: next(ticks))

    def test_invalid_input_timestamps_abort_callback(self):
        bridge = CaptureBridge(RecorderConfig(), FakeAbort)
        with self.assertRaises(FakeAbort):
            bridge.callback(pcm([1]), 1, SimpleNamespace(inputBufferAdcTime=float("nan"), currentTime=0), None)
        self.assertIn("timestamps", str(bridge.failure))
        self.assertTrue(bridge.blocks.empty())

    def test_wall_clock_is_sampled_once_for_contiguous_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            wall = unittest.mock.Mock(return_value=EPOCH_NS)
            config = RecorderConfig(output=Path(directory), sample_rate=1000, chunk_seconds=.1, duration_minutes=.2 / 60)
            record_audio(config, sounddevice_module=FakeSoundDevice([([1] * 200, 10, None)]),
                         wall_ns=wall, monotonic_ns=lambda: 0)
            wall.assert_called_once_with()
            records = [entry for entry in read_manifest(directory) if entry["type"] == "recording"]
            self.assertEqual(records[1]["startEpochMs"] - records[0]["startEpochMs"], 100)

    def test_help_and_list_devices_never_open_input(self):
        with patch("elevator.audio_recorder.load_sounddevice") as load, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_result:
                main(["--help"])
            self.assertEqual(exit_result.exception.code, 0)
            load.assert_not_called()
        device = FakeSoundDevice()
        with patch("elevator.audio_recorder.load_sounddevice", return_value=device), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--list-devices"]), 0)
        self.assertEqual(device.streams_opened, 0)

    def test_cli_reports_capture_failure_nonzero(self):
        with patch("elevator.audio_recorder.load_sounddevice", return_value=FakeSoundDevice()), \
             patch("elevator.audio_recorder.record_audio", side_effect=RecorderError("input disconnected")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([]), 1)


if __name__ == "__main__":
    unittest.main()
