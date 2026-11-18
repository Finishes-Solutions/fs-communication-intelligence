"""File previews for the admin console (Matthew, 2026-11-18: "preview them in the
console, not download them every time ... for every file type in our system").

``build(name, content_type, data)`` decides from the file's bytes (never only its
name or the sender's MIME claim) how the console can show it, and returns a
JSON-ready description:

- ``native``: PDF, image, audio or video the browser shows itself, from the
  original bytes, typed with ``media_type`` from this list (not the sender's).
- ``text``: text-like files (txt, md, json, xml, ics, log, eml), and HTML as
  readable text (tags removed; never rendered).
- ``table``: CSV/TSV and Excel sheets, capped.
- ``document``: a Word file's headings, paragraphs and tables.
- ``slides``: a PowerPoint file's text, slide by slide.
- ``archive``: a zip file's list of contents (nothing inside is opened).
- ``none``: everything else (CAD drawings, Outlook .msg, TIFF/HEIC images...),
  with the reason; the console then offers the extracted text, if any, and the
  download.

Office and zip files are read in a separate, time- and memory-limited Python
process, as text extraction does (ci_processing.extraction), with the same
zip-bomb limits; nothing in a file is executed.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import subprocess
import sys
import tempfile
from typing import Any

from ci_processing.extraction import _error_class, child_environment

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
MAX_TEXT_CHARS = 300_000
MAX_ROWS = 300
MAX_COLUMNS = 40
MAX_SHEETS = 10
MAX_CELL_CHARS = 500

_TEXT = {"txt", "md", "markdown", "json", "xml", "ics", "log", "eml", "yaml", "yml", "ini", "cfg"}
_TABLES = {"csv", "tsv"}
_HTML = {"html", "htm"}
_OFFICE = {"docx", "pptx", "xlsx", "xlsm"}


def _extension(name: str | None) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def _native(data: bytes, extension: str) -> tuple[str, str] | None:
    """(render, media type) for what the browser can show itself, by magic bytes."""
    head = data[:64]
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head[:2] == b"BM" and extension == "bmp":
        return "image", "image/bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio", "audio/wav"
    if head[:4] == b"OggS":
        return "audio", "audio/ogg"
    if head[:3] == b"ID3" or (head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return "audio", "audio/mpeg"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video", "video/webm"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "audio", "audio/mp4"
        return "video", "video/mp4"  # mp4 and most QuickTime (.mov) files
    if extension == "svg" and b"<svg" in data[:4096].lower() and b"\x00" not in data[:4096]:
        # Shown only as an <img>, where an SVG cannot run scripts or load anything.
        return "image", "image/svg+xml"
    return None


def _decode(data: bytes) -> str:
    for encoding in (
        ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",)
    ):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", "replace")


def _is_text(data: bytes, extension: str, content_type: str) -> bool:
    if b"\x00" in data[:4096] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        return False
    return (
        extension in _TEXT | _TABLES | _HTML
        or content_type.startswith("text/")
        or content_type in ("application/json", "application/xml", "message/rfc822")
    )


def _cell(value: str) -> str:
    value = value.replace("\x00", "")
    return value if len(value) <= MAX_CELL_CHARS else value[:MAX_CELL_CHARS] + "…"


def _csv_table(text: str, extension: str) -> dict[str, Any]:
    delimiter = (
        "\t" if extension == "tsv" or ("\t" in text[:2000] and "," not in text[:2000]) else ","
    )
    rows: list[list[str]] = []
    truncated = False
    for row in csv.reader(io.StringIO(text[:MAX_TEXT_CHARS]), delimiter=delimiter):
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        if len(row) > MAX_COLUMNS:
            truncated = True
        rows.append([_cell(c) for c in row[:MAX_COLUMNS]])
    if len(text) > MAX_TEXT_CHARS:
        truncated = True
    return {"mode": "table", "sheets": [{"name": "", "rows": rows, "truncated": truncated}]}


def _text(text: str) -> dict[str, Any]:
    clean = text.replace("\x00", "")
    return {
        "mode": "text",
        "text": clean[:MAX_TEXT_CHARS],
        "truncated": len(clean) > MAX_TEXT_CHARS,
    }


def _html_text(text: str) -> str:
    import html

    text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


# Runs in the child process: stdlib and openpyxl only. Prints one JSON object.
_WORKER = r"""
import io, json, sys, zipfile
import xml.etree.ElementTree as ET
kind, limits = sys.argv[1], json.loads(sys.argv[2])
data = sys.stdin.buffer.read()
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
except Exception:
    pass
MAX_TEXT, MAX_ROWS, MAX_COLS, MAX_SHEETS, MAX_CELL = (
    limits["text"], limits["rows"], limits["cols"], limits["sheets"], limits["cell"])

def cell(v):
    v = "" if v is None else str(v)
    return v if len(v) <= MAX_CELL else v[:MAX_CELL] + "…"

def open_zip():
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = z.infolist()
    if sum(i.file_size for i in infos) > 300 * 1024**2 or len(infos) > 5000:
        raise ValueError("zip_too_large")
    return z

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

