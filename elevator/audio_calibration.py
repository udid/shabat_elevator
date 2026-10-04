"""Offline, bounded-memory calibration of a repeated floor-seven departure sound.

This is evidence fitting, not a trained floor classifier or an accuracy estimate.
Only PCM WAVs are read; this module never opens an audio device or sends events.
"""
from __future__ import annotations

import hashlib
import math
import re
import wave
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
from scipy.fft import rfft, rfftfreq


EVENT_BAND_LEVEL_METRIC = (
    '10*log10(p90 of active-frame selected-band mean-square power relative to '
    'normalized PCM full-scale squared); one-sided Hann FFT power is normalized '
    'by nfft*sum(window**2). No per-event or per-file level normalization.'
)


class CalibrationInputError(ValueError):
    """The source timeline or PCM format cannot be interpreted safely."""


@dataclass(frozen=True)
class CalibrationConfig:
    min_period_seconds: float = 300.0
    max_period_seconds: float = 1800.0
    session_gap_seconds: float = 3600.0
    timing_tolerance_seconds: float = 3.0
    min_events: int = 3
    min_event_seconds: float = 0.10
    max_event_seconds: float = 20.0
    merge_gap_seconds: float = 1.5
    spectral_bin_hz: float = 50.0
    min_frequency_hz: float = 200.0
    max_frequency_hz: float = 8000.0
    max_spectral_peaks: int = 8
    profile_frames: int = 4096
    max_events_per_detector: int = 1000
    min_rms_dbfs: float = -85.0
    min_tonal_fraction: float = 0.08
    minimum_level_separation_db: float = 6.0
    channel: int = 0
    read_frames: int = 262144

    def validate(self):
        for name, value in asdict(self).items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise CalibrationInputError(f'{name} must be a finite number')
        if self.min_period_seconds != 300 or self.max_period_seconds != 1800:
            raise CalibrationInputError('The supported cycle range is strictly 300 < seconds < 1800')
        if self.min_events < 3 or self.channel < 0:
            raise CalibrationInputError('At least three events and a nonnegative channel are required')
        if not 0 < self.min_event_seconds < self.max_event_seconds < 300:
            raise CalibrationInputError('Invalid event duration bounds')
        if self.timing_tolerance_seconds <= 0 or self.merge_gap_seconds < 0:
            raise CalibrationInputError('Invalid event timing settings')
        if self.session_gap_seconds < self.max_period_seconds or self.read_frames < 4096:
            raise CalibrationInputError('Invalid session gap or read buffer')
        if not 0 < self.spectral_bin_hz <= 100 or self.max_spectral_peaks < 1 or self.profile_frames < 32:
            raise CalibrationInputError('Invalid spectral search settings')
        if not 0 <= self.min_frequency_hz < self.max_frequency_hz or not 0 < self.min_tonal_fraction < 1:
            raise CalibrationInputError('Invalid frequency or tonal concentration settings')
        if self.minimum_level_separation_db < 3:
            raise CalibrationInputError('Absolute sound-level separation must be at least 3 dB')
        for name in ('min_events', 'max_spectral_peaks', 'profile_frames', 'max_events_per_detector', 'channel', 'read_frames'):
            if not isinstance(getattr(self, name), int):
                raise CalibrationInputError(f'{name} must be an integer')


