"""The PDF writer is stdlib-only, so it must produce a file real readers accept.

The point of these tests is that the output is a *valid* PDF, not just bytes with
a ``%PDF`` header: they parse it back with ``pypdf`` and read the text out.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tools.base import ToolContext
from tools.documents import _paginate, _rows, create_pdf

pypdf = pytest.importorskip("pypdf")


@pytest.fixture
def ctx(settings):
    return ToolContext(settings=settings)


def _read(path: Path):
    return pypdf.PdfReader(io.BytesIO(path.read_bytes()))


def test_it_writes_a_pdf_a_reader_can_open(ctx):
    result = create_pdf("report.pdf", body="Hello Ayush. This is the body.", title="Report", ctx=ctx)
    assert result.ok, result.error
    target = Path(result.data["path"])
    assert target.exists()
    assert target.read_bytes().startswith(b"%PDF-1.4")
    assert target.read_bytes().rstrip().endswith(b"%%EOF")


def test_the_title_and_body_survive_the_round_trip(ctx):
    result = create_pdf(
        "report.pdf",
        title="Quarterly Report",
        body="The Raspberry Pi is at 42 degrees.",
        ctx=ctx,
    )
    assert result.ok
    text = _read(Path(result.data["path"])).pages[0].extract_text()
    assert "Quarterly Report" in text
    assert "42 degrees" in text


def test_long_documents_paginate_and_number_every_page(ctx):
    body = "\n\n".join("Section %d: %s" % (i, "word " * 40) for i in range(40))
    result = create_pdf("long.pdf", title="Long Doc", body=body, ctx=ctx)
    assert result.ok
    reader = _read(Path(result.data["path"]))
    assert len(reader.pages) > 1
    assert len(reader.pages) == result.data["pages"]
    for index, page in enumerate(reader.pages, start=1):
        assert "page %d of %d" % (index, len(reader.pages)) in (page.extract_text() or "")


def test_pdf_special_characters_do_not_break_the_file(ctx):
    result = create_pdf("tricky.pdf", body=r"Prices (up) 50% \ down \ 100%", ctx=ctx)
    assert result.ok
    text = _read(Path(result.data["path"])).pages[0].extract_text()
    assert "(up)" in text


def test_a_bare_filename_lands_with_the_generated_files(ctx):
    result = create_pdf("notes.pdf", body="hello", ctx=ctx)
    assert result.ok
    generated = Path(ctx.settings.paths.generated_dir).resolve()
    assert generated in Path(result.data["path"]).resolve().parents


def test_the_suffix_is_corrected_when_it_is_missing(ctx):
    result = create_pdf("notes", body="hello", ctx=ctx)
    assert result.ok
    assert result.data["path"].endswith(".pdf")


def test_paginate_never_emits_an_empty_first_page():
    rows = _rows("Title", "one line")
    pages = _paginate(rows)
    assert pages and pages[0]


def test_it_refuses_when_it_has_no_filename(ctx):
    result = create_pdf("", body="hello", ctx=ctx)
    assert not result.ok
