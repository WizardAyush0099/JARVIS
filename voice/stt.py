"""Speech to text.

The old ``SpeechToText.py`` ran ``recognizer.listen()`` in a loop inside the GUI
thread and raised on every failure, so a noisy room or a dropped connection took
the whole assistant down.

Now:

* engines are pluggable: Google (default, online), Whisper, PocketSphinx
  (fully offline) or Vosk (offline, light - the best fit for a Pi 4)
* every failure mode is a value, not an exception: no speech, unintelligible
  audio, service unreachable
* the loop lives on a daemon thread and can be paused while JARVIS is talking,
  which stops the classic feedback loop of the assistant hearing itself
* optional wake word, plus push-to-talk as a reliable fallback
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from config.settings import module_present
from core.logging_setup import get_logger

log = get_logger("voice.stt")


@dataclass
class Transcript:
    text: str = ""
    ok: bool = False
    confidence: float = 0.0
    error: str = ""
    language: str = ""

    @property
    def timed_out(self) -> bool:
        return not self.ok and "no speech" in (self.error or "").lower()


class STTEngine(ABC):
    name = "abstract"

    def available(self) -> bool:
        return False

    @abstractmethod
    def listen_once(self, timeout: float = 6.0, phrase_time_limit: float = 8.0) -> Transcript: ...

    def calibrate(self, seconds: float = 1.0) -> bool:
        return False


class NullSTTEngine(STTEngine):
    name = "none"

    def listen_once(self, timeout: float = 6.0, phrase_time_limit: float = 8.0) -> Transcript:
        return Transcript(ok=False, error="microphone support is not installed")


class SpeechRecognitionEngine(STTEngine):
    """Wrapper around the widely used ``speech_recognition`` package."""

    def __init__(self, settings: Any, backend: str = "google") -> None:
        self.settings = settings
        self.name = backend
        self._recognizer = None
        self._microphone = None
        self._ready = False
        self._error = ""

    def available(self) -> bool:
        if self._ready:
            return True
        try:
            import speech_recognition as sr  # type: ignore

            stt = getattr(self.settings, "stt", None)
            recognizer = sr.Recognizer()
            if stt is not None:
                recognizer.energy_threshold = int(getattr(stt, "energy_threshold", 300))
                recognizer.pause_threshold = float(getattr(stt, "pause_threshold", 0.8))
            recognizer.dynamic_energy_threshold = True
            microphone = sr.Microphone(device_index=getattr(stt, "device_index", None))
            self._recognizer = recognizer
            self._microphone = microphone
            self._ready = True
            return True
        except Exception as exc:  # noqa: BLE001 - pyaudio missing is the common case
            self._error = str(exc)
            log.info("speech_recognition unavailable: %s", exc)
            return False

    def _language(self) -> str:
        stt = getattr(self.settings, "stt", None)
        return str(getattr(stt, "language", "en-IN") or "en-IN")

    def listen_once(self, timeout: float = 6.0, phrase_time_limit: float = 8.0) -> Transcript:
        if not self.available():
            return Transcript(ok=False, error=f"speech recognition is unavailable ({self._error})")
        import speech_recognition as sr  # type: ignore

        try:
            with self._microphone as source:
                try:
                    self._recognizer.adjust_for_ambient_noise(source, duration=0.35)
                except Exception:
                    pass
                audio = self._recognizer.listen(
                    source, timeout=timeout, phrase_time_limit=phrase_time_limit
                )
        except sr.WaitTimeoutError:
            return Transcript(ok=False, error="no speech detected")
        except OSError as exc:
            return Transcript(ok=False, error=f"microphone error: {exc}")
        except Exception as exc:  # noqa: BLE001
            return Transcript(ok=False, error=f"could not capture audio: {exc}")

        return self._recognize(audio)

    def _recognize(self, audio: Any) -> Transcript:
        import speech_recognition as sr  # type: ignore  # noqa: F401 - import proves it is usable

        backend = self.name.lower()
        language = self._language()
        try:
            if backend == "sphinx":
                if not module_present("pocketsphinx"):
                    return Transcript(
                        ok=False,
                        error="offline mode needs pocketsphinx: pip install pocketsphinx",
                    )
                text = self._recognizer.recognize_sphinx(audio)
            elif backend == "whisper":
                text = self._recognizer.recognize_whisper(audio, model="base", language=None)
            else:
                text = self._recognizer.recognize_google(audio, language=language)
        except sr.UnknownValueError:
            return Transcript(ok=False, error="I couldn't make out what you said")
        except sr.RequestError as exc:
            return Transcript(ok=False, error=f"speech service unreachable: {exc}")
        except Exception as exc:  # noqa: BLE001
            return Transcript(ok=False, error=f"recognition failed: {exc}")

        text = (text or "").strip()
        if not text:
            return Transcript(ok=False, error="I didn't catch that")
        return Transcript(text=text, ok=True, confidence=0.8, language=language)

    def calibrate(self, seconds: float = 1.0) -> bool:
        if not self.available():
            return False
        try:
            with self._microphone as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=seconds)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("microphone calibration failed: %s", exc)
            return False


class VoskEngine(STTEngine):
    """Offline recognition with Vosk: small, fast, Pi-friendly."""

    name = "vosk"

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._model = None
        self._error = ""

    def available(self) -> bool:
        if self._model is not None:
            return True
        model_path = str(getattr(self.settings.stt, "vosk_model_path", "") or "")
        if not model_path:
            self._error = "VOSK_MODEL_PATH is not set"
            return False
        try:
            import json as jsonlib
            from pathlib import Path

            import vosk  # type: ignore

            if not Path(model_path).exists():
                self._error = f"model folder not found: {model_path}"
                return False
            vosk.SetLogLevel(-1)
            self._model = vosk.Model(model_path)
            self._json = jsonlib
            return True
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            return False

    def listen_once(self, timeout: float = 6.0, phrase_time_limit: float = 8.0) -> Transcript:
        if not self.available():
            return Transcript(ok=False, error=f"Vosk is unavailable ({self._error})")
        try:
            import pyaudio  # type: ignore
            import vosk  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return Transcript(ok=False, error=f"Vosk needs pyaudio: {exc}")

        rate = 16000
        stream = None
        audio_interface = None
        try:
            audio_interface = pyaudio.PyAudio()
            stream = audio_interface.open(
                format=pyaudio.paInt16, channels=1, rate=rate, input=True, frames_per_buffer=4000
            )
            stream.start_stream()
            recognizer = vosk.KaldiRecognizer(self._model, rate)
            deadline = time.time() + max(1.0, float(timeout) + float(phrase_time_limit))
            collected = ""
            silence_started = None
            while time.time() < deadline:
                data = stream.read(2000, exception_on_overflow=False)
                if recognizer.AcceptWaveform(data):
                    payload = self._json.loads(recognizer.Result())
                    collected = (payload.get("text") or "").strip()
                    if collected:
                        break
                    silence_started = silence_started or time.time()
                elif data == b"\x00" * len(data):
                    if silence_started is None:
                        silence_started = time.time()
                    elif time.time() - silence_started > 2.0:
                        break
            if not collected:
                collected = (self._json.loads(recognizer.FinalResult()).get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001
            return Transcript(ok=False, error=f"Vosk capture failed: {exc}")
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            if audio_interface is not None:
                try:
                    audio_interface.terminate()
                except Exception:
                    pass

        if not collected:
            return Transcript(ok=False, error="no speech detected")
        return Transcript(text=collected, ok=True, confidence=0.7)


def build_engine(settings: Any) -> STTEngine:
    name = str(getattr(settings.stt, "engine", "google") or "google").lower()
    if name in {"none", "off", "disabled"}:
        return NullSTTEngine()
    if name == "vosk":
        engine = VoskEngine(settings)
        if engine.available():
            return engine
        fallback = SpeechRecognitionEngine(settings, "sphinx")
        if fallback.available():
            log.warning("Vosk unavailable; falling back to offline PocketSphinx")
            return fallback
        return NullSTTEngine()
    engine = SpeechRecognitionEngine(settings, name)
    if engine.available():
        return engine
    return NullSTTEngine()


class Listener:
    """Background microphone loop with wake word, pausing and push-to-talk."""

    def __init__(
        self,
        settings: Any,
        events: Any = None,
        on_transcript: Optional[Callable[[str, Transcript], None]] = None,
        engine: Optional[STTEngine] = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.on_transcript = on_transcript
        self.engine = engine or build_engine(settings)
        self.wake_word = str(getattr(settings.stt, "wake_word", "") or "").lower()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._busy = threading.Lock()
        self.last_error = ""
        self.heard_count = 0

    # -- lifecycle ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings.stt, "enabled", False)) and not isinstance(
            self.engine, NullSTTEngine
        )

    def start(self) -> bool:
        if not self.enabled:
            log.info("speech input is disabled")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-stt", daemon=True)
        self._thread.start()
        log.info("listening on %s (wake word: %s)", self.engine.name, self.wake_word or "none")
        return True

    def stop(self, wait: float = 1.5) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=wait)
        self._thread = None

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "engine": self.engine.name,
            "running": self.running,
            "paused": self.paused,
            "wake_word": self.wake_word or None,
            "heard": self.heard_count,
            "available": self.engine.available(),
            "last_error": self.last_error,
        }

    # -- recognition -------------------------------------------------------
    def listen_once(self, timeout: float = 6.0, phrase_time_limit: float = 8.0) -> Transcript:
        """Push-to-talk: one blocking capture."""
        if isinstance(self.engine, NullSTTEngine):
            return Transcript(ok=False, error="microphone support is not installed")
        if not self._busy.acquire(blocking=False):
            return Transcript(ok=False, error="already listening")
        try:
            self._publish("listening")
            transcript = self.engine.listen_once(timeout=timeout, phrase_time_limit=phrase_time_limit)
        finally:
            self._busy.release()
        if transcript.ok:
            self.heard_count += 1
        else:
            self.last_error = transcript.error
        self._publish("idle")
        return transcript

    def _run(self) -> None:
        idle_timeout = 4.0
        while not self._stop.is_set():
            if self._pause.is_set():
                time.sleep(0.2)
                continue
            transcript = self.listen_once(timeout=idle_timeout, phrase_time_limit=8.0)
            if self._stop.is_set():
                break
            if not transcript.ok:
                if transcript.timed_out:
                    continue
                log.debug("listening hiccup: %s", transcript.error)
                if self.events is not None:
                    self.events.publish("stt_error", message=transcript.error)
                time.sleep(0.5)
                continue

            text = transcript.text
            if self.wake_word:
                lowered = text.lower()
                if self.wake_word not in lowered:
                    log.debug("ignored (no wake word): %s", text[:60])
                    continue
                index = lowered.index(self.wake_word) + len(self.wake_word)
                text = text[index:].lstrip(" ,.:;-")
                if not text:
                    continue
            if self.on_transcript is not None:
                try:
                    self.on_transcript(text, transcript)
                except Exception:  # pragma: no cover - never kill the listener
                    log.exception("transcript handler failed")

    def _publish(self, state: str) -> None:
        if self.events is None:
            return
        try:
            self.events.publish("mic", message=state, state=state)
        except Exception:
            pass


__all__ = [
    "Listener",
    "NullSTTEngine",
    "SpeechRecognitionEngine",
    "STTEngine",
    "Transcript",
    "VoskEngine",
    "build_engine",
]