@dataclass
class Recording:
    path: Path
    start: float
    frames: int
    sample_rate: int
    channels: int
    sample_width: int
    sha256: str
    session: int = 0

    @property
    def end(self):
        return self.start + self.frames / self.sample_rate

    def metadata(self):
        return {'path': str(self.path), 'sha256': self.sha256,
                'startEpochMs': round(self.start * 1000), 'startUtc': _iso(self.start),
                'endUtc': _iso(self.end), 'frames': self.frames, 'sampleRate': self.sample_rate,
                'channels': self.channels, 'sampleWidth': self.sample_width,
                'durationSeconds': self.frames / self.sample_rate, 'sessionId': self.session,
                'timestampSource': 'epoch_milliseconds_filename'}


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def inspect_recordings(paths, *, recursive=False, config=None):
    """Validate metadata and content hashes before using any file as evidence."""
    config = config or CalibrationConfig()
    config.validate()
    expanded = []
    duplicates = []
    seen_paths = set()
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            files = sorted(path.rglob('*') if recursive else path.iterdir())
            files = [p for p in files if p.is_file() and p.suffix.lower() == '.wav']
        elif path.is_file() and path.suffix.lower() == '.wav':
            files = [path]
        else:
            raise CalibrationInputError(f'Not a WAV file or directory: {path}')
        for file in files:
            file = file.resolve()
            if file in seen_paths:
                duplicates.append({'path': str(file), 'reason': 'same_resolved_path'})
            else:
                seen_paths.add(file)
                expanded.append(file)
    if not expanded:
        raise CalibrationInputError('No WAV files were found')
    recordings, by_name, by_hash = [], {}, {}
    for file in sorted(expanded):
        if not re.fullmatch(r'\d{13}', file.stem):
            raise CalibrationInputError(f'WAV filename must be its first-sample epoch milliseconds (13 digits): {file.name}')
        try:
            with wave.open(str(file), 'rb') as source:
                rate, frames = source.getframerate(), source.getnframes()
                channels, width = source.getnchannels(), source.getsampwidth()
                if source.getcomptype() != 'NONE' or width not in (1, 2, 3, 4):
                    raise CalibrationInputError(f'Only uncompressed PCM WAV is supported: {file}')
                if rate < 4000 or rate > 192000 or channels <= config.channel or frames < 1:
                    raise CalibrationInputError(f'Unsupported sample rate, channel, or empty recording: {file}')
                # Read the declared data, not just the header: truncated files are never evidence.
                remaining = frames * channels * width
                while remaining:
                    chunk = source.readframes(min(config.read_frames, math.ceil(remaining / (channels * width))))
                    if not chunk:
                        raise CalibrationInputError(f'Truncated PCM data: {file}')
                    remaining -= len(chunk)
        except (wave.Error, EOFError, OSError) as exc:
            raise CalibrationInputError(f'Cannot read PCM WAV {file}: {exc}') from exc
        digest = hashlib.sha256()
        with file.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        fingerprint = digest.hexdigest()
        name_key = file.name.lower()
        if name_key in by_name and by_name[name_key].sha256 != fingerprint:
            raise CalibrationInputError(f'Conflicting contents for timestamp filename {file.name}')
        if fingerprint in by_hash:
            by_name[name_key] = by_hash[fingerprint]
            duplicates.append({'path': str(file), 'duplicateOf': str(by_hash[fingerprint].path),
                               'sha256': fingerprint, 'reason': 'identical_file_content'})
            continue
        item = Recording(file, int(file.stem) / 1000, frames, rate, channels, width, fingerprint)
        by_hash[fingerprint] = by_name[name_key] = item
        recordings.append(item)
    recordings.sort(key=lambda item: item.start)
    session = 0
    for previous, current in zip(recordings, recordings[1:]):
        gap = current.start - previous.end
        # Epoch-ms filenames round sub-millisecond sample boundaries.
        if gap < -0.002:
            raise CalibrationInputError(f'Overlapping recordings: {previous.path.name} and {current.path.name}')
        if gap > config.session_gap_seconds:
            session += 1
        current.session = session
    return recordings, duplicates


def decode_pcm(raw, width, channels, channel=0):
    """Normalize every integer PCM width to float32 full scale before analysis."""
    if width == 1:
        samples = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        samples = np.frombuffer(raw, '<i2').astype(np.float32) / 32768
    elif width == 3:
        octets = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        values = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        samples = ((values ^ 0x800000) - 0x800000).astype(np.float32) / 8388608
    elif width == 4:
        samples = np.frombuffer(raw, '<i4').astype(np.float32) / 2147483648
    else:
        raise CalibrationInputError('Unsupported PCM width')
    return samples.reshape(-1, channels)[:, channel]


def _spectral_batches(recordings, config, edges, progress=None, stage='analysis'):
    """Continuous overlapping windows within covered audio; reset at every real gap."""
    pending = np.empty(0, np.float32)
    pending_start = 0.0
    previous = None
    segment = -1
    for file_index, item in enumerate(recordings):
        if progress:
            progress(stage, file_index + 1, len(recordings), str(item.path))
        continuous = previous is not None and item.sample_rate == previous.sample_rate and abs(item.start - previous.end) <= 0.002
        if not continuous:
            pending = np.empty(0, np.float32)
            pending_start = item.start
            segment += 1
        nfft = 2 ** math.ceil(math.log2(item.sample_rate * 0.064))
        hop = nfft // 2
        window = np.hanning(nfft).astype(np.float32)
        frequencies = rfftfreq(nfft, 1 / item.sample_rate)
        bins = np.searchsorted(frequencies, edges)
        bins = np.minimum(bins, len(frequencies))
        # Parseval + Hann energy normalization: integrated one-sided band power
        # is mean square in normalized PCM units (a 0.1-peak sine is -23.01 dBFS).
        norm = nfft * float(np.sum(window.astype(np.float64) ** 2))
        with wave.open(str(item.path), 'rb') as source:
            while raw := source.readframes(config.read_frames):
                samples = decode_pcm(raw, item.sample_width, item.channels, config.channel)
                pending = np.concatenate((pending, samples))
                if len(pending) < nfft:
                    continue
                frames = np.lib.stride_tricks.sliding_window_view(pending, nfft)[::hop]
                for offset in range(0, len(frames), 128):
                    block = frames[offset:offset + 128]
                    spectrum = np.abs(rfft(block * window, axis=1)) ** 2 * (2 / norm)
                    cumulative = np.pad(np.cumsum(spectrum, axis=1), ((0, 0), (1, 0)))
                    power = cumulative[:, bins[1:]] - cumulative[:, bins[:-1]]
                    times = pending_start + (np.arange(offset, offset + len(block)) * hop / item.sample_rate)
                    rms = np.mean(block * block, axis=1)
                    clipped = np.mean(np.abs(block) >= 0.999, axis=1)
                    yield segment, item.session, times, nfft / item.sample_rate, power, rms, clipped
                used = len(frames) * hop
                pending = pending[used:].copy()
                pending_start += used / item.sample_rate
        previous = item


