"""Documents: turn text into a real PDF, with no third-party dependency.

``reportlab`` and ``fpdf`` are not installed on a fresh Raspberry Pi, and pulling
one in just to print a page of text is a poor trade on a machine with a gigabyte
of RAM.  So this module writes PDF 1.4 by hand: the format is small and stable
enough that a correct, standards-compliant writer is a couple of hundred lines of
stdlib code, with no dependency that can fail to install.

Only Helvetica (one of the 14 standard fonts, so nothing is embedded) and plain
text are produced.  That is deliberate: it means the file opens everywhere, from
a phone PDF viewer to a printer, with no font handling at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from tools.base import ToolContext, ToolResult, tool
from tools.files import resolve_in_sandbox

#: A4 in PostScript points (72 per inch), the size every printer understands.
A4_WIDTH = 595.28
A4_HEIGHT = 841.89
MARGIN = 56.0
FONT_SIZE = 11.0
TITLE_SIZE = 16.0
LEADING = 15.0
TITLE_LEADING = 21.0
MAX_CHARS = 92
FIRST_BASELINE = A4_HEIGHT - MARGIN - 24.0
BOTTOM_LIMIT = 56.0

#: How much vertical room the first page and each later page have for body rows.
FIRST_PAGE_LINES = int((FIRST_BASELINE - BOTTOM_LIMIT) // LEADING)
NEXT_PAGE_LINES = int((A4_HEIGHT - MARGIN - 24.0 - BOTTOM_LIMIT) // LEADING)


def _escape(text: str) -> str:
    """Escape the characters that are special inside a PDF literal string."""
    return (
        (text or "")
        .replace("\\", r"\\")
        .replace("(", r"\(")
        .replace(")", r"\)")
        .replace("\r", "")
    )


def _wrap(paragraph: str, width: int = MAX_CHARS) -> List[str]:
    """Wrap one paragraph on word boundaries, never inside a word."""
    words = (paragraph or "").split()
    if not words:
        return [""]
    lines: List[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _rows(title: str, body: str) -> List[Tuple[str, str]]:
    """(text, style) pairs in reading order, with the title at the top."""
    out: List[Tuple[str, str]] = []
    for line in _wrap(title, MAX_CHARS - 8):
        out.append((line, "title"))
    if title:
        out.append(("", "body"))
    for paragraph in (body or "").replace("\r", "").split("\n"):
        for line in _wrap(paragraph):
            out.append((line, "body"))
    return out


def _paginate(rows: Sequence[Tuple[str, str]]) -> List[List[Tuple[str, str]]]:
    """Split rows into pages that fit between the top margin and the footer."""
    pages: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    remaining = FIRST_PAGE_LINES
    for row, style in rows:
        # a title line is set larger, so it claims two body rows of room
        needed = 2 if style == "title" else 1
        if current and len(current) + needed > remaining:
            pages.append(current)
            current = []
            remaining = NEXT_PAGE_LINES
        current.append((row, style))
    if current or not pages:
        pages.append(current)
    return pages


def _content(rows: Sequence[Tuple[str, str]], page_index: int, page_count: int) -> bytes:
    """One page's content stream: a title/body run plus a small footer."""
    parts: List[str] = ["BT"]
    y = FIRST_BASELINE
    current_size = None
    for text, style in rows:
        size = TITLE_SIZE if style == "title" else FONT_SIZE
        if current_size != size:
            parts.append(f"/F1 {size:g} Tf")
            current_size = size
        parts.append(f"1 0 0 1 {MARGIN:g} {y:g} Tm")
        if text:
            parts.append(f"({_escape(text)}) Tj")
        y -= TITLE_LEADING if style == "title" else LEADING
    parts.append("ET")
    footer = f"JARVIS  \u00b7  page {page_index} of {page_count}"
    parts.append("BT /F1 8 Tf 0.45 0.45 0.45 rg")
    parts.append(f"1 0 0 1 {MARGIN:g} 40 Tm ({_escape(footer)}) Tj ET")
    return "\n".join(parts).encode("latin-1", "replace")


def _assemble(pages: Sequence[Sequence[Tuple[str, str]]]) -> List[bytes]:
    """The PDF object table: catalog, page tree, font, then a stream+page pair."""
    count = len(pages)
    # Object numbering: 1 catalog, 2 pages, 3 font; then per page, a content
    # stream (4, 6, ...) and a page object (5, 7, ...).
    page_objects = [5 + 2 * index for index in range(count)]
    kids = " ".join(f"{number} 0 R" for number in page_objects)
    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("latin-1"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for index, rows in enumerate(pages):
        stream = _content(rows, index + 1, count)
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {A4_WIDTH:g} {A4_HEIGHT:g}] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {4 + 2 * index} 0 R >>"
            ).encode("latin-1")
        )
    return objects


def _serialize(objects: Sequence[bytes]) -> bytes:
    """Emit the file with a correct cross-reference table, as readers require."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    startxref = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def _default_folder(ctx: Optional[ToolContext]) -> Path:
    if ctx is not None and getattr(ctx, "settings", None) is not None:
        return Path(ctx.settings.paths.generated_dir)
    return Path("assets") / "generated"


def _web_url(path: Path, ctx: Optional[ToolContext]) -> str:
    try:
        relative = path.resolve().relative_to(Path(ctx.settings.paths.generated_dir).resolve())
        return f"/media/generated/{relative.as_posix()}"
    except Exception:
        return str(path)


@tool(
    name="create_pdf",
    description=(
        "Create a real PDF document from a title and body text, save it to disk "
        "and return the path so the user can open it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "where to save it; a bare name is kept with the generated files",
            },
            "body": {"type": "string", "description": "the text of the document"},
            "title": {"type": "string", "description": "document title (optional)"},
        },
        "required": ["path", "body"],
    },
    category="documents",
    offline_safe=True,
    aliases=("write_pdf", "make_pdf", "generate_pdf", "pdf_document"),
)
def create_pdf(
    path: str,
    body: str = "",
    title: str = "",
    ctx: Optional[ToolContext] = None,
) -> ToolResult:
    if not str(path or "").strip():
        return ToolResult.failure("I need a filename for the PDF.")

    # A bare filename is kept with generated content so the console can serve it;
    # anything with a directory is honoured through the same sandbox check the
    # other file tools use.
    raw = str(path).strip()
    if not os.path.isabs(raw) and os.path.dirname(raw) == "":
        folder = _default_folder(ctx)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult.failure(f"could not create {folder}: {exc}")
        target = folder / Path(raw).name
    else:
        try:
            target = resolve_in_sandbox(raw, ctx)
        except (ValueError, PermissionError) as exc:
            return ToolResult.failure(str(exc))

    if target.suffix.lower() != ".pdf":
        target = target.with_suffix(".pdf")

    pages = _paginate(_rows(title, body))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_serialize(_assemble(pages)))
    except OSError as exc:
        return ToolResult.failure(f"could not write {target}: {exc}")

    words = len((body or "").split())
    url = _web_url(target, ctx)
    return ToolResult.success(
        f"Wrote a {len(pages)}-page PDF to {target} ({words} words). Open it at {url}",
        data={"path": str(target), "pages": len(pages), "url": url, "bytes": target.stat().st_size},
    )


__all__ = ["create_pdf"]
