#!/usr/bin/env python3
"""Link Spotify to JARVIS, once.

JARVIS can already play, pause, skip, change the volume and tell you what is
playing with **no Spotify account at all** (that is MPRIS, in ``tools/media.py``).
This script adds the two things only Spotify's own API can answer:

* your **Liked Songs** (list them, like or unlike the track that is playing)
* starting something *specific* - "play Bohemian Rhapsody", "play my Discover
  Weekly" - on whichever device Spotify is active on

How to run it (on the Pi, or wherever JARVIS runs):

    .venv/bin/python scripts/spotify_auth.py

It prints the exact Redirect URI to paste into your Spotify app settings, opens
the consent page, catches the callback on 127.0.0.1:8888 and prints the three
values for your environment.  Re-run it any time to relink.

Headless Pi over SSH with no browser? Add ``--manual``: it prints the link, you
open it anywhere, and paste the address it redirects to back into the terminal.
"""

from __future__ import annotations

import argparse
import base64
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import PROJECT_ROOT, env, load_dotenv  # noqa: E402
from tools.spotify import SCOPES, TOKEN_URL  # noqa: E402

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PATH = "/callback"
PORT = 8888
REDIRECT_URI = f"http://{REDIRECT_HOST}:{PORT}{REDIRECT_PATH}"
WAIT_SECONDS = 300

MISSING = """
I need a Spotify app first (free, two minutes):

  1. open https://developer.spotify.com/dashboard and log in
  2. "Create app" - any name and description will do
  3. add this Redirect URI, exactly:

       {redirect}

  4. copy the Client ID, then "Show client secret" and copy that too
  5. put both in your .env (or Settings -> Environment):

       SPOTIFY_CLIENT_ID=...
       SPOTIFY_CLIENT_SECRET=...

  6. run me again:  .venv/bin/python scripts/spotify_auth.py
""".strip()


class _Callback(http.server.BaseHTTPRequestHandler):
    """Catches the one redirect Spotify sends back and shows a friendly page."""

    params: Dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - name fixed by http.server
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_error(404, "not the callback path")
            return
        _Callback.params = {
            key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items() if values
        }
        body = (
            "<!doctype html><meta charset='utf-8'><title>JARVIS</title>"
            "<body style='font-family:system-ui;background:#02060c;color:#e6f6ff;"
            "display:grid;place-items:center;height:100vh;margin:0'>"
            "<div style='text-align:center'>"
            "<h1 style='letter-spacing:.3em;color:#38d6ff'>JARVIS</h1>"
            "<p>Spotify is linked. You can close this tab and go back to the terminal.</p>"
            "</div></body>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # keep the terminal readable
        return


def _authorize_url(client_id: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "state": state,
            "show_dialog": "false",
        }
    )


def _exchange(client_id: str, client_secret: str, code: str) -> Dict[str, Any]:
    body = urllib.parse.urlencode(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    ).encode("utf-8")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_callback(url: str, state: str) -> Dict[str, str]:
    server = http.server.HTTPServer((REDIRECT_HOST, PORT), _Callback)
    server.timeout = 1.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        print("\nOpen this link if your browser did not open by itself:\n")
        print("  " + url + "\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        print(f"Waiting for the redirect on {REDIRECT_URI} (up to {WAIT_SECONDS // 60} minutes)...")
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline and not _Callback.params:
            time.sleep(0.4)
    finally:
        server.shutdown()
        server.server_close()

    params = dict(_Callback.params)
    if not params:
        raise SystemExit("Nothing came back in time. Run the script again and finish the consent page.")
    if params.get("state") != state:
        raise SystemExit("That redirect did not come from this run (state mismatch) - try again.")
    return params


def _manual_callback(url: str, state: str) -> Dict[str, str]:
    print("\nOpen this link in any browser:\n")
    print("  " + url + "\n")
    print("After you approve it, Spotify sends you to a 127.0.0.1 address that will fail to load.")
    print("That is fine - copy the whole address from the browser's address bar and paste it here.\n")
    pasted = input("Paste the redirected address: ").strip()
    parsed = urllib.parse.urlparse(pasted)
    params = {
        key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items() if values
    }
    if params.get("state") and params["state"] != state:
        raise SystemExit("That redirect did not come from this run (state mismatch) - try again.")
    return params


def _write_env(refresh_token: str) -> Optional[Path]:
    """Put SPOTIFY_REFRESH_TOKEN into .env without disturbing anything else."""
    target = PROJECT_ROOT / ".env"
    if not target.exists():
        return None
    lines = target.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("SPOTIFY_REFRESH_TOKEN"):
            lines[index] = f"SPOTIFY_REFRESH_TOKEN={refresh_token}"
            replaced = True
            break
    if not replaced:
        lines.append(f"SPOTIFY_REFRESH_TOKEN={refresh_token}")
    target.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return target


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Link Spotify to JARVIS (one time).")
    parser.add_argument("--manual", action="store_true",
                        help="no browser on this machine: paste the redirect address instead")
    parser.add_argument("--write", action="store_true",
                        help="write SPOTIFY_REFRESH_TOKEN straight into .env")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT)
    client_id = env("SPOTIFY_CLIENT_ID")
    client_secret = env("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(MISSING.format(redirect=REDIRECT_URI))
        return 1

    state = secrets.token_urlsafe(16)
    url = _authorize_url(client_id, state)
    params = _manual_callback(url, state) if args.manual else _wait_for_callback(url, state)

    if params.get("error"):
        raise SystemExit(f"Spotify refused the authorisation: {params['error']}")
    code = params.get("code")
    if not code:
        raise SystemExit("Spotify did not send an authorisation code - try again.")

    try:
        tokens = _exchange(client_id, client_secret, code)
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"Spotify rejected the code exchange ({exc.code}): {detail}")

    refresh = str(tokens.get("refresh_token") or "")
    if not refresh:
        raise SystemExit("Spotify did not return a refresh token - try again.")

    written = _write_env(refresh) if args.write else None
    print("\nLinked. Spotify now gives JARVIS your liked songs and playback control.\n")
    print("Add these to your environment (Settings -> Environment on Freebuff, or .env on the Pi):\n")
    print(f"  SPOTIFY_CLIENT_ID={client_id}")
    print("  SPOTIFY_CLIENT_SECRET=(the one you already set)")
    if written:
        print(f"  SPOTIFY_REFRESH_TOKEN=... written to {written}")
    else:
        print(f"  SPOTIFY_REFRESH_TOKEN={refresh}")
    print("\nThen restart JARVIS. Starting a track needs Spotify Premium; liked songs do not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
