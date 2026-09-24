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

from config.settings import Settings
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
    def __init__(self, settings: Settings, jarvis: Optional[Jarvis] = None) -> None:
        self.settings = settings
        self.jarvis = jarvis or Jarvis(
            settings,
            with_tts=True,
            with_stt=bool(settings.stt.enabled),
        )
        self.started = False
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
        require_token(request, x_jarvis_token, token)
        if "muted" in (payload or {}):
            muted = state.jarvis.set_muted(bool(payload["muted"]))
        else:
            muted = state.jarvis.set_muted(not state.jarvis.speaker.muted) if state.jarvis.speaker else True
        return {"muted": muted}

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

    return app


def _tool_list() -> List[Any]:
    from tools.base import all_tools

    return all_tools()


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


__all__ = ["HAVE_FASTAPI", "ServerState", "create_app", "lan_address", "serve"]
