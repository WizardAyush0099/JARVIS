"""Text to speech.

The old project piped ``edge-tts`` MP3 bytes into ``pygame.mixer`` inside the GUI
thread, so the interface froze for the length of every sentence and one failure
killed the whole app.

Here:

* engines are pluggable - ``edge`` (natural neural voices, free), ``piper``
  (fully offline neural, ideal for a Pi 4), ``pyttsx3`` and ``espeak``
  (offline fallbacks), or ``none``
* a ``Speaker`` owns a queue and a worker thread, so speaking never blocks the
  GUI, web UI or brain
* playback goes through an ``AudioSink`` that tries several players
* generated audio is cached, so repeated phrases cost nothing
* the default voices are chosen to be calm, deep and assistant-like without
  cloning any real person's voice
"""

from __future__ import annotations

import hashlib
import os
import queue
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.logging_setup import get_logger

log = get_logger("voice.tts")

DEVANAGARI = range(0x0900, 0x097F)


# --------------------------------------------------------------------------- #
# language helpers
# --------------------------------------------------------------------------- #
def detect_language(text: str) -> str:
    """Return 'hi' for Devanagari or romanised Hindi, else 'en'."""
    if not text:
        return "en"
    if any(ord(char) in DEVANAGARI for char in text):
        return "hi"
    lowered = text.lower()
    hinglish = ("kya", "hai ", "nahi", "kar ", "kaise", "bhai", "acha", "theek", "mujhe", "tum ")
    if sum(1 for word in hinglish if word in lowered) >= 2:
        return "hi"
    return "en"


def clean_for_speech(text: str) -> str:
    """Strip things that sound terrible when spoken aloud."""
    import re

    spoken = text or ""
    spoken = re.sub(r"```.*?```", " (code omitted) ", spoken, flags=re.S)
    spoken = re.sub(r"`([^`]*)`", r"\1", spoken)
    spoken = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", spoken)  # markdown links
    spoken = re.sub(r"https?://\S+", "the link I've shown you", spoken)
    spoken = re.sub(r"[*_#>|]", " ", spoken)
    spoken = re.sub(r"^\s*[-•]\s*", "", spoken, flags=re.M)
    spoken = re.sub(r"\s+", " ", spoken)
    return spoken.strip()


# --------------------------------------------------------------------------- #
# audio sinks
# --------------------------------------------------------------------------- #
class AudioSink(ABC):
    name = "abstract"

    @abstractmethod
    def play_file(self, path: Path) -> bool: ...

    def stop(self) -> bool:
        """Cut the current sound short.  Best effort, never raises."""
        return False


class NullSink(AudioSink):
    name = "none"

    def play_file(self, path: Path) -> bool:
        return False


class PygameSink(AudioSink):
    name = "pygame"

    def __init__(self) -> None:
        import pygame  # type: ignore

        pygame.mixer.init()
        self._pygame = pygame
        #: set by `stop()` so the playback loop can cut a clip short.  Must exist
        #: before `play_file` reads it, or the first real playback raises inside
        #: the loop and is reported (and swallowed) as a generic failure.
        self._stopped = False

    def play_file(self, path: Path) -> bool:
        try:
            self._pygame.mixer.music.load(str(path))
            self._pygame.mixer.music.play()
            while self._pygame.mixer.music.get_busy():
                if self._stopped:
                    break
                time.sleep(0.05)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("pygame playback failed: %s", exc)
            return False
        finally:
            self._stopped = False

    def stop(self) -> bool:
        self._stopped = True
        try:
            self._pygame.mixer.music.stop()
            return True
        except Exception:  # noqa: BLE001
            return False


