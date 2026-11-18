"""File previews (ci_processing.preview): every kind of file the system holds is
shown natively, as text, a table, a document, slides or an archive listing, or
told plainly that it has no preview. Decided from the bytes, not the name."""

import io
import zipfile

import openpyxl  # type: ignore[import-untyped]

from ci_processing import preview
from ci_retrieval import exports


def test_browsers_show_pdfs_images_audio_and_video_from_their_bytes() -> None:
    cases = {
        ("plan.pdf", b"%PDF-1.7 rest"): ("pdf", "application/pdf"),
        ("site.png", b"\x89PNG\r\n\x1a\n" + b"0" * 16): ("image", "image/png"),
        ("photo.bin", b"\xff\xd8\xff\xe0" + b"0" * 16): ("image", "image/jpeg"),  # name ignored
        ("anim.gif", b"GIF89a" + b"0" * 16): ("image", "image/gif"),
        ("pic.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "): ("image", "image/webp"),
        ("call.mp3", b"ID3" + b"0" * 16): ("audio", "audio/mpeg"),
        ("memo.wav", b"RIFF\x00\x00\x00\x00WAVEfmt "): ("audio", "audio/wav"),
        ("walk.mov", b"\x00\x00\x00\x14ftypqt  " + b"0" * 8): ("video", "video/mp4"),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'): ("image", "image/svg+xml"),
    }
    for (name, data), (render, media) in cases.items():
        assert preview.build(name, "application/octet-stream", data) == {
            "mode": "native",
            "render": render,
            "media_type": media,
        }, name
    # A sender's claim alone never makes something native.
    assert preview.build("evil.pdf", "application/pdf", b"<html>")["mode"] != "native"


def test_text_html_and_tables() -> None:
    text = preview.build("notes.txt", "text/plain", "Line one\nLíne two".encode())
    assert text == {"mode": "text", "text": "Line one\nLíne two", "truncated": False}
    page = preview.build(
        "page.html",
        "text/html",
        b"<html><head><script>alert(1)</script></head><body><p>Hi &amp; bye</p></body></html>",
    )
    assert page["mode"] == "text" and page["text"] == "Hi & bye"  # tags and scripts removed
    table = preview.build("costs.csv", "text/csv", b"item,cost\nsteel,1200\n")
    assert table["sheets"][0]["rows"] == [["item", "cost"], ["steel", "1200"]]
    big = preview.build("big.csv", "text/csv", b"\n".join(b"%d,x" % i for i in range(1000)))
    assert len(big["sheets"][0]["rows"]) == preview.MAX_ROWS and big["sheets"][0]["truncated"]
    eml = preview.build(
        "fwd.eml", "message/rfc822", b"From: a@b.example\r\nSubject: Hi\r\n\r\nBody"
    )
    assert eml["mode"] == "text" and "Subject: Hi" in eml["text"]


def test_office_files_and_archives_are_read_in_a_separate_process() -> None:
    word = preview.build(
        "letter.docx", None, exports.docx({"subject": "Lease", "body": "Rent is due."})
    )
    assert word["mode"] == "document"
    assert {"type": "paragraph", "text": "Rent is due."} in word["blocks"]
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Budget"
    sheet.append(["Item", "Cost"])
    sheet.append(["Steel", 1200])
    data = io.BytesIO()
    book.save(data)
    excel = preview.build("budget.xlsx", None, data.getvalue())
    assert excel["sheets"] == [
        {"name": "Budget", "rows": [["Item", "Cost"], ["Steel", "1200"]], "truncated": False}
    ]
    slides = io.BytesIO()
    with zipfile.ZipFile(slides, "w") as z:
        z.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="p" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<a:p><a:r><a:t>Schedule update</a:t></a:r></a:p></p:sld>",
        )
    deck = preview.build("update.pptx", None, slides.getvalue())
    assert deck == {"mode": "slides", "slides": [{"number": 1, "lines": ["Schedule update"]}]}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("plans/A101.pdf", b"%PDF")
    listing = preview.build("plans.zip", None, archive.getvalue())
    assert listing["mode"] == "archive" and listing["entries"] == [
        {"name": "plans/A101.pdf", "size": 4}
    ]


def test_everything_else_says_there_is_no_preview() -> None:
    for name, data in (
        ("site.dwg", b"AC1032" + b"\x00" * 64),
        ("mail.msg", b"\xd0\xcf\x11\xe0" + b"\x00" * 64),
        ("scan.tiff", b"II*\x00" + b"\x00" * 64),
        ("noname", b"\x00\x01\x02"),
    ):
        result = preview.build(name, "application/octet-stream", data)
        assert result["mode"] == "none" and result["reason"], name
    broken = preview.build("broken.docx", None, b"PK\x03\x04 not a real zip")
    assert broken == {"mode": "none", "reason": "The file could not be read for a preview."}
