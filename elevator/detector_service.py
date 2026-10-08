"""Supervise microphone capture without interrupting the read-only HTTP API."""

from __future__ import annotations

import logging
import hashlib
import json
import platform
import time
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from .state import iso, parse_time

LOG = logging.getLogger(__name__)


class DetectorSupervisor:
    def __init__(self, store, config, *, device=None, sample_rate=44100,
                 listener=None, retry_seconds=5, monitor_only=False, diagnostics=None):
        self.store = store
        self.config = config
        self.device = device
        self.sample_rate = sample_rate
        self.listener = listener
        self.retry_seconds = retry_seconds
        self.monitor_only = monitor_only
        self.diagnostics = diagnostics
        self._last_health = None
        self.stop_event = Event()
        self.thread = Thread(target=self._run, name="elevator-microphone", daemon=True)

    def _diagnostic(self, kind, **fields):
        if self.diagnostics is not None:
            try:
                self.diagnostics.event(kind, **fields)
            except Exception:
                LOG.warning("Could not enqueue diagnostic event", exc_info=True)

    def _heartbeat(self, observed):
        self.store.heartbeat({"deviceId": self.store.device_id, "observedAt": iso(observed)})
        now = time.monotonic()
        if self.diagnostics is not None and (self._last_health is None or now - self._last_health >= 60):
            self._last_health = now
            try:
                self._diagnostic("detector_health", observedAt=iso(observed),
                                 state=self.store.snapshot(), storage=self.diagnostics.status())
            except Exception:
                LOG.warning("Could not collect diagnostic health summary", exc_info=True)

    def _observe(self, observed):
        if self.monitor_only:
            LOG.info("Monitor-only chime candidate at %s; no elevator observation published", iso(observed))
            self._diagnostic("observation_decision", observedAt=iso(observed),
                             accepted=False, reason="monitor_only")
            return
        previous = self.store.snapshot().get("lastDepartureAt") if self.diagnostics is not None else None
        event_id = str(uuid4())
        result = self.store.observe({
            "deviceId": self.store.device_id,
            "eventId": event_id,
            "eventKind": "departure",
            "floor": self.config["floor"],
            "observedAt": iso(observed),
        })
        LOG.info("Departure candidate at %s: %s", iso(observed), result)
        if self.diagnostics is not None:
            self._diagnostic("observation_decision", eventId=event_id, observedAt=iso(observed),
                             intervalSeconds=(observed - parse_time(previous)).total_seconds() if previous else None,
                             result=result, state=self.store.snapshot())

    def _run(self):
        while not self.stop_event.is_set():
            try:
                listener = self.listener
                if listener is None:
                    from .live_detector import listen
                    listener = listen
                options = {"device": self.device, "sample_rate": self.sample_rate}
                if self.diagnostics is not None:
                    options["diagnostics"] = self.diagnostics
                listener(self.config, self._observe, self._heartbeat, self.stop_event, **options)
                if not self.stop_event.is_set():
                    raise RuntimeError("Microphone capture ended unexpectedly")
            except Exception as error:
                self.store.sensor_unavailable("המיקרופון אינו זמין; מנסה להתחבר מחדש")
                self._last_health = None
                self._diagnostic("detector_failure", errorType=type(error).__name__,
                                 error=str(error), retrySeconds=self.retry_seconds)
                if not self.stop_event.is_set():
                    LOG.exception("Microphone capture failed; retrying in %s seconds", self.retry_seconds)
            if self.stop_event.wait(self.retry_seconds):
                break

    def start(self):
        if self.diagnostics is not None:
            # Keep calibration and code identity with each run, without reading
            # credentials or invoking Git on the Raspberry Pi.
            try:
                config_hash = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
                code_hash = hashlib.sha256()
                for name in ("live_detector.py", "detector_service.py", "state.py", "diagnostics.py"):
                    code_hash.update(name.encode())
                    code_hash.update(Path(__file__).with_name(name).read_bytes())
                self._diagnostic("detector_start", deviceId=self.store.device_id, device=self.device,
                                 sampleRate=self.sample_rate, monitorOnly=self.monitor_only,
                                 config=self.config, configHash=config_hash, codeHash=code_hash.hexdigest(),
                                 pythonVersion=platform.python_version())
            except Exception:
                LOG.warning("Could not record detector configuration identity", exc_info=True)
        if self.monitor_only:
            self.store.sensor_unavailable("מצב בדיקה; טרם הופעל זיהוי בקומה 7")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        self.store.sensor_unavailable()
        self._diagnostic("detector_stop", threadStopped=not self.thread.is_alive())