class CommandSink(AudioSink):
    """Play through whichever command line player the system has."""

    CANDIDATES = (
        ("paplay", ["paplay"]),
        ("aplay", ["aplay", "-q"]),
        ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]),
        ("mpv", ["mpv", "--no-video", "--really-quiet"]),
        ("mpg123", ["mpg123", "-q"]),
        ("afplay", []),  # macOS
    )

    def __init__(self) -> None:
        self._commands = []
        self._process: Optional[subprocess.Popen] = None
        for name, prefix in self.CANDIDATES:
            found = shutil.which(name)
            if found:
                self._commands.append([found, *prefix[1:]] if prefix else [found])
        self.name = "command:" + (os.path.basename(self._commands[0][0]) if self._commands else "none")

    @property
    def available(self) -> bool:
        return bool(self._commands)

    def play_file(self, path: Path) -> bool:
        for command in self._commands:
            try:
                # Popen rather than run() so "stop" can end playback mid-sentence
                process = subprocess.Popen(
                    [*command, str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._process = process
                try:
                    process.wait(timeout=180)
                finally:
                    self._process = None
                if process.returncode == 0:
                    return True
            except Exception:
                continue
        return False

    def stop(self) -> bool:
        process = self._process
        if process is None:
            return False
        try:
            process.terminate()
            return True
        except Exception:  # noqa: BLE001
            return False


def build_sink(preferred: Optional[str] = None) -> AudioSink:
    order = [preferred] if preferred else []
    order += ["pygame", "command"]
    for name in order:
        if name == "pygame":
            try:
                return PygameSink()
            except Exception:
                continue
        if name in {"command", None}:
            sink = CommandSink()
            if sink.available:
                return sink
    return NullSink()


# --------------------------------------------------------------------------- #
# engines
# --------------------------------------------------------------------------- #
class TTSEngine(ABC):
    name = "abstract"

    def available(self) -> bool:
        return False

    @abstractmethod
    def synthesize(self, text: str, language: str, cache_dir: Path, settings: Any) -> Optional[Path]:
        """Produce an audio file, or ``None`` if this engine cannot."""


class EdgeTTSEngine(TTSEngine):
    """Microsoft Edge neural voices - free, natural, needs internet once."""

    name = "edge"

    def available(self) -> bool:
        try:
            import edge_tts

            # not just importable: the API this engine actually calls must exist
            return hasattr(edge_tts, "Communicate")
        except Exception:
            return False

    def synthesize(self, text: str, language: str, cache_dir: Path, settings: Any) -> Optional[Path]:
        try:
            import asyncio

            import edge_tts  # type: ignore
        except Exception:
            return None

        tts = getattr(settings, "tts", None)
        voice = tts.voice_hi if language == "hi" else tts.voice_en
        voice = voice or ("hi-IN-MadhurNeural" if language == "hi" else "en-GB-RyanNeural")
        rate = int(getattr(tts, "rate", -8) or 0)
        volume = int(float(getattr(tts, "volume", 0.9) or 0.9) * 100)
        rate_arg = f"{rate:+d}%" if rate else "+0%"
        volume_arg = f"{max(0, min(100, volume)):+d}%"

        target = cache_dir / f"{hashlib.sha1((voice + text).encode()).hexdigest()}.mp3"
        if target.exists() and target.stat().st_size > 0:
            return target  # already synthesized: no second network round trip

        async def run() -> None:
            communicate = edge_tts.Communicate(text, voice, rate=rate_arg, volume=volume_arg)
            await communicate.save(str(target))

        try:
            asyncio.run(run())
        except RuntimeError:
            # already inside an event loop (e.g. an async web handler)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(run())
            finally:
                loop.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("edge-tts failed: %s", exc)
            return None
        return target if target.exists() and target.stat().st_size > 0 else None


class PiperTTSEngine(TTSEngine):
    """Piper - offline neural TTS, light enough for a Pi 4."""

    name = "piper"

    def available(self) -> bool:
        return shutil.which("piper") is not None

    def synthesize(self, text: str, language: str, cache_dir: Path, settings: Any) -> Optional[Path]:
        binary = shutil.which("piper")
        if not binary:
            return None
        model = os.environ.get("PIPER_MODEL_HI" if language == "hi" else "PIPER_MODEL_EN", "")
        model = model or os.environ.get("PIPER_MODEL", "")
        if not model or not Path(model).exists():
            log.debug("piper model not configured (set PIPER_MODEL)")
            return None
        target = cache_dir / f"{hashlib.sha1((model + text).encode()).hexdigest()}.wav"
        if target.exists() and target.stat().st_size > 0:
            return target
        try:
            result = subprocess.run(
                [binary, "--model", model, "--output_file", str(target)],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("piper failed: %s", exc)
            return None
        if result.returncode != 0 or not target.exists():
            return None
        return target


class Pyttsx3Engine(TTSEngine):
    """Offline fallback that speaks directly through espeak/nsss."""

    name = "pyttsx3"

    def available(self) -> bool:
        try:
            import pyttsx3

            return hasattr(pyttsx3, "init")
        except Exception:
            return False

    def synthesize(self, text: str, language: str, cache_dir: Path, settings: Any) -> Optional[Path]:
        # pyttsx3 speaks synchronously; the Speaker calls this on its own thread.
        try:
            import pyttsx3  # type: ignore
        except Exception:
            return None
        try:
            engine = pyttsx3.init()
            rate = engine.getProperty("rate") or 180
            try:
                rate = max(90, int(rate * (1 + (int(getattr(settings.tts, "rate", 0) or 0) / 100.0))))
            except Exception:
                pass
            engine.setProperty("rate", rate)
            engine.setProperty("volume", float(getattr(settings.tts, "volume", 0.9) or 0.9))
            engine.say(text)
            engine.runAndWait()
            return Path("__spoken__")  # marker: already played
        except Exception as exc:  # noqa: BLE001
            log.warning("pyttsx3 failed: %s", exc)
            return None


class EspeakEngine(TTSEngine):
    name = "espeak"

    def available(self) -> bool:
        return shutil.which("espeak-ng") is not None or shutil.which("espeak") is not None

    def synthesize(self, text: str, language: str, cache_dir: Path, settings: Any) -> Optional[Path]:
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        if not binary:
            return None
        target = cache_dir / f"{hashlib.sha1((binary + text).encode()).hexdigest()}.wav"
        if target.exists() and target.stat().st_size > 0:
            return target
        voice = "hi" if language == "hi" else "en-gb"
        try:
            subprocess.run(
                [binary, "-v", voice, "-s", "150", "-w", str(target), text],
                capture_output=True,
                timeout=90,
            )
        except Exception:
            return None
        return target if target.exists() else None


def build_engine(name: str, settings: Any = None) -> TTSEngine:
    name = (name or "edge").lower()
    candidates = {
        "edge": [EdgeTTSEngine, PiperTTSEngine, Pyttsx3Engine, EspeakEngine],
        "piper": [PiperTTSEngine, EdgeTTSEngine, Pyttsx3Engine, EspeakEngine],
        "pyttsx3": [Pyttsx3Engine, EspeakEngine, EdgeTTSEngine],
        "espeak": [EspeakEngine, Pyttsx3Engine],
        "none": [],
    }.get(name, [EdgeTTSEngine, PiperTTSEngine, Pyttsx3Engine, EspeakEngine])

    for engine_class in candidates:
        engine = engine_class()
        try:
            if engine.available():
                return engine
        except Exception:
            continue
    log.warning("no TTS engine available (wanted '%s')", name)
    return NullEngine()


class NullEngine(TTSEngine):
    name = "none"

    def synthesize(self, text: str, language: str, cache_dir: Path, settings: Any) -> Optional[Path]:
        return None


# --------------------------------------------------------------------------- #
# speaker
# --------------------------------------------------------------------------- #
class Speaker:
    """Queue-based, non-blocking speech output."""

    def __init__(
        self,
        settings: Any,
        events: Any = None,
        engine: Optional[TTSEngine] = None,
        sink: Optional[AudioSink] = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.engine = engine or build_engine(getattr(settings.tts, "engine", "edge"), settings)
        self.sink = sink or build_sink()
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._muted = not bool(getattr(settings.tts, "enabled", True))
        #: False when the audio is delivered to a browser instead of the local sink
        self._local_output = True
        cache = getattr(getattr(settings, "paths", None), "voice_cache_dir", None)
        self.cache_dir = Path(cache) if cache else Path("assets") / "voice_cache"
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        #: cleaned text -> audio file already produced this run, so a repeated
        #: sentence costs nothing on any engine (and no network call on edge)
        self._path_cache: Dict[str, Path] = {}
        self.spoken_count = 0
        self.failures = 0
        self.last_error = ""

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-tts", daemon=True)
        self._thread.start()
        log.info("speaker ready (engine=%s, sink=%s)", self.engine.name, self.sink.name)

    def stop(self, wait: float = 1.0) -> None:
        self._stop.set()
        self._queue.put(("__stop__", None))
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=wait)
        self._thread = None

    # -- state -------------------------------------------------------------
    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    @property
    def available(self) -> bool:
        return not isinstance(self.engine, NullEngine) and not isinstance(self.sink, NullSink)

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if self._muted:
            self.clear_queue()

    def toggle_muted(self) -> bool:
        self.set_muted(not self._muted)
        return self._muted

    @property
    def local_output(self) -> bool:
        return self._local_output

    def set_local_output(self, enabled: bool) -> bool:
        """Choose whether speech is played here or handed to a remote client."""
        self._local_output = bool(enabled)
        if not self._local_output:
            self.interrupt()
        return self._local_output

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": not self._muted,
            "engine": self.engine.name,
            "sink": self.sink.name,
            "available": self.available,
            "local_output": self._local_output,
            "speaking": self.speaking,
            "queued": self._queue.qsize(),
            "spoken": self.spoken_count,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    def _already_synthesized(self, spoken: str) -> Optional[Path]:
        cached = self._path_cache.get(spoken)
        if cached is not None and Path(cached).exists():
            return Path(cached)
        return None

    def _synthesize(self, spoken: str) -> Optional[Path]:
        """Run the engine once per phrase, remembering the file it produced.

        ``"__spoken__"`` is returned as-is for engines that speak directly
        (pyttsx3) and leave no file behind - the caller knows what that means.
        """
        cached = self._already_synthesized(spoken)
        if cached is not None:
            return cached
        try:
            path = self.engine.synthesize(spoken, detect_language(spoken), self.cache_dir, self.settings)
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self.last_error = str(exc)
            log.warning("TTS synthesis failed: %s", exc)
            return None
        if path is None:
            return None
        if str(path) == "__spoken__":
            return path
        self._path_cache[spoken] = Path(path)
        return Path(path)

    def synthesize_only(self, text: str) -> Optional[Path]:
        """Synthesize without playing anything.

        Used to hand the same neural voice to a browser client: the audio is
        produced by the exact engine and cache the local speaker uses, but the
        client is what plays it.  Returns ``None`` when the voice is muted or no
        engine can produce audio.
        """
        spoken = clean_for_speech(text)
        if not spoken or self._muted or isinstance(self.engine, NullEngine):
            return None
        path = self._synthesize(spoken)
        if path is None or str(path) == "__spoken__":
            # pyttsx3 speaks on the device and has no file a browser could play
            return None
        return Path(path)

    def interrupt(self) -> None:
        """Stop speaking now: drop what is queued and cut the current sound."""
        self.clear_queue()
        try:
            self.sink.stop()
        except Exception:  # noqa: BLE001
            pass
        self._speaking.clear()

    # -- speaking ----------------------------------------------------------
    def speak(self, text: str, on_done: Optional[Callable[[], None]] = None) -> bool:
        """Queue text for speech.  Returns False when speech is impossible."""
        spoken = clean_for_speech(text)
        if not spoken or self._muted or not self._local_output or isinstance(self.engine, NullEngine):
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass
            return False
        self._queue.put((spoken, on_done))
        return True

    def say_now(self, text: str) -> bool:
        """Blocking variant used by tools/tests."""
        spoken = clean_for_speech(text)
        if not spoken or self._muted or isinstance(self.engine, NullEngine):
            return False
        return self._utter(spoken)

    def clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    # -- worker ------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            text, on_done = item
            if text == "__stop__":
                break
            if self._muted:
                continue
            self._speaking.set()
            self._publish_state("speaking")
            try:
                self._utter(text)
            finally:
                self._speaking.clear()
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:
                        pass
                if self._queue.empty():
                    self._publish_state("idle")

    def _utter(self, text: str) -> bool:
        path = self._synthesize(text)
        if path is None:
            self.failures += 1
            self.last_error = self.last_error or "synthesis produced no audio"
            return False
        if str(path) == "__spoken__":  # pyttsx3 already played it
            self.spoken_count += 1
            return True
        try:
            played = self.sink.play_file(Path(path))
        except Exception as exc:  # noqa: BLE001
            played = False
            self.last_error = str(exc)
        if played:
            self.spoken_count += 1
            return True
        self.failures += 1
        self.last_error = self.last_error or f"could not play audio with {self.sink.name}"
        return False

    def _publish_state(self, state: str) -> None:
        if self.events is None:
            return
        try:
            self.events.publish("speech", message=state, state=state)
        except Exception:
            pass


def build_speaker(settings: Any, events: Any = None) -> Speaker:
    speaker = Speaker(settings, events)
    speaker.start()
    return speaker


__all__ = [
    "AudioSink",
    "EdgeTTSEngine",
    "EspeakEngine",
    "NullEngine",
    "PiperTTSEngine",
    "PygameSink",
    "Pyttsx3Engine",
    "Speaker",
    "TTSEngine",
    "build_engine",
    "build_sink",
    "build_speaker",
    "clean_for_speech",
    "detect_language",
]
