"""Web server.

One small FastAPI app gives JARVIS the interface the spec asks for: open it from
any phone browser on the same network, from the Pi's own desktop browser, or from
VS Code's port forwarding.

* the SPA in ``gui/web/`` is served with no build step (nothing to compile on the
  Pi, and it works offline)
* ``/api/*`` is the request/response surface
* ``/ws`` streams state, tool activity, messages and errors live
* ``/media/generated`` serves images JARVIS produced, so "I generated an image"
  can actually be shown rather than claimed
* an optional shared token (``JARVIS_WEB_TOKEN``) gates the API, because this
  listens on the LAN
"""

from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import Settings, module_present
from core.brain import Jarvis
from core.logging_setup import get_logger, ring_handler

log = get_logger("server")

WEB_DIR = Path(__file__).resolve().parent.parent / "gui" / "web"
TOKEN_HEADER = "x-jarvis-token"

try:  # FastAPI is the supported path
    from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    HAVE_FASTAPI = True
except Exception:  # pragma: no cover - import error is reported by main.py
    HAVE_FASTAPI = False
    FastAPI = None  # type: ignore


# --------------------------------------------------------------------------- #
# state shared with the routes
# --------------------------------------------------------------------------- #
class ServerState:
    """The one JARVIS instance the interface drives, plus its voice routing."""

    def __init__(self, settings: Settings, jarvis: Optional[Jarvis] = None) -> None:
        self.settings = settings
        self.jarvis = jarvis or Jarvis(
            settings,
            with_tts=True,
            with_stt=bool(settings.stt.enabled),
        )
        self.started = False
        #: how many web clients are attached right now
        self.clients = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if not self.started:
                self.jarvis.start()
                self.started = True

    def stop(self) -> None:
        with self._lock:
            if self.started:
                self.jarvis.stop()
                self.started = False

    # -- voice -------------------------------------------------------------
    def voice_mode(self) -> str:
        return self.jarvis.voice_output

    def set_voice_mode(self, mode: str) -> str:
        """`browser`, `device` or `off`, remembering that a client asked."""
        return self.jarvis.set_voice_output(mode)

    def toggle_voice(self) -> str:
        """The AI-voice mute button: off <-> wherever it was before."""
        if self.jarvis.voice_output == "off":
            return self.set_voice_mode(self.jarvis.voice_routing)
        self.jarvis.set_muted(True)
        return "off"

    def attach_client(self) -> int:
        with self._lock:
            self.clients += 1
            return self.clients

    def detach_client(self) -> int:
        """Last client gone: give the voice back to the machine.

        Otherwise JARVIS would stay silent at the keyboard after you close the
        browser tab, because the audio was pointed at a client nobody is using.
        """
        with self._lock:
            self.clients = max(0, self.clients - 1)
            remaining = self.clients
        if remaining == 0 and self.jarvis.voice_output == "browser":
            fallback = str(getattr(self.settings.tts, "voice_output", "device") or "device")
            if fallback == "browser":
                fallback = "device"
            try:
                self.set_voice_mode(fallback)
            except ValueError:  # pragma: no cover - no speaker on this machine
                self.jarvis.set_muted(True)
        return remaining

    def speech_for(self, text: str) -> Optional[Dict[str, str]]:
        """Synthesize a reply for the browser, when that is where voice goes.

        Uses the very same engine and cache as local playback - this is the
        backend TTS, not the browser's toy speech synthesis.  Called by the
        client *after* it has shown the text, so a slow voice never delays the
        answer appearing on screen.
        """
        if not text or self.jarvis.voice_output != "browser":
            return None
        path = self.jarvis.synthesize_speech(text)
        if path is None:
            return None
        return {"url": f"/media/voice/{Path(path).name}", "text": text}


def _authorised(request: Any, token_header: Optional[str], token_query: Optional[str]) -> bool:
    settings: Settings = request.app.state.jarvis_settings
    expected = settings.web.token
    if not expected:
        return True
    return token_header == expected or token_query == expected


