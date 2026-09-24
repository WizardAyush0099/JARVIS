"""File tools.

The old project let the assistant read and write anywhere on the filesystem with
paths assembled by string concatenation.  This module keeps the capability but
adds the guard rails:

* every path is resolved and must live inside an allowed root (project folder,
  home directory, plus anything in ``ALLOWED_PATHS``).  ``../../etc/passwd`` is
  refused, not silently followed.
* reads are capped (a 40 MB log would otherwise wipe out a Pi's remaining RAM);
  truncation is reported honestly instead of looking like the whole file
* ``delete_file`` moves things to ``data/trash/`` instead of unlinking them, so
  a wrong "yes" is recoverable
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool

log = get_logger("tools.files")

MAX_READ_BYTES = 256 * 1024
TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".htm",
    ".csv", ".log", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".env",
    ".c", ".cpp", ".h", ".java", ".rs", ".go", ".sql", ".xml",
}


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #
def allowed_roots(ctx: Optional[ToolContext]) -> List[Path]:
    if ctx is not None and getattr(ctx, "settings", None) is not None:
        roots = [Path(p).expanduser().resolve() for p in ctx.settings.safety.allowed_paths]
        if roots:
            return roots
    return [Path.cwd().resolve()]


def default_base(ctx: Optional[ToolContext]) -> Path:
    roots = allowed_roots(ctx)
    return roots[0]


def resolve_in_sandbox(raw: str, ctx: Optional[ToolContext] = None) -> Path:
    """Resolve ``raw`` and refuse anything outside the allowed roots."""
    if raw is None or not str(raw).strip():
        raise ValueError("no path was given")
    candidate = Path(str(raw).strip().strip("\"'")).expanduser()
    if not candidate.is_absolute():
        candidate = default_base(ctx) / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError(f"invalid path: {exc}") from exc

    for root in allowed_roots(ctx):
        if resolved == root or root in resolved.parents:
            return resolved
    raise PermissionError(
        f"'{raw}' is outside the folders I'm allowed to touch "
        f"({', '.join(str(r) for r in allowed_roots(ctx))}). Add it to ALLOWED_PATHS first."
    )


def _is_texty(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(2048)
        chunk.decode("utf-8")
        return b"\x00" not in chunk
    except (OSError, UnicodeDecodeError):
        return False


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
@tool(
    name="read_text_file",
    description="Read a text file from disk.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file path"},
            "max_chars": {"type": "integer", "description": "characters to return (default 4000)"},
        },
        "required": ["path"],
    },
    category="files",
    offline_safe=True,
    aliases=("read_file", "cat_file"),
)
def read_text_file(path: str, max_chars: int = 4000, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not target.exists():
        return ToolResult.failure(f"there is no file at {target.relative_to(default_base(ctx))}")
    if target.is_dir():
        return ToolResult.failure(f"{target.name} is a folder, not a file")
    try:
        size = target.stat().st_size
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_READ_BYTES)
    except OSError as exc:
        return ToolResult.failure(f"could not read {target.name}: {exc}")

    limit = max(200, min(int(max_chars or 4000), 20000))
    truncated = False
    if len(content) > limit:
        content = content[:limit]
        truncated = True
    note = ""
    if truncated or size > MAX_READ_BYTES:
        note = f"\n\n[truncated - showing the first {len(content)} characters of {size} bytes]"
    return ToolResult.success(
        content + note,
        data={"path": str(target), "size": size, "truncated": truncated},
    )


@tool(
    name="read_json_file",
    description="Read and parse a JSON file.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    category="files",
    offline_safe=True,
)
def read_json_file(path: str, max_chars: int = 4000, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ToolResult.failure(f"there is no file at {target}")
    except ValueError as exc:
        return ToolResult.failure(f"{target.name} is not valid JSON: {exc}")
    except OSError as exc:
        return ToolResult.failure(f"could not read {target.name}: {exc}")
    pretty = json.dumps(data, indent=2, ensure_ascii=False)
    limit = max(200, min(int(max_chars or 4000), 20000))
    if len(pretty) > limit:
        pretty = pretty[:limit] + "\n[truncated]"
    return ToolResult.success(pretty, data={"path": str(target)})


@tool(
    name="read_pdf",
    description="Extract text from a PDF (needs the optional pypdf package).",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_pages": {"type": "integer", "description": "pages to read (default 5)"},
        },
        "required": ["path"],
    },
    category="files",
    offline_safe=True,
)
def read_pdf(path: str, max_pages: int = 5, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not target.exists():
        return ToolResult.failure(f"there is no file at {target}")
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            return ToolResult.failure(
                "reading PDFs needs the optional dependency: pip install pypdf"
            )
    try:
        reader = PdfReader(str(target))
        pages = min(int(max_pages or 5), len(reader.pages))
        chunks: List[str] = []
        for index in range(pages):
            try:
                chunks.append(reader.pages[index].extract_text() or "")
            except Exception as exc:  # a single bad page must not fail the read
                chunks.append(f"[page {index + 1} could not be read: {exc}]")
        text = "\n".join(chunks).strip()
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"could not read that PDF: {exc}")
    if not text:
        return ToolResult.failure("that PDF has no extractable text (it may be scanned images)")
    return ToolResult.success(
        text[:8000], data={"path": str(target), "pages_read": pages, "total_pages": len(reader.pages)}
    )


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
@tool(
    name="write_text_file",
    description="Create or append to a text file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "description": "'overwrite' (default) or 'append'"},
        },
        "required": ["path", "content"],
    },
    category="files",
    offline_safe=True,
    aliases=("create_file", "save_file", "write_file"),
)
def write_text_file(
    path: str, content: str = "", mode: str = "overwrite", ctx: Optional[ToolContext] = None
) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if target.exists() and target.is_dir():
        return ToolResult.failure(f"{target.name} is a folder")
    appending = str(mode or "").lower().startswith("app")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if appending else "w", encoding="utf-8") as handle:
            handle.write(str(content or ""))
    except OSError as exc:
        return ToolResult.failure(f"could not write {target.name}: {exc}")
    verb = "Appended to" if appending else "Wrote"
    return ToolResult.success(
        f"{verb} {target} ({len(str(content or ''))} characters).",
        data={"path": str(target), "mode": "append" if appending else "overwrite"},
    )


@tool(
    name="write_json_file",
    description="Create a JSON file from structured data.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "data": {"type": "object", "description": "object or array to serialise"},
        },
        "required": ["path", "data"],
    },
    category="files",
    offline_safe=True,
)
def write_json_file(path: str, data: Any = None, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult.failure(f"could not write {target.name}: {exc}")
    return ToolResult.success(f"Wrote {target}.", data={"path": str(target)})


@tool(
    name="make_directory",
    description="Create a folder (including parents).",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    category="files",
    offline_safe=True,
)
def make_directory(path: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ToolResult.failure(f"could not create {target}: {exc}")
    return ToolResult.success(f"Folder ready: {target}", data={"path": str(target)})


# --------------------------------------------------------------------------- #
# browsing / searching
# --------------------------------------------------------------------------- #
@tool(
    name="list_directory",
    description="List the contents of a folder.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "folder path (default: project root)"},
            "limit": {"type": "integer"},
        },
    },
    category="files",
    offline_safe=True,
    aliases=("list_files", "ls"),
)
def list_directory(path: str = ".", limit: int = 40, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path or ".", ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not target.exists():
        return ToolResult.failure(f"there is no folder at {target}")
    if not target.is_dir():
        return ToolResult.failure(f"{target.name} is a file, not a folder")
    try:
        entries = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    except OSError as exc:
        return ToolResult.failure(f"could not list {target}: {exc}")
    cap = max(1, min(int(limit or 40), 200))
    lines: List[str] = []
    for entry in entries[:cap]:
        try:
            if entry.is_dir():
                lines.append(f"  {entry.name}/")
            else:
                lines.append(f"  {entry.name} ({entry.stat().st_size} bytes)")
        except OSError:
            lines.append(f"  {entry.name}")
    more = f"\n  ... and {len(entries) - cap} more" if len(entries) > cap else ""
    body = "\n".join(lines) if lines else "  (empty)"
    return ToolResult.success(
        f"{target} contains {len(entries)} items:\n{body}{more}",
        data={"path": str(target), "count": len(entries)},
    )


@tool(
    name="search_files",
    description="Find files by name, and optionally by content, inside the allowed folders.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "name fragment or text to look for"},
            "path": {"type": "string", "description": "folder to search in (default: project root)"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    },
    category="files",
    offline_safe=True,
    aliases=("find_files", "locate_files"),
)
def search_files(
    query: str, path: str = ".", limit: int = 15, ctx: Optional[ToolContext] = None
) -> ToolResult:
    needle = (query or "").strip().lower()
    if not needle:
        return ToolResult.failure("what should I look for?")
    try:
        root = resolve_in_sandbox(path or ".", ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not root.is_dir():
        return ToolResult.failure(f"{root} is not a folder")

    cap = max(1, min(int(limit or 15), 50))
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "dist", "build"}
    name_hits: List[Path] = []
    content_hits: List[Tuple[Path, int, str]] = []
    inspected = 0

    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for filename in filenames:
            candidate = Path(current) / filename
            if needle in filename.lower():
                name_hits.append(candidate)
            if len(content_hits) < cap and _is_texty(candidate) and candidate.stat().st_size < 512 * 1024:
                inspected += 1
                if inspected > 400:
                    break
                try:
                    with open(candidate, "r", encoding="utf-8", errors="ignore") as handle:
                        for number, line in enumerate(handle, 1):
                            if needle in line.lower():
                                content_hits.append((candidate, number, line.strip()[:140]))
                                break
                except OSError:
                    continue
        if len(name_hits) >= cap and len(content_hits) >= cap:
            break

    if not name_hits and not content_hits:
        return ToolResult.success(f"I found nothing matching '{query}' in {root}.")
    lines = [f"Matches for '{query}':"]
    for hit in name_hits[:cap]:
        lines.append(f"  file: {hit}")
    for hit, number, snippet in content_hits[:cap]:
        lines.append(f"  {hit}:{number}: {snippet}")
    return ToolResult.success(
        "\n".join(lines),
        data={"names": [str(p) for p in name_hits], "content": [(str(p), n) for p, n, _ in content_hits]},
    )


@tool(
    name="file_info",
    description="Show size, type and modified time for a path.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    category="files",
    offline_safe=True,
)
def file_info(path: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not target.exists():
        return ToolResult.failure(f"{target} does not exist")
    try:
        stat = target.stat()
    except OSError as exc:
        return ToolResult.failure(f"could not stat {target}: {exc}")
    kind = "folder" if target.is_dir() else "file"
    modified = time.strftime("%d %b %Y %H:%M", time.localtime(stat.st_mtime))
    return ToolResult.success(
        f"{target} is a {kind}, {stat.st_size} bytes, last modified {modified}.",
        data={"path": str(target), "size": stat.st_size, "modified": stat.st_mtime},
    )


# --------------------------------------------------------------------------- #
# organising
# --------------------------------------------------------------------------- #
@tool(
    name="copy_file",
    description="Copy a file or folder.",
    parameters={
        "type": "object",
        "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
        "required": ["source", "destination"],
    },
    category="files",
    offline_safe=True,
)
def copy_file(source: str, destination: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        src = resolve_in_sandbox(source, ctx)
        dst = resolve_in_sandbox(destination, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not src.exists():
        return ToolResult.failure(f"there is nothing at {src}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    except OSError as exc:
        return ToolResult.failure(f"could not copy: {exc}")
    return ToolResult.success(f"Copied {src.name} to {dst}.", data={"source": str(src), "destination": str(dst)})


@tool(
    name="move_file",
    description="Move or rename a file or folder.",
    parameters={
        "type": "object",
        "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
        "required": ["source", "destination"],
    },
    category="files",
    dangerous=True,
)
def move_file(source: str, destination: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        src = resolve_in_sandbox(source, ctx)
        dst = resolve_in_sandbox(destination, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not src.exists():
        return ToolResult.failure(f"there is nothing at {src}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return ToolResult.failure(f"could not move: {exc}")
    return ToolResult.success(f"Moved {src.name} to {dst}.", data={"source": str(src), "destination": str(dst)})


@tool(
    name="delete_file",
    description="Move a file or folder to JARVIS's trash folder (recoverable).",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    category="files",
    dangerous=True,
)
def delete_file(path: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not target.exists():
        return ToolResult.failure(f"there is nothing at {target}")
    if ctx is None or getattr(ctx, "settings", None) is None:
        return ToolResult.failure("deleting needs a configured context so I can use the trash folder")
    trash = Path(ctx.settings.paths.trash_dir)
    try:
        trash.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = trash / f"{stamp}-{target.name}"
        shutil.move(str(target), str(destination))
    except OSError as exc:
        return ToolResult.failure(f"could not move to trash: {exc}")
    return ToolResult.success(
        f"Moved {target.name} to the JARVIS trash ({destination}). Say 'undelete' with that path to restore it.",
        data={"trashed": str(destination)},
    )


__all__ = ["MAX_READ_BYTES", "allowed_roots", "default_base", "resolve_in_sandbox"]