def _profile(recordings, config, edges, progress):
    total_seconds = sum(item.frames / item.sample_rate for item in recordings)
    expected_frames = total_seconds / 0.04
    stride = max(1, math.ceil(expected_frames / config.profile_frames))
    samples = []
    maximum = np.zeros(len(edges) - 1, np.float32)
    count = 0
    clipped_count = 0
    for _, _, _, _, power, _, clipped in _spectral_batches(recordings, config, edges, progress, 'profile'):
        clean = clipped < 0.02
        if clean.any():
            maximum = np.maximum(maximum, power[clean].max(axis=0))
        indices = np.flatnonzero((np.arange(count, count + len(power)) % stride == 0) & clean)
        if len(indices):
            samples.append(power[indices])
        count += len(power)
        clipped_count += int((~clean).sum())
    if not samples:
        return None
    reservoir = np.concatenate(samples)
    # The floor prevents floating-point residuals beside a static tone becoming infinite SNR.
    baseline = np.maximum(np.median(reservoir, axis=0), 1e-13)
    ranking = 10 * np.log10(np.maximum(maximum, 1e-20) / baseline)
    valid = (edges[:-1] >= config.min_frequency_hz) & (maximum > 10 ** (config.min_rms_dbfs / 10))
    peaks = []
    for index in np.argsort(ranking)[::-1]:
        center = (edges[index] + edges[index + 1]) / 2
        if valid[index] and ranking[index] >= 8 and all(abs(center - old) >= 150 for old in peaks):
            peaks.append(float(center))
            if len(peaks) >= config.max_spectral_peaks:
                break
    return {'baseline': baseline, 'peaks': peaks, 'frames': count,
            'clippedFraction': clipped_count / max(count, 1)}


class _EventBuilder:
    def __init__(self, config, settings):
        self.config, self.settings = config, settings
        self.events = []
        self.pending = None
        self.segment = None
        self.excessive = False

    def finish(self):
        item = self.pending
        self.pending = None
        if item is None or item['end'] - item['time'] < self.config.min_event_seconds or item['tooLong']:
            return
        vector = item.pop('spectrum')
        length = float(np.linalg.norm(vector))
        if length <= 0:
            return
        item['fingerprint'] = vector / length
        item['duration'] = item['end'] - item['time']
        item['eventBandDbfs'] = 10 * math.log10(max(float(np.quantile(item.pop('bandPowers'), 0.9)), 1e-20))
        self.events.append(item)
        if len(self.events) > self.config.max_events_per_detector:
            self.excessive = True
            self.events.clear()

    def add(self, segment, session, time, window_seconds, excess, snr, band_power):
        if self.excessive:
            return
        if segment != self.segment or (self.pending is not None and time - self.pending['end'] > self.config.merge_gap_seconds):
            self.finish()
        self.segment = segment
        if self.pending is None:
            self.pending = {'time': float(time), 'end': float(time + window_seconds), 'session': session,
                            'spectrum': np.zeros_like(excess), 'peakSnrDb': float(snr), 'tooLong': False,
                            'bandPowers': []}
        item = self.pending
        item['end'] = float(time + window_seconds)
        item['tooLong'] = item['end'] - item['time'] > self.config.max_event_seconds
        if not item['tooLong']:
            item['spectrum'] += excess
            item['bandPowers'].append(float(band_power))
        item['peakSnrDb'] = max(item['peakSnrDb'], float(snr))