def para_text(p):
    out = []
    for node in p.iter():
        if node.tag == W + "t" and node.text:
            out.append(node.text)
        elif node.tag in (W + "tab",):
            out.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)

result = {}
if kind == "docx":
    z = open_zip()
    root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    blocks, chars = [], 0
    for child in list(body if body is not None else []):
        if chars > MAX_TEXT or len(blocks) > 3000:
            result["truncated"] = True
            break
        if child.tag == W + "p":
            text = para_text(child)
            style = child.find(W + "pPr/" + W + "pStyle")
            name = (style.get(W + "val") or "") if style is not None else ""
            heading = name.lower().startswith(("heading", "title"))
            if text.strip():
                blocks.append({"type": "heading" if heading else "paragraph", "text": text})
                chars += len(text)
        elif child.tag == W + "tbl":
            rows = []
            for tr in child.iter(W + "tr"):
                if len(rows) >= MAX_ROWS:
                    break
                cells = [cell("\n".join(para_text(p) for p in tc.iter(W + "p")))
                         for tc in tr.iter(W + "tc")][:MAX_COLS]
                rows.append(cells)
                chars += sum(map(len, cells))
            blocks.append({"type": "table", "rows": rows})
    result.update({"mode": "document", "blocks": blocks})
elif kind == "pptx":
    z = open_zip()
    names = [n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
    names.sort(key=lambda n: int("".join(c for c in n if c.isdigit()) or 0))
    slides, chars = [], 0
    for number, n in enumerate(names[:300], start=1):
        root = ET.fromstring(z.read(n))
        lines = []
        for p in root.iter(A + "p"):
            text = "".join(t.text or "" for t in p.iter(A + "t"))
            if text.strip():
                lines.append(text)
        chars += sum(map(len, lines))
        slides.append({"number": number, "lines": lines})
        if chars > MAX_TEXT:
            result["truncated"] = True
            break
    result.update({"mode": "slides", "slides": slides})
elif kind == "xlsx":
    open_zip()  # zip-bomb check only
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets[:MAX_SHEETS]:
        rows, truncated = [], False
        for row in ws.iter_rows(values_only=True):
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            values = [cell(v) for v in row[:MAX_COLS]]
            while values and values[-1] == "":
                values.pop()
            if len(row) > MAX_COLS:
                truncated = True
            rows.append(values)
        while rows and not rows[-1]:
            rows.pop()
        sheets.append({"name": ws.title, "rows": rows, "truncated": truncated})
    result.update({"mode": "table", "sheets": sheets,
                   "more_sheets": max(0, len(wb.worksheets) - MAX_SHEETS)})
elif kind == "zip":
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = z.infolist()
    entries = [{"name": i.filename, "size": i.file_size} for i in infos[:500] if not i.is_dir()]
    result.update({"mode": "archive", "entries": entries, "truncated": len(infos) > 500})
json.dump(result, sys.stdout)
"""


def _child(kind: str, data: bytes) -> dict[str, Any] | None:
    limits = {
        "text": MAX_TEXT_CHARS,
        "rows": MAX_ROWS,
        "cols": MAX_COLUMNS,
        "sheets": MAX_SHEETS,
        "cell": MAX_CELL_CHARS,
    }
    try:
        with tempfile.TemporaryDirectory() as empty:
            completed = subprocess.run(  # noqa: S603 - fixed interpreter and code, no shell
                [sys.executable, "-P", "-s", "-c", _WORKER, kind, json.dumps(limits)],
                input=data,
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
                cwd=empty,
                env=child_environment(),
            )
    except subprocess.TimeoutExpired:
        log.warning("preview timed out kind=%s", kind)
        return None
    if completed.returncode != 0:
        log.warning("preview failed kind=%s error=%s", kind, _error_class(completed.stderr))
        return None
    result: dict[str, Any] = json.loads(completed.stdout)
    return result


def build(name: str | None, content_type: str | None, data: bytes) -> dict[str, Any]:
    """How the console can show this file (see the module docstring)."""
    extension = _extension(name)
    declared = (content_type or "").split(";")[0].strip().lower()
    native = _native(data, extension)
    if native is not None:
        render, media = native
        return {"mode": "native", "render": render, "media_type": media}
    if data.startswith(b"PK\x03\x04"):
        if extension in _OFFICE:
            result = _child("xlsx" if extension == "xlsm" else extension, data)
        else:
            result = _child("zip", data)
        if result is None:
            return {"mode": "none", "reason": "The file could not be read for a preview."}
        return result
    if _is_text(data, extension, declared):
        text = _decode(data[: MAX_TEXT_CHARS * 4])
        if extension in _TABLES or declared in ("text/csv", "text/tab-separated-values"):
            return _csv_table(text, extension)
        if extension in _HTML or declared == "text/html":
            return _text(_html_text(text))
        return _text(text)
    kind = extension.upper() if extension else (declared or "this type")
    return {"mode": "none", "reason": f"No preview for {kind} files."}
