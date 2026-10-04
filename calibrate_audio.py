"""Find a repeated floor-seven departure sound in timestamped PCM WAV recordings."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def write_json_atomic(path, value):
    """Replace a generated JSON artifact only after its complete write succeeds."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Example: python calibrate_audio.py data/recordings --output data/calibration.json\n'
        'Record at least three departures (preferably five or more). WAV filenames must be '
        'first-sample epoch milliseconds, e.g. 1699622345612.wav. The cycle must be strictly '
        'longer than 300 and shorter than 1800 seconds. No microphone is opened.'))
    parser.add_argument('paths', nargs='+', help='PCM WAV files or directories containing timestamped WAVs')
    parser.add_argument('--output', type=Path, default=Path('data/calibration.json'), help='JSON report destination (default: data/calibration.json)')
    parser.add_argument('--runtime-output', type=Path,
                        help='Compact runtime JSON destination (default: <report-stem>.runtime.json next to the report)')
    parser.add_argument('--recursive', action='store_true', help='Search inside subdirectories')
    parser.add_argument('--channel', type=int, default=0, help='Zero-based audio channel to analyze (default: 0)')
    parser.add_argument('--min-events', type=int, default=3, help='Minimum matching departures, at least 3 (default: 3)')
    parser.add_argument('--timing-tolerance', type=float, default=3.0, metavar='SECONDS', help='Allowed timing residual around a constant cycle')
    parser.add_argument('--merge-gap', type=float, default=1.5, metavar='SECONDS', help='Merge nearby tone pulses into one departure motif')
    parser.add_argument('--quiet', action='store_true', help='Hide per-file progress')
    args = parser.parse_args(argv)
    if args.output.suffix.lower() != '.json':
        parser.error('--output must end in .json, so source WAV recordings cannot be overwritten')
    runtime_output = args.runtime_output or args.output.with_name(args.output.stem + '.runtime.json')
    if runtime_output.suffix.lower() != '.json':
        parser.error('--runtime-output must end in .json')
    if args.output.resolve() == runtime_output.resolve():
        parser.error('--output and --runtime-output must be different files')
    try:
        from elevator.audio_calibration import CalibrationConfig, CalibrationInputError, calibrate
    except ImportError as exc:
        parser.exit(2, f'Audio dependencies are missing: {exc}. Run: uv sync --group audio\n')
    config = CalibrationConfig(channel=args.channel, min_events=args.min_events,
                               timing_tolerance_seconds=args.timing_tolerance, merge_gap_seconds=args.merge_gap)
    progress = None if args.quiet else lambda stage, index, count, path: print(f'{stage} [{index}/{count}] {Path(path).name}', file=sys.stderr, flush=True)
    try:
        report = calibrate(args.paths, config=config, recursive=args.recursive, progress=progress)
    except (CalibrationInputError, OSError) as exc:
        print(f'Calibration input error: {exc}', file=sys.stderr)
        print(f'Runtime configuration not updated: {runtime_output.resolve()}', file=sys.stderr)
        return 2
    try:
        write_json_atomic(args.output, report)
    except (OSError, ValueError) as exc:
        print(f'Cannot write calibration report: {exc}', file=sys.stderr)
        print(f'Runtime configuration not updated: {runtime_output.resolve()}', file=sys.stderr)
        return 2
    print(f'{report["status"]}: {report["reason"]}')
    if report['periodSeconds'] is not None:
        print(f'Candidate cycle: {report["periodSeconds"]:.3f} seconds; matching departures: {len(report["matchingEvents"])}')
    separation = report.get('intensitySeparation', {})
    if separation.get('clear'):
        print(f'Sound-band level gate: {separation["thresholdDbfs"]:.2f} dBFS; '
              f'strong/weak separation: {separation["gapDb"]:.2f} dB; '
              f'weaker candidates excluded: {separation["weakExcludedCount"]}')
    print(f'Report: {args.output.resolve()}')
    if report['status'] != 'ready':
        retained = '; existing file retained' if runtime_output.exists() else '; no file created'
        print(f'Runtime configuration not updated ({report["status"]}){retained}: {runtime_output.resolve()}')
        return 1
    try:
        from elevator.runtime_config import build_runtime_config
        write_json_atomic(runtime_output, build_runtime_config(report))
    except (OSError, ValueError) as exc:
        print(f'Cannot write runtime configuration: {exc}', file=sys.stderr)
        print(f'Runtime configuration not updated: {runtime_output.resolve()}', file=sys.stderr)
        return 2
    print(f'Runtime configuration: {runtime_output.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