def create_app(settings: Optional[Settings] = None, jarvis: Optional[Jarvis] = None) -> "FastAPI":
    if not HAVE_FASTAPI:
        raise RuntimeError(
            "FastAPI/uvicorn are not installed. Run: pip install -r requirements.txt"
        )
    settings = settings or Settings.load()
    state = ServerState(settings, jarvis)
    app = FastAPI(title="JARVIS", docs_url=None, redoc_url=None)
    app.state.jarvis_settings = settings
    app.state.server_state = state

    # ---------------------------------------------------------------- #
    # lifecycle
    # ---------------------------------------------------------------- #
    @app.on_event("startup")
    async def _startup() -> None:
        state.start()
        log.info("web interface ready on http://%s:%s", settings.web.host, settings.web.port)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        state.stop()

    # ---------------------------------------------------------------- #
    # helpers
    # ---------------------------------------------------------------- #
    def require_token(request: Request, header_token: Optional[str], query_token: Optional[str]) -> None:
        if not _authorised(request, header_token, query_token):
            raise HTTPException(status_code=401, detail="invalid or missing JARVIS token")

    def snapshot() -> Dict[str, Any]:
        jarvis_instance = state.jarvis
        return {
            "identity": {
                "assistant": settings.assistant_name,
                "owner": settings.owner_name,
            },
            "settings": settings.public_dict(),
            "status": jarvis_instance.status(),
            "messages": jarvis_instance.memory.export(80),
            "events": jarvis_instance.events.recent(40),
            "tools": sorted({tool.name for tool in _tool_list()}),
            "token_required": bool(settings.web.token),
            "voice": {
                "mode": state.voice_mode(),
                "routing": jarvis_instance.voice_routing,
                "mic_muted": jarvis_instance.mic_muted,
                "configured": bool(getattr(settings.tts, "enabled", False)),
                "available": bool(jarvis_instance.speaker and jarvis_instance.speaker.available),
                "clients": state.clients,
            },
        }

    # ---------------------------------------------------------------- #
    # pages + static
    # ---------------------------------------------------------------- #
    @app.get("/", response_class=HTMLResponse)
    async def index() -> Any:
        page = WEB_DIR / "index.html"
        if not page.exists():
            return PlainTextResponse("JARVIS web UI is missing (gui/web/index.html)", status_code=500)
        return FileResponse(str(page), media_type="text/html")

    @app.get("/docs", response_class=HTMLResponse)
    async def docs() -> Any:
        """Setup and troubleshooting, deliberately *beside* the assistant.

        The console lives at `/`; this page is the manual, so the app never turns
        into documentation.  It shows the same facts `main.py --check` prints.
        """
        return HTMLResponse(render_docs_page(settings))

    @app.get("/health", response_class=JSONResponse)
    async def health() -> Dict[str, Any]:
        return {"ok": True, "assistant": settings.assistant_name, "started": state.started}

    @app.get("/manifest.webmanifest", response_class=JSONResponse)
    async def manifest() -> Any:
        path = WEB_DIR / "manifest.webmanifest"
        if path.exists():
            return FileResponse(str(path), media_type="application/manifest+json")
        return JSONResponse({"name": settings.assistant_name, "short_name": "JARVIS", "start_url": "/"})

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    generated = settings.paths.generated_dir
    generated.mkdir(parents=True, exist_ok=True)
    app.mount("/media/generated", StaticFiles(directory=str(generated)), name="media")
    voice_cache = settings.paths.voice_cache_dir
    voice_cache.mkdir(parents=True, exist_ok=True)
    app.mount("/media/voice", StaticFiles(directory=str(voice_cache)), name="voice")

    # ---------------------------------------------------------------- #
    # API
    # ---------------------------------------------------------------- #
    @app.get("/api/state", response_class=JSONResponse)
    async def api_state(
        request: Request,
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        require_token(request, x_jarvis_token, token)
        return snapshot()

    @app.post("/api/chat", response_class=JSONResponse)
    async def api_chat(
        request: Request,
        payload: Dict[str, Any] = Body(...),
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        require_token(request, x_jarvis_token, token)
        text = str((payload or {}).get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        reply = await asyncio.to_thread(state.jarvis.handle, text, "web")
        return {"reply": reply.as_dict(), "state": state.jarvis.events.state}

    @app.post("/api/confirm", response_class=JSONResponse)
    async def api_confirm(
        request: Request,
        payload: Dict[str, Any] = Body(default={}),
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        require_token(request, x_jarvis_token, token)
        approved = bool((payload or {}).get("approved", False))
        reply = await asyncio.to_thread(state.jarvis.confirm, approved, "web")
        return {"reply": reply.as_dict(), "state": state.jarvis.events.state}

    @app.post("/api/listen", response_class=JSONResponse)
    async def api_listen(
        request: Request,
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        require_token(request, x_jarvis_token, token)
        reply = await asyncio.to_thread(state.jarvis.listen_once, 7.0)
        return {"reply": reply.as_dict(), "state": state.jarvis.events.state}

    @app.post("/api/clear", response_class=JSONResponse)
    async def api_clear(
        request: Request,
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        require_token(request, x_jarvis_token, token)
        removed = state.jarvis.clear_history()
        return {"ok": True, "removed": removed}

    @app.post("/api/speech", response_class=JSONResponse)
    async def api_speech(
        request: Request,
        payload: Dict[str, Any] = Body(default={}),
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        """JARVIS's voice: mute/unmute, or point it at the browser or the device."""
        require_token(request, x_jarvis_token, token)
        data = payload or {}
        mode = str(data.get("mode") or "").strip().lower()
        try:
            if mode:
                if mode == "toggle":
                    state.toggle_voice()
                else:
                    state.set_voice_mode(mode)
            elif "muted" in data:
                state.jarvis.set_muted(bool(data["muted"]))
            else:
                state.toggle_voice()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "mode": state.voice_mode(),
            "routing": state.jarvis.voice_routing,
            "muted": state.voice_mode() == "off",
            "engine": (state.jarvis.speaker.engine.name if state.jarvis.speaker else "none"),
        }

    @app.post("/api/speak", response_class=JSONResponse)
    async def api_speak(
        request: Request,
        payload: Dict[str, Any] = Body(default={}),
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        """Synthesize text with the backend TTS engine and return audio for it.

        This is how the browser gets JARVIS's real voice: the audio is produced
        by the configured engine (Edge neural, Piper, ...), cached on disk, and
        served from ``/media/voice``.  Nothing is spoken by the browser itself.
        """
        require_token(request, x_jarvis_token, token)
        text = str((payload or {}).get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        if state.jarvis.speaker is None:
            raise HTTPException(status_code=503, detail="speech output is not installed")
        if not state.jarvis.speaker.available:
            raise HTTPException(
                status_code=503,
                detail=(
                    "no speech engine is installed on the machine running JARVIS "
                    "(sh scripts/install.sh --voice)"
                ),
            )
        if state.jarvis.voice_output == "off":
            raise HTTPException(status_code=409, detail="the voice is muted")
        speech = await asyncio.to_thread(state.speech_for, text)
        if not speech:
            engine = state.jarvis.speaker.status()
            raise HTTPException(
                status_code=502,
                detail=(
                    f"the {engine.get('engine')} voice could not produce audio"
                    + (f": {engine.get('last_error')}" if engine.get("last_error") else "")
                ),
            )
        return speech

    @app.post("/api/mic", response_class=JSONResponse)
    async def api_mic(
        request: Request,
        payload: Dict[str, Any] = Body(default={}),
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        """Microphone mute: stop (or resume) listening, server side."""
        require_token(request, x_jarvis_token, token)
        data = payload or {}
        if "muted" in data:
            muted = state.jarvis.set_mic_muted(bool(data["muted"]))
        else:
            muted = state.jarvis.set_mic_muted(not state.jarvis.mic_muted)
        return {"muted": muted, "mic": state.jarvis.status()["mic"]}

    @app.post("/api/live", response_class=JSONResponse)
    async def api_live(
        request: Request,
        payload: Dict[str, Any] = Body(default={}),
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        """Live Talk: hand the microphone loop to JARVIS and keep it listening."""
        require_token(request, x_jarvis_token, token)
        enabled = bool((payload or {}).get("enabled", True))
        status = await asyncio.to_thread(state.jarvis.listen_live, enabled)
        return {"requested": enabled, **status}

    @app.post("/api/stop", response_class=JSONResponse)
    async def api_stop(
        request: Request,
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        """Stop speaking immediately."""
        require_token(request, x_jarvis_token, token)
        await asyncio.to_thread(state.jarvis.interrupt)
        return {"ok": True, "state": state.jarvis.events.state}

    @app.post("/api/transcribe", response_class=JSONResponse)
    async def api_transcribe(
        request: Request,
        rate: int = Query(default=16000, ge=8000, le=48000),
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        """Recognise audio a client recorded, with the installed STT engine.

        A phone's microphone is on the other side of the network, so the audio is
        sent here as raw 16-bit PCM and transcribed by the exact engine that
        would have driven the Pi's own microphone (Google, Whisper, Vosk or
        PocketSphinx).  Nothing is guessed: a failure is reported as one.
        """
        require_token(request, x_jarvis_token, token)
        if state.jarvis.mic_muted:
            raise HTTPException(status_code=409, detail="the microphone is muted")
        pcm = await request.body()
        if not pcm:
            raise HTTPException(status_code=400, detail="no audio was sent")
        listener = state.jarvis.listener
        if listener is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "no speech engine is installed on the machine running JARVIS "
                    "(pip install -r requirements-voice.txt)"
                ),
            )
        transcript = await asyncio.to_thread(listener.transcribe_audio, pcm, rate, 2)
        return {
            "ok": bool(transcript.ok),
            "text": transcript.text,
            "error": transcript.error,
            "engine": listener.engine.name,
        }

    @app.post("/api/providers/reset", response_class=JSONResponse)
    async def api_reset_providers(
        request: Request,
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        require_token(request, x_jarvis_token, token)
        state.jarvis.reset_providers()
        return {"ok": True, "providers": state.jarvis.ai.status()}

    @app.get("/api/logs", response_class=JSONResponse)
    async def api_logs(
        request: Request,
        limit: int = 80,
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        require_token(request, x_jarvis_token, token)
        handler = ring_handler()
        records = handler.snapshot(limit) if handler else []
        return {"logs": records}

    @app.get("/api/tools", response_class=JSONResponse)
    async def api_tools(
        request: Request,
        x_jarvis_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        require_token(request, x_jarvis_token, token)
        return {
            "tools": [
                {
                    "name": spec.name,
                    "category": spec.category,
                    "description": spec.description,
                    "dangerous": spec.dangerous,
                    "offline_safe": spec.offline_safe,
                }
                for spec in _tool_list()
            ]
        }

    # ---------------------------------------------------------------- #
    # live events
    # ---------------------------------------------------------------- #
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        header_token = websocket.headers.get(TOKEN_HEADER)
        query_token = websocket.query_params.get("token")
        if settings.web.token and header_token != settings.web.token and query_token != settings.web.token:
            await websocket.close(code=4401)
            return

        await websocket.accept()
        loop = asyncio.get_running_loop()
        pending: "queue.Queue" = queue.Queue(maxsize=500)

        def on_event(event: Dict[str, Any]) -> None:
            # called from worker threads; hop back onto the event loop
            try:
                loop.call_soon_threadsafe(pending.put_nowait, event)
            except Exception:
                pass

        unsubscribe = state.jarvis.events.subscribe(on_event)
        state.attach_client()
        try:
            await websocket.send_json({"type": "hello", "state": snapshot()})
            while True:
                try:
                    # block off the event loop so a long poll never starves the API
                    event = await asyncio.to_thread(pending.get, True, 20.0)
                except queue.Empty:
                    await websocket.send_json({"type": "ping"})
                    continue
                await websocket.send_json({"type": "event", "event": event})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("websocket closed: %s", exc)
        finally:
            unsubscribe()
            state.detach_client()

    return app


def _tool_list() -> List[Any]:
    from tools.base import all_tools

    return all_tools()


# --------------------------------------------------------------------------- #
# /docs - the manual, kept out of the assistant itself
# --------------------------------------------------------------------------- #
def _check_row(label: str, value: str, tone: str = "") -> str:
    return (
        f'<div class="row"><span class="label">{label}</span>'
        f'<span class="value {tone}">{value}</span></div>'
    )


def render_docs_page(settings: Optional[Settings] = None) -> str:
    """A plain setup page: what is installed, what is missing, what to do next."""
    import html as htmllib
    import socket

    settings = settings or Settings.load()
    rows: List[str] = []

    rows.append(_check_row("python", "3.9 or newer required", "ok"))
    for name, package, purpose in (
        ("fastapi", "fastapi", "web interface"),
        ("uvicorn", "uvicorn", "web server"),
        ("speech_recognition", "speech_recognition", "microphone input"),
        ("pyaudio", "pyaudio", "microphone device access"),
        ("edge_tts", "edge_tts", "natural neural voice"),
        ("pygame", "pygame", "audio playback"),
        ("gpiozero", "gpiozero", "real GPIO hardware"),
        ("psutil", "psutil", "system metrics"),
    ):
        present = module_present(name, package)
        rows.append(_check_row(package, "installed" if present else f"missing - {purpose}", "ok" if present else "warn"))

    providers = [
        f"{item.slug}: {'ready' if item.usable else 'needs a key'}" for item in settings.ai.providers
    ]
    rows.append(_check_row("AI providers", htmllib.escape(", ".join(providers) or "none")))
    rows.append(_check_row("voice output", htmllib.escape(f"{settings.tts.engine} ({settings.tts.voice_output})")))
    stt_state = settings.stt.engine if settings.stt.enabled else f"{settings.stt.engine} (disabled)"
    rows.append(_check_row("voice input", htmllib.escape(stt_state)))
    rows.append(_check_row("web token", "set" if settings.web.token else "not set - anyone on your network can use JARVIS", "ok" if settings.web.token else "warn"))
    rows.append(_check_row("listening on", htmllib.escape(f"{settings.web.host}:{settings.web.port}")))
    rows.append(_check_row("project root", htmllib.escape(str(settings.paths.root))))

    try:  # the same probe `main.py --check` uses
        socket.create_connection(("1.1.1.1", 53), timeout=2.0).close()
        rows.append(_check_row("internet", "reachable", "ok"))
    except Exception:
        rows.append(_check_row("internet", "not reachable - offline mode", "warn"))

    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>JARVIS - setup</title>
<style>
  :root { color-scheme: dark; --cyan:#22d3ee; --line:rgba(34,211,238,.22); --text:#e8f6fb; --dim:#8fb3c4; }
  * { box-sizing: border-box; }
  body { margin:0; padding:28px 18px 60px; background:#04070d; color:var(--text);
         font:15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 20px; letter-spacing:.24em; text-transform:uppercase; margin:0 0 4px; }
  p.lede { color:var(--dim); margin:0 0 22px; }
  a { color: var(--cyan); }
  .row { display:flex; gap:14px; justify-content:space-between; padding:9px 12px; border:1px solid var(--line);
         border-radius:10px; margin-bottom:6px; background:rgba(9,22,33,.7); flex-wrap:wrap; }
  .label { color:var(--dim); font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
           letter-spacing:.06em; text-transform:uppercase; }
  .value { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; text-align:right; }
  .value.ok { color:#7ff0cd; } .value.warn { color:#ffd7a1; }
  h2 { font-size:13px; letter-spacing:.18em; text-transform:uppercase; color:var(--dim); margin:26px 0 10px; }
  ol, ul { padding-left: 20px; } li { margin-bottom: 6px; }
  code { background:rgba(34,211,238,.1); padding:1px 5px; border-radius:5px; font-size:12.5px; }
  .back { display:inline-block; margin-bottom:20px; }
</style></head><body><main>
<a class="back" href="/">&larr; back to JARVIS</a>
<h1>JARVIS setup</h1>
<p class="lede">This page is the manual. The assistant is at <a href="/">/</a>.</p>
<h2>This install</h2>
""" + "\n".join(rows) + """
<h2>Run it</h2>
<ol>
  <li><code>sh scripts/install.sh --voice</code> - core packages plus speech</li>
  <li><code>cp env.example .env</code> then add one AI provider key (Gemini or Groq are free)</li>
  <li><code>.venv/bin/python main.py --check</code> - reports what is still missing</li>
  <li><code>.venv/bin/python main.py</code> - open the printed LAN address from your phone</li>
</ol>
<h2>Voice notes</h2>
<ul>
  <li><b>JARVIS voice</b> is synthesized by the backend and played by whichever output you pick -
      <code>browser</code> or <code>device</code> - and never by the browser's own speech synthesis.</li>
  <li><b>Microphone</b> input uses the installed STT engine. The Pi's own microphone is used by
      Live Talk; a phone records and sends the audio to the same engine over <code>/api/transcribe</code>.</li>
  <li>Neither needs a key beyond what the recogniser itself needs (Google needs internet, Vosk does not).</li>
</ul>
<h2>Full documentation</h2>
<p>See <code>README.md</code> in the project, and <code>JARVIS_UPGRADE_PROMPT.md</code> for the
specification this build follows.</p>
</main></body></html>"""


__all__ = ["HAVE_FASTAPI", "ServerState", "create_app", "lan_address", "render_docs_page", "serve"]


def serve(settings: Optional[Settings] = None, open_browser: Optional[bool] = None) -> None:
    """Run the web interface with uvicorn (blocking)."""
    if not HAVE_FASTAPI:
        raise RuntimeError("FastAPI/uvicorn are not installed. Run: pip install -r requirements.txt")
    import uvicorn

    settings = settings or Settings.load()
    app = create_app(settings)
    should_open = settings.web.open_browser if open_browser is None else open_browser
    host, port = settings.web.host, settings.web.port

    if should_open:
        url = f"http://localhost:{port}/" + (f"?token={settings.web.token}" if settings.web.token else "")
        threading.Thread(target=_open_later, args=(url,), daemon=True).start()

    log.info("starting uvicorn on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


def _open_later(url: str, delay: float = 1.5) -> None:
    import time
    import webbrowser

    time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def lan_address(settings: Settings) -> str:
    """Best-effort LAN URL to print so a phone can connect."""
    import socket

    host = settings.web.host
    if host in {"0.0.0.0", "::", ""}:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(0.5)
            probe.connect(("8.8.8.8", 80))
            host = probe.getsockname()[0]
            probe.close()
        except Exception:
            host = "localhost"
    suffix = f"?token={settings.web.token}" if settings.web.token else ""
    return f"http://{host}:{settings.web.port}/{suffix}"



