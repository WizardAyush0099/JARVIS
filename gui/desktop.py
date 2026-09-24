"""Desktop GUI (Tkinter).

The old ``GUI.py`` was a PyQt5 window that called the brain directly from the GUI
thread, so the interface froze while JARVIS thought or spoke, and a single
exception took the window down.  It is also a heavy dependency to pull onto a Pi.

This version:

* uses **Tkinter** from the standard library (no extra install on Raspberry Pi OS)
* never blocks: work happens on the brain's own threads and results arrive
  through the event bus into a queue that Tk polls with ``after()``
* gives **every** assistant message its own COPY button, as the spec asks
* still shows listening / thinking / speaking state, tool activity, errors and
  provider status
* keeps the animation budget low on purpose

Run it with ``python main.py --gui``.
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from config.settings import Settings
from core.brain import Jarvis
from core.events import EventBus
from core.logging_setup import get_logger

log = get_logger("gui")

# palette (kept in one place so the desktop and web UIs look related)
BG = "#04070d"
BG_PANEL = "#0a1622"
BG_CARD = "#081521"
FG = "#d6e8f5"
FG_DIM = "#8fb0c6"
MUTED = "#5d7d94"
CYAN = "#22d3ee"
AMBER = "#f5b642"
GREEN = "#34d399"
RED = "#f87171"
VIOLET = "#a78bfa"

STATE_COLOURS = {
    "idle": CYAN,
    "listening": GREEN,
    "thinking": AMBER,
    "working": AMBER,
    "speaking": VIOLET,
    "error": RED,
}


class JarvisWindow:
    def __init__(self, settings: Settings, jarvis: Optional[Jarvis] = None) -> None:
        import tkinter as tk

        self.tk = tk
        self.settings = settings
        self.events: EventBus = (jarvis.events if jarvis else EventBus())
        self.jarvis = jarvis or Jarvis(settings, self.events, with_tts=True, with_stt=bool(settings.stt.enabled))
        self.inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._last_reply = ""
        self._unsubscribe = self.events.subscribe(lambda event: self.inbox.put(event))

        self.root = tk.Tk()
        self.root.title(f"{settings.assistant_name} - {settings.owner_name}")
        self.root.geometry("880x700")
        self.root.minsize(560, 520)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_header()
        self._build_chat()
        self._build_composer()
        self._build_statusbar()

        self.jarvis.start()
        self._push("system", f"{settings.assistant_name} online. Ask me anything.", {})
        self.root.after(80, self._drain)
        self.root.after(4000, self._refresh_status)

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #
    def _build_header(self) -> None:
        tk = self.tk
        header = tk.Frame(self.root, bg=BG_PANEL, height=52)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="J A R V I S", bg=BG_PANEL, fg="#eaf8ff",
            font=("TkDefaultFont", 14, "bold"),
        ).pack(side="left", padx=16)

        self.state_label = tk.Label(header, text="● booting", bg=BG_PANEL, fg=MUTED, font=("TkFixedFont", 10))
        self.state_label.pack(side="left", padx=8)

        self.provider_label = tk.Label(header, text="", bg=BG_PANEL, fg=FG_DIM, font=("TkFixedFont", 9))
        self.provider_label.pack(side="right", padx=16)

    def _build_chat(self) -> None:
        tk = self.tk
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill="both", expand=True, padx=10, pady=(8, 0))

        self.canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.messages = tk.Frame(self.canvas, bg=BG)
        self._window = self.canvas.create_window((0, 0), window=self.messages, anchor="nw")
        self.messages.bind(
            "<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.bind(
            "<Configure>", lambda event: self.canvas.itemconfigure(self._window, width=event.width)
        )
        # mouse wheel scrolling on Pi and desktop
        self.canvas.bind_all("<Button-4>", lambda _e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind_all("<Button-5>", lambda _e: self.canvas.yview_scroll(2, "units"))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 60)), "units"))

    def _build_composer(self) -> None:
        tk = self.tk
        bar = tk.Frame(self.root, bg=BG_PANEL)
        bar.pack(fill="x", side="bottom", padx=10, pady=8)

        self.entry = tk.Entry(bar, bg=BG_CARD, fg=FG, insertbackground=CYAN, relief="flat", font=("TkDefaultFont", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        self.entry.bind("<Return>", lambda _e: self.send())
        self.entry.focus_set()

        for text, command, colour in (
            ("SEND", self.send, CYAN),
            ("MIC", self.toggle_mic, GREEN),
            ("MUTE", self.toggle_mute, AMBER),
            ("CLEAR", self.clear, RED),
        ):
            tk.Button(
                bar, text=text, command=command, bg=BG_CARD, fg=colour,
                activebackground=BG_CARD, activeforeground="#ffffff",
                relief="flat", font=("TkFixedFont", 9, "bold"), padx=10, pady=6,
            ).pack(side="left", padx=2)

    def _build_statusbar(self) -> None:
        tk = self.tk
        self.status = tk.StringVar(value="ready")
        tk.Label(
            self.root, textvariable=self.status, bg=BG, fg=MUTED,
            anchor="w", font=("TkFixedFont", 9),
        ).pack(fill="x", side="bottom", padx=14, pady=(0, 6))

    # ------------------------------------------------------------------ #
    # message cards
    # ------------------------------------------------------------------ #
    def _push(self, role: str, text: str, meta: Dict[str, Any]) -> None:
        tk = self.tk
        card = tk.Frame(self.messages, bg=BG_CARD, highlightbackground="#12303f", highlightthickness=1)
        card.pack(fill="x", pady=5, padx=2)

        head = tk.Frame(card, bg=BG_CARD)
        head.pack(fill="x", padx=10, pady=(7, 0))

        who = "you" if role == "user" else self.settings.assistant_name.lower()
        colour = CYAN if role == "user" else (MUTED if role == "system" else GREEN)
        tk.Label(head, text=who.upper(), bg=BG_CARD, fg=colour, font=("TkFixedFont", 8, "bold")).pack(side="left")
        tk.Label(
            head, text=meta.get("time") or datetime.now().strftime("%H:%M:%S"),
            bg=BG_CARD, fg=MUTED, font=("TkFixedFont", 8),
        ).pack(side="left", padx=8)

        if role == "assistant":
            tk.Button(
                head, text="COPY", command=lambda t=text: self.copy(t),
                bg=BG_CARD, fg=FG_DIM, activebackground=BG_CARD, activeforeground=CYAN,
                relief="flat", font=("TkFixedFont", 8, "bold"),
            ).pack(side="right")
            self._last_reply = text

        body = tk.Label(
            card, text=text, bg=BG_CARD, fg=FG, justify="left", anchor="w",
            wraplength=780, font=("TkDefaultFont", 10),
        )
        body.pack(fill="x", padx=10, pady=(2, 9))

        steps = meta.get("steps") or []
        if steps:
            summary = "  ".join(f"{'OK' if s.get('ok') else 'FAILED'}:{s.get('tool')}" for s in steps)
            tk.Label(card, text=summary, bg=BG_CARD, fg=MUTED, font=("TkFixedFont", 8), anchor="w").pack(
                fill="x", padx=10, pady=(0, 8)
            )

        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)
        self.status.set(self._last_reply[:120] if self._last_reply else "")

    def copy(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text or "")
            self.status.set("copied to clipboard")
        except Exception as exc:  # noqa: BLE001 - clipboard can be unavailable on headless X
            self.status.set(f"clipboard unavailable: {exc}")

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def send(self, text: Optional[str] = None) -> None:
        message = (text if text is not None else self.entry.get()).strip()
        if not message:
            return
        self.entry.delete(0, "end")
        self._push("user", message, {})
        self._run_async(lambda: self.jarvis.handle(message, "gui"))

    def toggle_mic(self) -> None:
        def work() -> None:
            reply = self.jarvis.listen_once(6.0)
            if reply.error:
                self.inbox.put({"kind": "notice", "message": reply.text})

        self._run_async(work)

    def toggle_mute(self) -> None:
        muted = self.jarvis.set_muted(not (self.jarvis.speaker.muted if self.jarvis.speaker else True))
        self.status.set("voice muted" if muted else "voice on")

    def clear(self) -> None:
        self.jarvis.clear_history()
        for child in self.messages.winfo_children():
            child.destroy()
        self._last_reply = ""
        self._push("system", "Conversation cleared.", {})

    def _run_async(self, work) -> None:
        def runner() -> None:
            try:
                work()
            except Exception as exc:  # noqa: BLE001 - surface instead of dying
                self.inbox.put({"kind": "error", "message": str(exc)})

        threading.Thread(target=runner, daemon=True, name="gui-task").start()

    # ------------------------------------------------------------------ #
    # event pump
    # ------------------------------------------------------------------ #
    def _drain(self) -> None:
        try:
            while True:
                event = self.inbox.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(90, self._drain)

    def _handle_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "state":
            state = event.get("state", "idle")
            note = event.get("note") or state
            self.state_label.configure(text=f"● {note}", fg=STATE_COLOURS.get(state, MUTED))
        elif kind == "message":
            if event.get("role") == "assistant":
                self._push("assistant", event.get("text", ""), {"time": event.get("time"), "steps": event.get("steps")})
        elif kind == "tool_start":
            self.status.set(f"running {event.get('tool')}...")
        elif kind == "tool_result":
            if not event.get("ok"):
                self._push("system", f"{event.get('tool')} failed: {event.get('error', '')}", {})
        elif kind in {"notice", "error"}:
            self._push("system", str(event.get("message", "")), {})
        elif kind == "confirm":
            self._push("system", event.get("message", "confirmation required"), {})
        elif kind == "provider":
            self.status.set(str(event.get("message", "")))

    def _refresh_status(self) -> None:
        try:
            status = self.jarvis.status()
            live = [p for p in status["providers"] if p["status"] == "ready"]
            chain = live[0]["slug"] if live else "offline"
            self.provider_label.configure(
                text=f"brain:{chain}  mic:{status['mic'].get('engine')}  tts:{status['speech'].get('engine')}"
            )
        except Exception:
            pass
        self.root.after(4000, self._refresh_status)

    # ------------------------------------------------------------------ #
    def on_close(self) -> None:
        try:
            self._unsubscribe()
        except Exception:
            pass
        try:
            self.jarvis.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch(settings: Optional[Settings] = None) -> None:
    """Entry point used by ``python main.py --gui``."""
    settings = settings or Settings.load()
    try:
        window = JarvisWindow(settings)
    except Exception as exc:  # noqa: BLE001 - usually no display on a headless Pi
        print(
            "Could not open the desktop window: "
            f"{exc}\n"
            "This needs a graphical session (a monitor on the Pi, or X forwarding).\n"
            "Try `python main.py --web` and open it in a browser instead."
        )
        return
    window.run()


__all__ = ["JarvisWindow", "launch"]
