"""Supervise microphone capture without interrupting the read-only HTTP API."""

from __future__ import annotations

import logging
from threading import Event, Thread
from uuid import uuid4

from .state import iso

LOG = logging.getLogger(__name__)


class DetectorSupervisor:
    def __init__(self, store, config, *, device=None, sample_rate=44100,
                 listener=None, retry_seconds=5, monitor_only=False):
        self.store = store
        self.config = config
        self.device = device
        self.sample_rate = sample_rate
        self.listener = listener
        self.retry_seconds = retry_seconds
        self.monitor_only = monitor_only
        self.stop_event = Event()
        self.thread = Thread(target=self._run, name="elevator-microphone", daemon=True)

    def _heartbeat(self, observed):
        self.store.heartbeat({"deviceId": self.store.device_id, "observedAt": iso(observed)})

    def _observe(self, observed):
        if self.monitor_only:
            LOG.info("Monitor-only chime candidate at %s; no elevator observation published", iso(observed))
            return
        result = self.store.observe({
            "deviceId": self.store.device_id,
            "eventId": str(uuid4()),
            "eventKind": "departure",
            "floor": self.config["floor"],
            "observedAt": iso(observed),
        })
        LOG.info("Departure candidate at %s: %s", iso(observed), result)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                listener = self.listener
                if listener is None:
                    from .live_detector import listen
                    listener = listen
                listener(self.config, self._observe, self._heartbeat, self.stop_event,
                         device=self.device, sample_rate=self.sample_rate)
                if not self.stop_event.is_set():
                    raise RuntimeError("Microphone capture ended unexpectedly")
            except Exception:
                self.store.sensor_unavailable("המיקרופון אינו זמין; מנסה להתחבר מחדש")
                if not self.stop_event.is_set():
                    LOG.exception("Microphone capture failed; retrying in %s seconds", self.retry_seconds)
            if self.stop_event.wait(self.retry_seconds):
                break

    def start(self):
        if self.monitor_only:
            self.store.sensor_unavailable("מצב בדיקה; טרם הופעל זיהוי בקומה 7")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        self.store.sensor_unavailable()