def _detect(recordings, config, edges, profile, progress):
    builders = []
    centers = (edges[:-1] + edges[1:]) / 2
    for center in profile['peaks']:
        for width in (150.0, 300.0):
            selected = (centers >= center - width / 2) & (centers <= center + width / 2)
            low, high = float(edges[np.flatnonzero(selected)[0]]), float(edges[np.flatnonzero(selected)[-1] + 1])
            tonal_fraction = max(config.min_tonal_fraction, 2.5 * (high - low) / max(edges[-1] - config.min_frequency_hz, high - low))
            for snr in (8.0, 14.0):
                builders.append(_EventBuilder(config, {'frequencyLowHz': low, 'frequencyHighHz': high,
                    'snrThresholdDb': snr, 'minimumTonalFraction': tonal_fraction,
                    'noiseBandPower': float(profile['baseline'][selected].sum()), 'mask': selected}))
    minimum_power = 10 ** (config.min_rms_dbfs / 10)
    audible = edges[:-1] >= config.min_frequency_hz
    for segment, session, times, window_seconds, power, rms, clipped in _spectral_batches(recordings, config, edges, progress, 'detect'):
        excess = np.maximum(power - profile['baseline'] * 1.5, 0)
        total_excess = np.maximum(excess[:, audible].sum(axis=1), 1e-20)
        good = (rms >= minimum_power) & (clipped < 0.02)
        for builder in builders:
            if builder.excessive:
                continue
            settings = builder.settings
            band = power[:, settings['mask']].sum(axis=1)
            snr = 10 * np.log10(np.maximum(band, 1e-20) / settings['noiseBandPower'])
            concentration = excess[:, settings['mask']].sum(axis=1) / total_excess
            active = good & (snr >= settings['snrThresholdDb']) & (concentration >= settings['minimumTonalFraction']) & (band >= minimum_power)
            for index in np.flatnonzero(active):
                builder.add(segment, session, times[index], window_seconds, excess[index], snr[index], band[index])
    for builder in builders:
        builder.finish()
    return builders


def _acoustic_groups(events):
    groups = []
    for item in events:
        choices = []
        for index, group in enumerate(groups):
            ratio = item['duration'] / group['duration']
            similarity = float(item['fingerprint'] @ group['prototype'])
            if group['session'] == item['session'] and 0.4 <= ratio <= 2.5 and similarity >= 0.82:
                choices.append((similarity, index))
        if choices:
            group = groups[max(choices)[1]]
            group['events'].append(item)
            vector = group['prototype'] * (len(group['events']) - 1) + item['fingerprint']
            group['prototype'] = vector / np.linalg.norm(vector)
            group['duration'] = float(np.median([event['duration'] for event in group['events']]))
        else:
            groups.append({'events': [item], 'prototype': item['fingerprint'].copy(),
                           'duration': item['duration'], 'session': item['session']})
    return groups


def _level_variants(group, config):
    """A loud singleton cannot establish a local-floor threshold.

    Keep the ungated hypothesis. Additional hypotheses require an observed gap
    in absolute event levels, several strong events, and at least two weak ones.
    Their cadence and full-session coverage still need independent verification.
    """
    yield group
    events = group['events']
    if len(events) < config.min_events + 2 or any('eventBandDbfs' not in event for event in events):
        return
    ordered = sorted(events, key=lambda event: event['eventBandDbfs'])
    for boundary in range(2, len(ordered) - config.min_events + 1):
        low = ordered[boundary - 1]['eventBandDbfs']
        high = ordered[boundary]['eventBandDbfs']
        if high - low < config.minimum_level_separation_db:
            continue
        threshold = (low + high) / 2
        strong = [event for event in events if event['eventBandDbfs'] >= threshold]
        weak = [event for event in events if event['eventBandDbfs'] < threshold]
        prototype = np.mean([event['fingerprint'] for event in strong], axis=0)
        prototype /= np.linalg.norm(prototype)
        yield {**group, 'events': strong, 'prototype': prototype,
               'duration': float(np.median([event['duration'] for event in strong])),
               'levelGate': {'thresholdDbfs': threshold, 'gapDb': high - low,
                             'weakEvents': weak, 'strongCandidates': strong}}


