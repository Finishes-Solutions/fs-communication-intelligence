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


# ----- ZIP archives (Matthew, 2026-11-18) ------------------------------------


def zipped(files: dict[str, bytes], *, encrypted: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    raw = bytearray(buf.getvalue())
    for name in encrypted:  # set the "encrypted" flag in both headers of that entry
        for signature, flag_at, name_at in ((b"PK\x03\x04", 6, 30), (b"PK\x01\x02", 8, 46)):
            at = raw.find(signature)
            while at != -1:
                if raw[at + name_at : at + name_at + len(name)] == name.encode():
                    raw[at + flag_at] |= 0x1
                at = raw.find(signature, at + 4)
    return bytes(raw)


def test_zip_kind_comes_from_bytes_and_the_name() -> None:
    archive = zipped({"a.txt": b"hello"})
    assert detect_kind("plans.zip", None, archive) == "zip"
    assert detect_kind("plans", "application/x-zip-compressed", archive) == "zip"
    assert detect_kind("tool.jar", "application/java-archive", archive) is None
    assert detect_kind("fake.zip", "application/zip", b"not a zip") is None


def test_zip_files_are_read_and_unreadable_ones_named() -> None:
    archive = zipped(
        {
            "Invoices/Invoice-4471.pdf": fixture_pdf(),
            "scope.docx": docx(["Change order 7 for Lot 4"]),
            "notes.txt": b"Draw request due Friday",
            "Drawings/site.dwg": b"\x00\x01binary",
            "secret.txt": b"locked",
            "Empty/": b"",
        },
        encrypted=("secret.txt",),
    )
    result = extract("Package.zip", "application/zip", archive, max_bytes=50 * MB)
    assert result.status is ExtractionStatus.EXTRACTED and result.kind == "zip"
    text = result.text
    assert "[file Invoices/Invoice-4471.pdf]" in text and "$18,250.00" in text
    assert "[file scope.docx]" in text and "Change order 7 for Lot 4" in text
    assert "[file notes.txt]" in text and "Draw request due Friday" in text
    assert "[file Drawings/site.dwg: not read, unsupported type]" in text
    assert "[file secret.txt: not read, encrypted]" in text and "locked" not in text


def test_nested_zips_are_read_ten_levels_deep_and_no_further() -> None:
    def nest(levels: int) -> bytes:
        archive = zipped({"deepest.txt": f"level {levels} text".encode()})
        for level in range(levels - 1, 0, -1):
            archive = zipped({f"level{level + 1}.zip": archive, f"note{level}.txt": b"x"})
        return archive

    ten = extract("ten.zip", None, nest(10), max_bytes=50 * MB)
    assert ten.status is ExtractionStatus.EXTRACTED and "level 10 text" in ten.text
    assert "[archive level2.zip]" in ten.text
    eleven = extract("eleven.zip", None, nest(11), max_bytes=50 * MB)
    assert "level 11 text" not in eleven.text
    assert "level11.zip: not read, nested too deep]" in eleven.text
    assert "[file note1.txt]" in eleven.text  # everything above the limit is still read


def test_zip_bomb_member_is_stopped_by_bytes_actually_read() -> None:
    # 60 MB of zeros compresses to about 60 KB: over the per-file limit when decompressed.
    archive = zipped({"bomb.txt": b"\x00" * (60 * MB), "ok.txt": b"still read"})
    assert len(archive) < MB
    result = extract("bomb.zip", None, archive, max_bytes=50 * MB)
    assert "[file bomb.txt: not read, too large]" in result.text
    assert "still read" in result.text


def test_zip_total_budget_and_file_count_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from ci_processing import extraction

    monkeypatch.setattr(extraction, "ZIP_MAX_TOTAL_BYTES", 1000)
    monkeypatch.setattr(extraction, "ZIP_MAX_MEMBER_BYTES", 600)
    files = {f"f{i}.txt": f"file {i} ".encode() * 40 for i in range(5)}  # about 300 bytes each
    result = extract("many.zip", None, zipped(files), max_bytes=MB)
    assert "file 0" in result.text and "file 2" in result.text
    assert "archive size limit reached" in result.text and "file 4 " not in result.text

    monkeypatch.setattr(extraction, "ZIP_MAX_TOTAL_BYTES", 10 * MB)
    monkeypatch.setattr(extraction, "ZIP_MAX_MEMBERS", 3)
    counted = extract("many.zip", None, zipped(files), max_bytes=MB)
    assert "file 2" in counted.text and "file 3 " not in counted.text
    assert "file count limit reached" in counted.text


def test_empty_and_damaged_zips() -> None:
    empty = io.BytesIO()
    zipfile.ZipFile(empty, "w").close()
    assert extract("empty.zip", None, empty.getvalue(), max_bytes=MB).status is (
        ExtractionStatus.NO_TEXT
    )
    damaged = extract("damaged.zip", None, b"PK\x03\x04garbage", max_bytes=MB)
    assert damaged.status is ExtractionStatus.FAILED
