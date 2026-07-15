"""Background reconciliation for scheduled reservations."""

from __future__ import annotations

import logging
import threading

from .service import ReservationService

LOGGER = logging.getLogger(__name__)


class ReservationReconciler:
    def __init__(
        self,
        service: ReservationService,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="reservation-reconciler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.service.reconcile()
            except Exception:
                LOGGER.exception("Unable to reconcile scheduled reservations")