def _intensity_evidence(group, matched, period_ready, config):
    levels = [item['eventBandDbfs'] for item in matched if 'eventBandDbfs' in item]
    evidence = {'clear': False, 'thresholdDbfs': None, 'gapDb': None,
                'weakExcludedCount': 0, 'strongMatchedCount': len(matched),
                'eventBandLevelMetric': EVENT_BAND_LEVEL_METRIC,
                'requiresStableGain': True, 'reason': 'no_supported_absolute_level_gap',
                'matchedLevelRangeDbfs': [min(levels), max(levels)] if levels else None}
    gate = group.get('levelGate')
    if not gate or not levels:
        return evidence
    matched_ids = {id(event) for event in matched}
    # An isolated far-louder transient cannot veto a quieter, repeatable train.
    # Off-phase events overlapping the train's level range cannot be separated by
    # this proposed amplitude gate, so they require review instead of a floor claim.
    overlap = [event for event in gate['strongCandidates'] if id(event) not in matched_ids
               and min(levels) - config.minimum_level_separation_db < event['eventBandDbfs']
               <= max(levels) + config.minimum_level_separation_db]
    weak_levels = [event['eventBandDbfs'] for event in gate['weakEvents']]
    evidence.update({'clear': period_ready and not overlap, 'thresholdDbfs': gate['thresholdDbfs'],
                     'gapDb': min(levels) - max(weak_levels),
                     'weakExcludedCount': len(weak_levels),
                     'weakLevelRangeDbfs': [min(weak_levels), max(weak_levels)],
                     'overlappingOffPhaseEventCount': len(overlap),
                     'strongCandidateCount': len(gate['strongCandidates']),
                     'reason': 'overlapping_sound_levels' if overlap else
                               ('clear_level_gap_and_repeated_strong_train' if period_ready else 'strong_train_needs_timing_review')})
    return evidence


def _period_votes(times, config):
    votes = Counter()
    for index, value in enumerate(times):
        for later in times[index + 1:index + 13]:
            delta = later - value
            if delta > config.max_period_seconds * 5:
                break
            for divisor in range(1, min(8, int(delta / config.min_period_seconds)) + 1):
                period = delta / divisor
                if config.min_period_seconds < period < config.max_period_seconds:
                    votes[round(period * 2) / 2] += 1 / math.sqrt(divisor)
    return [period for period, _ in votes.most_common(48)]


def _fit_period(times, period, tolerance):
    relative = times - times[0]
    seeds = np.unique(np.r_[np.arange(min(12, len(times))), np.linspace(0, len(times) - 1, min(12, len(times))).astype(int)])
    best = None
    for seed in seeds:
        origin = relative[seed]
        refined = period
        selected = np.array([], dtype=int)
        for _ in range(3):
            cycles = np.rint((relative - origin) / refined).astype(np.int64)
            error = np.abs(relative - origin - cycles * refined)
            eligible = np.flatnonzero(error <= tolerance)
            chosen = {}
            for index in eligible:
                key = cycles[index]
                if key not in chosen or error[index] < error[chosen[key]]:
                    chosen[key] = index
            selected = np.array(sorted(chosen.values()), dtype=int)
            if len(selected) < 3 or np.ptp(cycles[selected]) < 2:
                break
            x, y = cycles[selected].astype(float), relative[selected]
            refined = float(np.dot(x - x.mean(), y - y.mean()) / np.dot(x - x.mean(), x - x.mean()))
            origin = float(y.mean() - refined * x.mean())
        if len(selected) < 3:
            continue
        cycles = np.rint((relative[selected] - origin) / refined).astype(int)
        errors = relative[selected] - origin - cycles * refined
        keep = np.abs(errors) <= tolerance
        selected, cycles, errors = selected[keep], cycles[keep], errors[keep]
        if len(selected) < 3:
            continue
        rmse = float(np.sqrt(np.mean(errors ** 2)))
        rank = (len(selected), -rmse)
        if best is None or rank > best['rank']:
            best = {'indices': selected, 'cycles': cycles, 'period': refined,
                    'origin': float(origin + times[0]), 'rmse': rmse, 'rank': rank}
    return best


def _covered(time, intervals, margin):
    return any(start <= time - margin and end >= time + margin for start, end in intervals)


def _coverage_intervals(recordings, session):
    merged = []
    for item in recordings:
        if item.session != session:
            continue
        if merged and item.start - merged[-1][1] <= 0.002:
            merged[-1] = (merged[-1][0], item.end)
        else:
            merged.append((item.start, item.end))
    return merged


