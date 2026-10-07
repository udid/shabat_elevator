"""Compact runtime export and CLI contracts, using reports rather than audio."""

import contextlib
import copy
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_calibrate_audio
from run_calibrate_audio import CalibrationInputError
from elevator.runtime_config import build_runtime_config


def ready_report():
    return {
        "schemaVersion": 2,
        "status": "ready",
        "reason": "repeated_strong_sound_separated_from_weaker_matches",
        "eventKind": "departure",
        "floor": 7,
        "periodSeconds": 570.25,
        "createdAt": "2023-12-23T00:00:00Z",
        "matchingEvents": [{"observedAt": "2023-12-23T01:00:00Z", "sourceFile": "PRIVATE_SOURCE_PATH.wav"}],
        "source": {"files": [{"path": "PRIVATE_SOURCE_PATH.wav", "sha256": "PRIVATE_HISTORY_HASH"}]},
        "history": [{"oldTimestamp": "2023-12-23T01:00:00Z"}],
        "alternatives": [{"periodSeconds": 1140.5}],
        "intensitySeparation": {"clear": True, "thresholdDbfs": -38.5, "gapDb": 12,
                                "weakExcludedCount": 9, "strongMatchedCount": 4},
        "parameters": {
            "algorithm": "hann_spectral_band_snr_level_v2",
            "channel": 0,
            "frequencyLowHz": 2850,
            "frequencyHighHz": 3200,
            "snrThresholdDb": 14.0,
            "noiseBandPower": 1.5e-9,
            "minimumEventBandDbfs": -38.5,
            "minimumRmsDbfs": -85.0,
            "minimumEventSeconds": .3,
            "maximumEventSeconds": 2.5,
            "mergeGapSeconds": 1.5,
            "rearmSeconds": 4.0,
            "minimumTonalFraction": .25,
            "spectralFingerprint": [0.001] * 4096,
            "fingerprintBinEdgesHz": list(range(4096)),
            "referenceNoisePowerByBin": [1e-9] * 4096,
            "unknownFutureSourceField": "PRIVATE_SOURCE_PATH.wav",
        },
    }


DETECTOR_FIELDS = {
    "profile", "channel", "frequencyLowHz", "frequencyHighHz", "snrThresholdDb",
    "noiseBandPower", "minimumEventBandDbfs", "minimumRmsDbfs",
    "minimumEventSeconds", "maximumEventSeconds", "mergeGapSeconds", "rearmSeconds",
}


