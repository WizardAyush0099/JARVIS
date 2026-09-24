"""JARVIS web interface (API + browser UI)."""

from server.app import HAVE_FASTAPI, create_app, lan_address, serve  # noqa: F401

__all__ = ["HAVE_FASTAPI", "create_app", "lan_address", "serve"]