def _evaluate_group(group, builder, recordings, config):
    events = group['events']
    if len(events) < config.min_events:
        return []
    times = np.array([event['time'] for event in events])
    results = []
    intervals = _coverage_intervals(recordings, group['session'])
    for period in _period_votes(times, config):
        fit = _fit_period(times, period, config.timing_tolerance_seconds)
        if fit is None:
            continue
        period = fit['period']
        if not config.min_period_seconds < period < config.max_period_seconds:
            continue
        matched = [events[index] for index in fit['indices']]
        if len(matched) < config.min_events:
            continue
        cycle_indices = fit['cycles']
        consecutive = int(np.sum(np.diff(cycle_indices) == 1))
        if not consecutive:
            continue  # An unsupported divisor of every observed interval is not a learned cycle.
        covered_missing = 0
        unrecorded = 0
        present = set(int(x) for x in cycle_indices)
        # Constant-cycle support must cover the recording session, not just a convenient
        # burst between its first and last detections. Never count gaps as silence.
        margin = config.timing_tolerance_seconds + group['duration']
        first_cycle = math.ceil((intervals[0][0] + margin - fit['origin']) / period)
        last_cycle = math.floor((intervals[-1][1] - margin - fit['origin']) / period)
        for cycle in range(first_cycle, last_cycle + 1):
            if cycle not in present:
                expected = fit['origin'] + cycle * period
                if _covered(expected, intervals, margin):
                    covered_missing += 1
                else:
                    unrecorded += 1
        fraction = len(matched) / len(events)
        coverage = len(matched) / (len(matched) + covered_missing)
        similarity = float(np.median([item['fingerprint'] @ group['prototype'] for item in matched]))
        regularity = math.exp(-fit['rmse'] / config.timing_tolerance_seconds)
        # A long stream must contribute many repeats, not three accidental alignments.
        score = (0.35 * fraction + 0.25 * coverage + 0.25 * regularity + 0.15 * similarity) * min(1.0, len(matched) / 5)
        ready = fraction >= 0.75 and coverage >= 0.65 and similarity >= 0.82
        boundary = min(period - 300, 1800 - period) <= max(0.15, fit['rmse'] * 2)
        reason = 'period_near_strict_boundary' if boundary else ('periodic_subset_of_competing_sounds' if not ready else 'repeated_acoustically_similar_sound')
        intensity = _intensity_evidence(group, matched, ready and not boundary, config)
        if intensity.get('overlappingOffPhaseEventCount'):
            ready = False
            reason = 'overlapping_sound_levels'
        elif intensity['clear']:
            reason = 'repeated_strong_sound_separated_from_weaker_matches'
        result = {'periodSeconds': float(period), 'sessionId': group['session'], 'score': float(score),
                  'ready': ready and not boundary, 'reason': reason, 'matchingEvents': matched,
                  'intensitySeparation': intensity,
                  'excludedWeakEvents': group.get('levelGate', {}).get('weakEvents', []),
                  'support': {'matchingEvents': len(matched), 'candidateEventsInAcousticGroup': len(events),
                              'cyclesSpanned': int(np.ptp(cycle_indices)), 'consecutiveIntervals': consecutive,
                              'missedEventsInCoveredAudio': covered_missing, 'cyclesInsideRecordingGaps': unrecorded,
                              'expectedEventsInCoveredAudio': len(matched) + covered_missing,
                              'inlierFraction': fraction, 'coverageFraction': coverage,
                              'acousticSimilarity': similarity, 'timingRmseSeconds': fit['rmse']},
                  'settings': builder.settings, 'prototype': group['prototype']}
        if not any(abs(old['periodSeconds'] - period) <= 1.0 for old in results):
            results.append(result)
    return results


def _same_pattern(left, right, tolerance):
    if left['sessionId'] != right['sessionId'] or abs(left['periodSeconds'] - right['periodSeconds']) > max(2.0, left['periodSeconds'] * 0.01):
        return False
    a = np.array([x['time'] for x in left['matchingEvents']])
    b = np.array([x['time'] for x in right['matchingEvents']])
    overlap = sum(np.min(abs(b - x)) <= tolerance for x in a)
    return overlap / max(len(a), len(b)) >= 0.75


def _is_demonstrably_weaker(candidate, strong, tolerance):
    """Compare the same observed events in the winner's band, not different bands' dB."""
    if candidate['intensitySeparation']['clear']:
        return False  # Conflicting strong-tier evidence must remain available for review.
    if not strong['intensitySeparation']['clear'] or candidate['sessionId'] != strong['sessionId']:
        return False
    if candidate['prototype'].shape != strong['prototype'].shape or float(candidate['prototype'] @ strong['prototype']) < 0.82:
        return False  # A different sound coinciding with a weak chime remains a competitor.
    weak_times = np.array([event['time'] for event in strong['excludedWeakEvents']])
    return bool(len(weak_times) and all(np.min(abs(weak_times - event['time'])) <= tolerance
                                      for event in candidate['matchingEvents']))


