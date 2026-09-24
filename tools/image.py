"""Image generation.

Replaces ``ImageGeneration.py``, which had a single hard-coded provider and
silently wrote nothing when the request failed.

* ``pollinations`` (default) is free and needs no key
* ``openai`` is used when an OpenAI key is configured
* the provider is a config switch (``IMAGE_PROVIDER``), not a rewrite

The result is always the real outcome: a path to a file that exists, or an error.
"""

from __future__ import annotations

import base64
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from ai.http import HttpError, request_bytes, request_json
from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool

log = get_logger("tools.image")

MAX_IMAGE_BYTES = 12 * 1024 * 1024


def _safe_name(prompt: str, max_words: int = 6) -> str:
    words = re.findall(r"[A-Za-z0-9]+", (prompt or "image").lower())[:max_words]
    stem = "-".join(words) or "image"
    return stem[:60]


def _output_path(ctx: Optional[ToolContext], prompt: str, extension: str = ".png") -> Path:
    if ctx is not None and getattr(ctx, "settings", None) is not None:
        folder = Path(ctx.settings.paths.generated_dir)
    else:
        folder = Path("assets") / "generated"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{_safe_name(prompt)}{extension}"


def _web_url(path: Path, ctx: Optional[ToolContext]) -> str:
    try:
        relative = path.resolve().relative_to(Path(ctx.settings.paths.generated_dir).resolve())
        return f"/media/generated/{relative.as_posix()}"
    except Exception:
        return ""


def _pollinations(prompt: str, width: int, height: int, timeout: float) -> bytes:
    encoded = urllib.parse.quote(prompt.strip()[:1500], safe="")
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&nologo=true&safe=false&seed={int(time.time()) % 100000}"
    )
    return request_bytes(
        url,
        headers={"Accept": "image/*"},
        timeout=timeout,
        retries=1,
        max_bytes=MAX_IMAGE_BYTES,
    )


def _openai_images(prompt: str, width: int, height: int, settings: Any, timeout: float) -> bytes:
    api_key = ""
    base = "https://api.openai.com/v1"
    for provider in getattr(settings, "ai", None).providers if settings else []:
        if provider.slug == "openai" and provider.api_key:
            api_key, base = provider.api_key, provider.base_url
            break
    if not api_key:
        raise RuntimeError("no OpenAI key configured")
    payload = {"model": "gpt-image-1", "prompt": prompt[:4000], "size": f"{width}x{height}", "n": 1}
    data = request_json(
        f"{base}/images/generations",
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=max(timeout, 90.0),
        retries=0,
    )
    items = (data or {}).get("data") or []
    if not items:
        raise RuntimeError("the image service returned no image")
    first = items[0]
    if first.get("b64_json"):
        return base64.b64decode(first["b64_json"])
    if first.get("url"):
        return request_bytes(first["url"], timeout=timeout, retries=1, max_bytes=MAX_IMAGE_BYTES)
    raise RuntimeError("unrecognised image response")


@tool(
    name="generate_image",
    description="Generate an image from a text prompt, save it locally and show it in the UI.",
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "what the image should show"},
            "width": {"type": "integer", "description": "pixels (default from config)"},
            "height": {"type": "integer", "description": "pixels (default from config)"},
        },
        "required": ["prompt"],
    },
    category="image",
    aliases=("make_image", "image", "draw"),
)
def generate_image(
    prompt: str,
    width: int = 0,
    height: int = 0,
    ctx: Optional[ToolContext] = None,
) -> ToolResult:
    text = (prompt or "").strip()
    if not text:
        return ToolResult.failure("what should the image show?")

    settings = getattr(ctx, "settings", None)
    configured = getattr(settings, "image", None)
    width = int(width or (configured.width if configured else 1024))
    height = int(height or (configured.height if configured else 1024))
    width = max(128, min(width, 2048))
    height = max(128, min(height, 2048))
    provider = (configured.provider if configured else "pollinations").lower()

    if ctx is not None:
        ctx.notify("tool", f"generating an image with {provider}...")

    order = [provider] + [other for other in ("pollinations", "openai") if other != provider]
    errors: list = []
    data: Optional[bytes] = None
    used = provider

    for candidate in order:
        try:
            if candidate == "openai":
                data = _openai_images(text, width, height, settings, timeout=120.0)
            else:
                data = _pollinations(text, width, height, timeout=120.0)
            used = candidate
            break
        except HttpError as exc:
            errors.append(f"{candidate}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {exc}")
        log.warning("image provider %s failed: %s", candidate, errors[-1])

    if not data:
        return ToolResult.failure(
            "image generation failed (" + "; ".join(errors[:2]) + "). "
            "Check the internet connection or set IMAGE_PROVIDER/OPENAI_API_KEY."
        )
    if len(data) < 100:
        return ToolResult.failure("the image service returned an invalid file")

    path = _output_path(ctx, text)
    try:
        path.write_bytes(data)
    except OSError as exc:
        return ToolResult.failure(f"I generated the image but could not save it: {exc}")

    size_kb = round(len(data) / 1024, 1)
    return ToolResult.success(
        f"Generated '{text}' with {used} and saved it to {path} ({size_kb} KB).",
        data={"path": str(path), "url": _web_url(path, ctx), "provider": used, "bytes": len(data)},
    )


__all__ = ["generate_image"]
