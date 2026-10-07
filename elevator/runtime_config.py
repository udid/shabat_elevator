"""Export scalar runtime settings from a successful offline calibration.

``band_level_snr_v1`` is a small band/SNR/level/duration detector profile, not
the full offline spectral-fingerprint clustering model. Its level units follow
calibration v2: normalized PCM, symmetric Hann windows, FFT size equal to the
next power of two above sample_rate * .064, half-window hops, spectral power
normalized by nfft * sum(window**2), and p90 active-frame band power per event.

defaultCycleSeconds is a bootstrap duration, never an observation timestamp.
The first real departure establishes phase; the first valid interval between
two real departures replaces the default rather than averaging it as a sample.
"""

from __future__ import annotations

import math


def _number(source, key, *, nullable=False):
    if key not in source:
        raise ValueError(f"Missing calibration parameter: {key}")
    value = source[key]
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    return value


def build_runtime_config(report):
    """Return a compact allowlist; never promote an uncertain result to runtime."""
    if not isinstance(report, dict) or report.get("status") != "ready":
        raise ValueError("Runtime configuration requires a ready calibration")
    if report.get("schemaVersion") != 2:
        raise ValueError("Runtime configuration requires calibration schema version 2")
    if report.get("eventKind") != "departure" or type(report.get("floor")) is not int or report["floor"] != 7:
        raise ValueError("Runtime calibration must describe departures from floor 7")
    cycle = _number(report, "periodSeconds")
    if not 300 < cycle < 1800:
        raise ValueError("The default cycle must be strictly between 300 and 1800 seconds")
    parameters = report.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("algorithm") != "hann_spectral_band_snr_level_v2":
        raise ValueError("Unsupported calibration algorithm or missing parameters")
    channel = parameters.get("channel")
    if type(channel) is not int or channel < 0:
        raise ValueError("channel must be a nonnegative integer")

    detector = {"profile": "band_level_snr_v1", "channel": channel}
    for key in ("frequencyLowHz", "frequencyHighHz", "snrThresholdDb", "noiseBandPower",
                "minimumRmsDbfs", "minimumEventSeconds", "maximumEventSeconds",
                "mergeGapSeconds", "rearmSeconds"):
        detector[key] = _number(parameters, key)
    detector["minimumEventBandDbfs"] = _number(parameters, "minimumEventBandDbfs", nullable=True)
    if not 0 < detector["frequencyLowHz"] < detector["frequencyHighHz"]:
        raise ValueError("Frequency bounds must satisfy 0 < low < high")
    if detector["noiseBandPower"] <= 0 or detector["snrThresholdDb"] < 0:
        raise ValueError("Noise power must be positive and SNR threshold nonnegative")
    if detector["minimumRmsDbfs"] > 0 or (
        detector["minimumEventBandDbfs"] is not None and detector["minimumEventBandDbfs"] > 0
    ):
        raise ValueError("Level thresholds must not exceed 0 dBFS")
    if not 0 < detector["minimumEventSeconds"] <= detector["maximumEventSeconds"] < 300:
        raise ValueError("Event durations must satisfy 0 < minimum <= maximum < 300")
    if detector["mergeGapSeconds"] < 0 or detector["rearmSeconds"] <= 0:
        raise ValueError("Merge gap must be nonnegative and rearm time positive")
    return {"schemaVersion": 1, "eventKind": "departure", "floor": 7,
            "defaultCycleSeconds": cycle, "detector": detector}


def validate_runtime_config(value):
    """Validate a compact runtime document, returning a detached allowlist copy.

    The detailed calibration exporter remains the source of field validation.
    This adapter does not need audio dependencies and does not accept a detailed
    report (or its historic observation times) as a live runtime document.
    """
    if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise ValueError("Runtime configuration requires schema version 1")
    detector = value.get("detector")
    if not isinstance(detector, dict) or detector.get("profile") != "band_level_snr_v1":
        raise ValueError("Unsupported runtime detector profile")
    return build_runtime_config({
        "schemaVersion": 2,
        "status": "ready",
        "eventKind": value.get("eventKind"),
        "floor": value.get("floor"),
        "periodSeconds": value.get("defaultCycleSeconds"),
        "parameters": {**detector, "algorithm": "hann_spectral_band_snr_level_v2"},
    })