def _source_event(event, recordings):
    item = next((item for item in recordings if item.start - 0.002 <= event['time'] < item.end), None)
    return {'observedAt': _iso(event['time']), 'epochMs': round(event['time'] * 1000),
            'durationSeconds': round(event['duration'], 4), 'peakSnrDb': round(event['peakSnrDb'], 3),
            'eventBandDbfs': round(event['eventBandDbfs'], 4),
            'sourceFile': str(item.path) if item else None,
            'sourceSampleOffset': round((event['time'] - item.start) * item.sample_rate) if item else None,
            'timestampMeaning': 'start_of_first_matching_spectral_window'}


def _summary(candidate):
    return {'periodSeconds': candidate['periodSeconds'], 'sessionId': candidate['sessionId'],
            'score': candidate['score'], 'support': candidate['support'], 'reason': candidate['reason'],
            'intensitySeparation': candidate['intensitySeparation'],
            'frequencyLowHz': candidate['settings']['frequencyLowHz'],
            'frequencyHighHz': candidate['settings']['frequencyHighHz']}


def calibrate(paths, *, config=None, recursive=False, progress: Callable | None = None):
    """Return a JSON-safe ready/review/insufficient_evidence report; never send arrivals."""
    config = config or CalibrationConfig()
    recordings, duplicates = inspect_recordings(paths, recursive=recursive, config=config)
    max_frequency = min(config.max_frequency_hz, min(item.sample_rate / 2 for item in recordings))
    edges = np.arange(0, max_frequency + 0.001, config.spectral_bin_hz)
    if len(edges) < 8:
        raise CalibrationInputError('The requested frequency resolution cannot fit this sample rate')
    report = {'schemaVersion': 2, 'eventKind': 'departure', 'floor': 7,
              'createdAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'status': 'insufficient_evidence', 'reason': 'no_repeated_sound_supported',
              'periodSeconds': None, 'parameters': None, 'matchingEvents': [], 'alternatives': [],
              'source': {'files': [item.metadata() for item in recordings], 'duplicatesExcluded': duplicates,
                         'recordedSeconds': sum(item.frames / item.sample_rate for item in recordings),
                         'sessions': len({item.session for item in recordings}),
                         'gaps': [{'after': str(a.path), 'before': str(b.path), 'seconds': b.start - a.end}
                                  for a, b in zip(recordings, recordings[1:]) if b.start - a.end > 0.002]},
              'searchSettings': asdict(config),
              'limitations': ['The floor and departure meaning are supplied assumptions, not acoustically verified labels.',
                              'Absolute level discrimination assumes fixed microphone position and unchanged gain, without automatic gain control; recalibrate after changes.',
                              'A level gap separates stronger and weaker recorded sounds; it does not independently identify their floors.',
                              'This search targets transient tonal motifs; purely broadband clicks or speech may require another detector.',
                              'Scores measure agreement in these recordings, not accuracy on future audio.',
                              'An arrival at floor 7 is a different event; this report must not be posted to the arrival API.',
                              'Filename timestamps approximate capture start; a recorder manifest can document clock uncertainty.']}
    if max((items[-1].end - items[0].start for session in {x.session for x in recordings}
            if (items := [x for x in recordings if x.session == session])), default=0) <= 600:
        report['reason'] = 'record_longer_at_least_three_departures_in_one_session'
        return report
    profile = _profile(recordings, config, edges, progress)
    if profile is None or not profile['peaks']:
        report['reason'] = 'no_transient_tonal_signal_above_noise'
        return report
    report['audioQuality'] = {'spectralFrames': profile['frames'], 'clippedFrameFraction': profile['clippedFraction'],
                              'spectralPeakFrequenciesHz': profile['peaks']}
    builders = _detect(recordings, config, edges, profile, progress)
    candidates = []
    for builder in builders:
        if not builder.excessive:
            for group in _acoustic_groups(builder.events):
                for variant in _level_variants(group, config):
                    candidates.extend(_evaluate_group(variant, builder, recordings, config))
    report['searchDiagnostics'] = {'detectorsTested': len(builders),
                                 'noisyDetectorsExcluded': sum(item.excessive for item in builders),
                                 'largestCandidateEventCount': max((len(x.events) for x in builders), default=0)}
    if not candidates:
        return report
    # Suppression needs a repeated strong train and proof that every competing
    # event lies in its clearly weaker acoustic tier. Similar SNR is insufficient.
    strong_candidates = [item for item in candidates if item['intensitySeparation']['clear']]
    weak_candidates = [item for item in candidates if any(_is_demonstrably_weaker(item, strong, config.timing_tolerance_seconds)
                               for strong in strong_candidates)]
    weak_ids = {id(item) for item in weak_candidates}
    survivors = [item for item in candidates if id(item) not in weak_ids]
    contradictory_level_gates = not survivors
    if survivors:
        candidates = survivors
    else:
        # Different frequency bands can give contradictory level orderings. Keep
        # all evidence and require review rather than selecting through a cycle.
        weak_candidates = []
    candidates.sort(key=lambda item: (item['score'], item['support']['matchingEvents']), reverse=True)
    distinct = []
    for candidate in candidates:
        equivalent = next((index for index, previous in enumerate(distinct)
                           if _same_pattern(candidate, previous, config.timing_tolerance_seconds)), None)
        if equivalent is None:
            distinct.append(candidate)
        elif candidate['intensitySeparation']['clear'] and not distinct[equivalent]['intensitySeparation']['clear']:
            distinct[equivalent] = candidate
    distinct.sort(key=lambda item: (item['score'], item['support']['matchingEvents']), reverse=True)
    winner = distinct[0]
    report['periodSeconds'] = winner['periodSeconds']
    report['support'] = winner['support']
    report['score'] = winner['score']
    report['matchingEvents'] = [_source_event(event, recordings) for event in winner['matchingEvents']]
    report['intensitySeparation'] = {**winner['intensitySeparation'],
        'excludedWeakEvents': [{'observedAt': _iso(item['time']), 'eventBandDbfs': round(item['eventBandDbfs'], 4)}
                              for item in winner['excludedWeakEvents']]}
    suppressed = []
    for candidate in weak_candidates:
        if _is_demonstrably_weaker(candidate, winner, config.timing_tolerance_seconds) and not any(
                _same_pattern(candidate, previous, config.timing_tolerance_seconds) for previous in suppressed):
            suppressed.append(candidate)
    report['intensitySeparation']['weakerPeriodicAlternativesExcluded'] = len(suppressed)
    report['alternatives'] = ([_summary(item) for item in distinct[1:9]] +
                              [{**_summary(item), 'excludedByStrongLevelGate': True} for item in suppressed[:4]])
    competing = [item for item in distinct[1:] if
                 ((item['score'] >= winner['score'] * 0.85
                   and (item['ready'] or item['support']['matchingEvents'] >= winner['support']['matchingEvents']))
                  or (item['intensitySeparation']['clear'] and winner['intensitySeparation']['clear']))
                 and (item['sessionId'] == winner['sessionId'] or abs(item['periodSeconds'] - winner['periodSeconds']) > 3)]
    report['status'] = 'ready' if winner['ready'] and not competing else 'review'
    report['reason'] = 'competing_periodic_sound_patterns' if competing else winner['reason']
    if contradictory_level_gates:
        report['status'] = 'review'
        report['reason'] = 'conflicting_sound_level_gates'
        report['intensitySeparation'].update(clear=False, reason='conflicting_sound_level_gates')
    durations = np.array([item['duration'] for item in winner['matchingEvents']])
    settings = {key: value for key, value in winner['settings'].items() if key != 'mask'}
    settings.update({'algorithm': 'hann_spectral_band_snr_level_v2', 'channel': config.channel,
                     'amplitudeUnits': 'normalized_pcm_full_scale_squared',
                     'powerNormalization': 'nfft * sum(hann_window**2)',
                     'eventBandLevelMetric': EVENT_BAND_LEVEL_METRIC,
                     'minimumEventBandDbfs': winner['intensitySeparation']['thresholdDbfs'] if report['intensitySeparation']['clear'] else None,
                     'minimumLevelSeparationDb': config.minimum_level_separation_db,
                     'spectralBinHz': config.spectral_bin_hz, 'window': 'hann',
                     'fftSizeRule': 'next_power_of_two(sample_rate * 0.064)', 'hopFraction': 0.5,
                     'minimumRmsDbfs': config.min_rms_dbfs,
                     'mergeGapSeconds': config.merge_gap_seconds,
                     'minimumEventSeconds': max(config.min_event_seconds, float(np.quantile(durations, .1)) * .6),
                     'maximumEventSeconds': min(config.max_event_seconds, float(np.quantile(durations, .9)) * 1.6),
                     'typicalEventSeconds': float(np.median(durations)),
                     'rearmSeconds': max(config.merge_gap_seconds, float(np.max(durations)) * 1.5),
                     'acousticSimilarityMinimum': 0.82,
                     'spectralFingerprint': winner['prototype'].astype(float).tolist(),
                     'fingerprintBinEdgesHz': edges.astype(float).tolist(),
                     'referenceNoisePowerByBin': profile['baseline'].astype(float).tolist(),
                     'timingToleranceSeconds': config.timing_tolerance_seconds})
    report['parameters'] = settings
    return report
