"""Attachment extraction (separate process, no OCR) and paragraph chunking."""

import base64
import io
import json
import os
import zipfile
from pathlib import Path

import openpyxl  # type: ignore[import-untyped]
import pypdf
import pytest

from ci_processing.chunking import MAX_CHARS, TARGET_CHARS, chunk_text
from ci_processing.extraction import (
    ExtractionStatus,
    _error_class,
    child_environment,
    detect_kind,
    extract,
    is_signature_image,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "email" / "messages"
MB = 1024 * 1024


def fixture_pdf() -> bytes:
    data = json.loads((FIXTURES / "01_invoice_action_required.json").read_text())
    return base64.b64decode(data["attachments"][0]["contentBase64"])


def docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(
            "word/document.xml", f'<w:document xmlns:w="w"><w:body>{body}</w:body></w:document>'
        )
    return buf.getvalue()


def test_pdf_text_is_extracted_with_page_markers() -> None:
    result = extract("Invoice-4471.pdf", "application/pdf", fixture_pdf(), max_bytes=25 * MB)
    assert result.status is ExtractionStatus.EXTRACTED
    assert "[page 1]" in result.text and "$18,250.00" in result.text


def test_docx_and_xlsx() -> None:
    result = extract(
        "scope.docx", None, docx(["Scope &amp; schedule", "Second para"]), max_bytes=25 * MB
    )
    assert result.status is ExtractionStatus.EXTRACTED
    assert "Scope & schedule" in result.text and "Second para" in result.text
    wb = openpyxl.Workbook()
    wb.active.append(["Lot", "Amount"])
    wb.active.append(["12", 18250])
    buf = io.BytesIO()
    wb.save(buf)
    sheet = extract("budget.xlsx", None, buf.getvalue(), max_bytes=25 * MB)
    assert sheet.status is ExtractionStatus.EXTRACTED and "18250" in sheet.text


def test_type_comes_from_bytes_not_the_claimed_name() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    assert detect_kind("looks.pdf", "application/pdf", png) is None
    assert (
        extract("looks.pdf", "application/pdf", png, max_bytes=MB).status
        is ExtractionStatus.UNSUPPORTED
    )
    assert detect_kind("renamed.bin", None, fixture_pdf()) == "pdf"


def test_limits_and_failures() -> None:
    assert (
        extract("big.pdf", None, fixture_pdf(), max_bytes=10).status is ExtractionStatus.TOO_LARGE
    )
    broken = extract("broken.pdf", None, b"%PDF-1.4 not really a pdf", max_bytes=MB)
    assert broken.status in (ExtractionStatus.FAILED, ExtractionStatus.NO_TEXT)
    bad_zip = extract("bad.docx", None, b"PK\x03\x04garbage", max_bytes=MB)
    assert bad_zip.status is ExtractionStatus.FAILED
    assert (
        extract("notes.txt", "text/plain", b"   ", max_bytes=MB).status is ExtractionStatus.NO_TEXT
    )


def test_html_is_stripped() -> None:
    html = b"<html><style>x{}</style><body><p>Pay app 3 &amp; lien waiver</p></body></html>"
    result = extract("note.html", "text/html", html, max_bytes=MB)
    assert result.text == "Pay app 3 & lien waiver"


def test_chunks_respect_size_and_overlap() -> None:
    paragraphs = [f"Paragraph {i}. " + "word " * 120 for i in range(12)]
    chunks = chunk_text("\n\n".join(paragraphs))
    assert len(chunks) > 3
    assert all(len(c) <= MAX_CHARS for c in chunks)
    assert all(len(c) <= TARGET_CHARS + MAX_CHARS for c in chunks)
    joined = " ".join(chunks)
    assert all(f"Paragraph {i}." in joined for i in range(12))


def test_long_paragraph_and_empty_text() -> None:
    chunks = chunk_text("x" * 5000)
    assert all(len(c) <= MAX_CHARS for c in chunks) and sum(map(len, chunks)) >= 5000
    assert chunk_text("   \n\n  ") == []


@pytest.mark.parametrize(
    ("name", "content_type", "size", "inline", "skipped"),
    [
        ("image001.png", "image/png", 5_000, True, True),  # signature logo
        ("photo.jpg", "image/jpeg", 2_500_000, True, True),  # any inline image
        ("logo.gif", None, 40_000, False, True),  # small attached image, by extension
        ("Site photo.jpg", "image/jpeg", 2_500_000, False, False),  # kept as a file
        ("Invoice.pdf", "application/pdf", 5_000, True, False),  # never by size alone
    ],
)
def test_signature_images_and_logos_are_skipped(
    name: str, content_type: str | None, size: int, inline: bool, skipped: bool
) -> None:
    assert is_signature_image(name, content_type, size, inline) is skipped


def test_child_process_is_given_the_parser_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    # Azure Functions puts packages on sys.path, not PYTHONPATH; the child must still find them.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    folders = child_environment()["PYTHONPATH"].split(os.pathsep)
    assert Path(pypdf.__file__).parent.parent == Path(folders[0])
    assert str(Path(openpyxl.__file__).parent.parent) in folders


def test_child_errors_are_reported_by_class_only() -> None:
    trace = b"Traceback...\npypdf.errors.PdfReadError: secret invoice text\n"
    assert _error_class(trace) == "pypdf.errors.PdfReadError"
    assert _error_class(b"Weird: <html>\n") == "Weird"
    assert _error_class(b"a b c\n") == "unknown"
