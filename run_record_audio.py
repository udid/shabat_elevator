"""CLI entry point; microphone access happens only when this script is run."""

from elevator.audio_recorder import main


if __name__ == "__main__":
    raise SystemExit(main())
