"""In-process event bus.

The brain runs on worker threads while the Tkinter GUI and the web UI both need
a live picture of what is happening.  Instead of polling each other they all
subscribe to one bus, which is the single source of truth for:

* the assistant *state* (idle / listening / thinking / speaking / error)
* chat messages
* tool activity
* errors and status changes
* pending confirmations

Publishing is thread-safe and a misbehaving listener can never break a caller.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Callable, Dict, List

#: Assistant states used by every front-end.
STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_WORKING = "working"  # a tool is running
STATE_SPEAKING = "speaking"
STATE_ERROR = "error"

Listener = Callable[[Dict[str, Any]], None]


class EventBus:
    """Thread-safe fan-out with a small replay buffer for late subscribers."""

    def __init__(self, history: int = 300) -> None:
        self._lock = threading.RLock()
        self._listeners: List[Listener] = []
        self._history: List[Dict[str, Any]] = []
        self._history_size = max(10, int(history))
        self._counter = itertools.count(1)
        self._state = STATE_IDLE
        self._state_note = ""

    # -- subscription ------------------------------------------------------
    def subscribe(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    # -- publishing --------------------------------------------------------
    def publish(self, kind: str, **payload: Any) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "id": next(self._counter),
            "kind": kind,
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S"),
            "state": self._state,
        }
        event.update(payload)

        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_size:
                del self._history[: len(self._history) - self._history_size]
            listeners = list(self._listeners)

        for listener in listeners:
            try:
                listener(event)
            except Exception:  # a broken listener must not stop the others
                continue
        return event

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def state_note(self) -> str:
        with self._lock:
            return self._state_note

    def set_state(self, state: str, note: str = "") -> Dict[str, Any]:
        with self._lock:
            changed = state != self._state or note != self._state_note
            self._state = state
            self._state_note = note
        if changed:
            return self.publish("state", state=state, note=note)
        return {"state": state, "note": note, "kind": "state"}

    # -- history -----------------------------------------------------------
    def recent(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history[-max(0, int(limit)) :])

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()


__all__ = [
    "EventBus",
    "Listener",
    "STATE_ERROR",
    "STATE_IDLE",
    "STATE_LISTENING",
    "STATE_SPEAKING",
    "STATE_THINKING",
    "STATE_WORKING",
]
