"""Cached machine telemetry for the console's gauges.

The web console shows live CPU, memory, temperature and disk gauges, exactly like
the HUD it is modelled on.  Reading ``/proc`` or shelling out to ``vcgencmd``
costs a fraction of a second, and doing that inside a request handler would stall
the event loop - so the numbers are sampled on a daemon thread and cached here.

Nothing is invented: a metric this machine cannot provide stays ``None`` and the
console draws a dash instead of a number.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from core.logging_setup import get_logger

log = get_logger("telemetry")

#: slow enough to stay free on a Pi, fast enough that the gauges feel live
DEFAULT_INTERVAL = 3.0


def _default_sampler() -> Dict[str, Any]:
    from tools.system import machine_metrics

    return machine_metrics()


class Telemetry:
    """A periodically refreshed snapshot of this machine."""

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL,
        sampler: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self.interval = max(0.5, float(interval))
        self._sampler = sampler or _default_sampler
        self._lock = threading.Lock()
        self._latest: Dict[str, Any] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- reading ----------------------------------------------------------- #
    def sample(self) -> Dict[str, Any]:
        error = ""
        try:
            data = self._sampler() or {}
            if not isinstance(data, dict):  # pragma: no cover - defensive
                data = {}
        except Exception as exc:  # noqa: BLE001 - a gauge must never break JARVIS
            log.debug("telemetry sample failed: %s", exc)
            data = {}
            error = str(exc)
        with self._lock:
            self._latest = {**self._latest, **data, "sampled": time.time(), "error": error}
            return dict(self._latest)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            cached = dict(self._latest)
        if cached:
            return cached
        return self.sample()  # first call: measure now rather than show nothing

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        if not self._latest:
            self.sample()  # the first page load gets real numbers, not dashes
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-telemetry", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


__all__ = ["DEFAULT_INTERVAL", "Telemetry"]