class RuntimeConfigurationTests(unittest.TestCase):
    def test_compact_allowlist_preserves_operating_values_without_history_or_arrays(self):
        report = ready_report()
        original = copy.deepcopy(report)
        runtime = build_runtime_config(report)
        self.assertEqual(set(runtime), {"schemaVersion", "eventKind", "floor", "defaultCycleSeconds", "detector"})
        self.assertEqual(runtime["schemaVersion"], 1)
        self.assertEqual(runtime["eventKind"], "departure")
        self.assertEqual(runtime["floor"], 7)
        self.assertEqual(runtime["defaultCycleSeconds"], report["periodSeconds"])
        self.assertEqual(set(runtime["detector"]), DETECTOR_FIELDS)
        self.assertEqual(runtime["detector"]["profile"], "band_level_snr_v1")
        for key in DETECTOR_FIELDS - {"profile"}:
            self.assertEqual(runtime["detector"][key], report["parameters"][key])
        serialized = json.dumps(runtime, allow_nan=False)
        for forbidden in ("PRIVATE_SOURCE_PATH", "PRIVATE_HISTORY_HASH", "2023-12-23", "spectralFingerprint",
                          "sourceFile", "referenceNoisePowerByBin", "minimumTonalFraction"):
            self.assertNotIn(forbidden, serialized)
        self.assertLess(len(serialized), 3000)
        self.assertEqual(report, original, "Export must not remove fields from the detailed report")

    def test_review_and_insufficient_reports_never_build_active_runtime_configuration(self):
        for status in ("review", "insufficient_evidence", "failed", None):
            with self.subTest(status=status):
                report = ready_report()
                report["status"] = status
                with self.assertRaises(ValueError):
                    build_runtime_config(report)

    def test_invalid_periods_are_rejected_instead_of_clamped_or_serialized(self):
        for value in (None, True, "570", 0, 299.99, 300, 1800, 1800.01, math.nan, math.inf, -math.inf):
            with self.subTest(period=value):
                report = ready_report()
                report["periodSeconds"] = value
                with self.assertRaises(ValueError):
                    build_runtime_config(report)
        for value in (300.001, 570, 1799.999):
            with self.subTest(valid_period=value):
                report = ready_report()
                report["periodSeconds"] = value
                self.assertEqual(build_runtime_config(report)["defaultCycleSeconds"], value)

    def test_report_identity_and_algorithm_must_match_the_supported_departure_profile(self):
        for key, value in (("schemaVersion", 1), ("eventKind", "arrival"), ("floor", 8)):
            with self.subTest(key=key, value=value):
                report = ready_report()
                report[key] = value
                with self.assertRaises(ValueError):
                    build_runtime_config(report)
        report = ready_report()
        report["parameters"]["algorithm"] = "unknown_detector"
        with self.assertRaises(ValueError):
            build_runtime_config(report)

    def test_invalid_detector_values_cannot_reach_the_runtime_file(self):
        cases = {
            "channel": (-1, True, .5),
            "frequencyLowHz": (0, 3200, math.nan),
            "frequencyHighHz": (2850, -1, math.inf),
            "snrThresholdDb": (-1, math.nan),
            "noiseBandPower": (0, -1, math.inf),
            "minimumEventBandDbfs": (math.nan, math.inf, "-38"),
            "minimumRmsDbfs": (None, math.nan),
            "minimumEventSeconds": (0, 3, math.inf),
            "maximumEventSeconds": (.1, 300, math.inf),
            "mergeGapSeconds": (-1, math.nan),
            "rearmSeconds": (0, -1, math.nan),
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(parameter=key, value=value):
                    report = ready_report()
                    report["parameters"][key] = value
                    with self.assertRaises(ValueError):
                        build_runtime_config(report)

    def test_only_optional_absolute_level_may_be_null_and_equal_duration_limits_are_valid(self):
        report = ready_report()
        report["parameters"]["minimumEventBandDbfs"] = None
        report["parameters"]["maximumEventSeconds"] = report["parameters"]["minimumEventSeconds"]
        runtime = build_runtime_config(report)
        self.assertIsNone(runtime["detector"]["minimumEventBandDbfs"])
        self.assertEqual(runtime["detector"]["minimumEventSeconds"], runtime["detector"]["maximumEventSeconds"])


class RuntimeExportCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, report, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("run_calibrate_audio.calibrate", return_value=report) as calibration, \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = run_calibrate_audio.main(["never-open-this.wav", "--quiet", *map(str, arguments)])
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue(), calibration

    def test_default_runtime_path_derives_from_report_stem_and_both_outputs_are_written(self):
        report_path = self.directory / "chosen.summary.json"
        runtime_path = self.directory / "chosen.summary.runtime.json"
        report = ready_report()
        code, stdout, stderr, calibration = self.invoke(report, ["--output", report_path])
        self.assertEqual(code, 0, stdout + stderr)
        calibration.assert_called_once()
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)
        self.assertEqual(json.loads(runtime_path.read_text(encoding="utf-8")), build_runtime_config(report))

    def test_custom_runtime_destination_is_separate_from_detailed_report(self):
        report_path = self.directory / "report.json"
        runtime_path = self.directory / "device" / "floor7.json"
        report = ready_report()
        code, stdout, stderr, _ = self.invoke(report, ["--output", report_path, "--runtime-output", runtime_path])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertTrue(report_path.is_file())
        self.assertEqual(json.loads(runtime_path.read_text(encoding="utf-8")), build_runtime_config(report))
        self.assertFalse((self.directory / "report.runtime.json").exists())

    def test_same_resolved_paths_are_rejected_before_either_file_is_overwritten(self):
        target = self.directory / "keep.json"
        target.write_bytes(b"existing report/runtime")
        alias = self.directory / "unused" / ".." / "keep.json"
        code, _, _, calibration = self.invoke(ready_report(), ["--output", target, "--runtime-output", alias])
        self.assertEqual(code, 2)
        self.assertEqual(target.read_bytes(), b"existing report/runtime")
        calibration.assert_not_called()

    def test_runtime_output_requires_json_extension_and_cannot_overwrite_wav(self):
        target = self.directory / "source.wav"
        target.write_bytes(b"existing source audio")
        code, _, _, calibration = self.invoke(ready_report(), ["--output", self.directory / "report.json", "--runtime-output", target])
        self.assertEqual(code, 2)
        self.assertEqual(target.read_bytes(), b"existing source audio")
        calibration.assert_not_called()

    def test_review_or_insufficient_writes_report_but_preserves_existing_runtime(self):
        for status in ("review", "insufficient_evidence"):
            with self.subTest(status=status):
                report_path = self.directory / f"{status}.json"
                runtime_path = self.directory / f"{status}.runtime.json"
                runtime_path.write_bytes(b"previous trusted runtime")
                report = ready_report()
                report["status"] = status
                code, stdout, stderr, _ = self.invoke(report, ["--output", report_path])
                self.assertEqual(code, 1, stdout + stderr)
                self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["status"], status)
                self.assertEqual(runtime_path.read_bytes(), b"previous trusted runtime")
                self.assertIn("runtime", (stdout + stderr).lower())

    def test_insufficient_evidence_without_candidate_creates_no_runtime_file(self):
        report_path = self.directory / "no-candidate.json"
        report = ready_report()
        report.update(status="insufficient_evidence", periodSeconds=None, parameters=None, matchingEvents=[])
        report["intensitySeparation"] = {"clear": False}
        code, stdout, stderr, _ = self.invoke(report, ["--output", report_path])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertTrue(report_path.exists())
        self.assertFalse((self.directory / "no-candidate.runtime.json").exists())

    def test_failed_input_does_not_touch_existing_runtime(self):
        report_path = self.directory / "report.json"
        runtime_path = self.directory / "report.runtime.json"
        runtime_path.write_bytes(b"previous trusted runtime")
        with patch("run_calibrate_audio.calibrate", side_effect=CalibrationInputError("bad input")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = run_calibrate_audio.main(["never-open-this.wav", "--quiet", "--output", str(report_path)])
        self.assertEqual(code, 2)
        self.assertFalse(report_path.exists())
        self.assertEqual(runtime_path.read_bytes(), b"previous trusted runtime")

    def test_failed_runtime_validation_preserves_existing_runtime(self):
        report_path = self.directory / "report.json"
        runtime_path = self.directory / "report.runtime.json"
        runtime_path.write_bytes(b"previous trusted runtime")
        report = ready_report()
        report["periodSeconds"] = 1800
        code, stdout, stderr, _ = self.invoke(report, ["--output", report_path])
        self.assertEqual(code, 2, stdout + stderr)
        self.assertEqual(runtime_path.read_bytes(), b"previous trusted runtime")

    def test_failed_atomic_replacement_returns_error_and_preserves_existing_runtime(self):
        original_replace = Path.replace
        for fail_at in ("report", "runtime"):
            with self.subTest(fail_at=fail_at):
                report_path = self.directory / f"{fail_at}.json"
                runtime_path = self.directory / f"{fail_at}.runtime.json"
                runtime_path.write_bytes(b"previous trusted runtime")
                failing_path = report_path if fail_at == "report" else runtime_path

                def fail_selected_replace(source, target):
                    if Path(target) == failing_path:
                        raise OSError("simulated storage replacement failure")
                    return original_replace(source, target)

                with patch.object(Path, "replace", fail_selected_replace):
                    code, stdout, stderr, _ = self.invoke(ready_report(), ["--output", report_path])
                self.assertEqual(code, 2, stdout + stderr)
                self.assertEqual(runtime_path.read_bytes(), b"previous trusted runtime")
                self.assertFalse(list(self.directory.glob("*.tmp")), "Failed writes must clean their temporary file")


if __name__ == "__main__":
    unittest.main()
