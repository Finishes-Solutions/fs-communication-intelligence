"""Attachment text extraction (no OCR) in a separate, time-limited process.

Approved exception to CLAUDE.md L11 (Matthew, 2026-10-28): files are parsed
without malware scanning for the first real-mail run. Containment instead:
a size cap, a separate child Python process with a timeout and, on Linux, a
memory limit, zip-bomb checks for Office files, and no execution of anything
inside a file. The child runs with ``-P -s`` from an empty temporary directory
(nothing importable from the working directory or user site) but keeps
``PYTHONPATH``, which Azure Functions uses for installed packages. Scanned
PDFs and images yield ``no_text`` or ``unsupported`` until OCR is added.

Supported: PDF (pypdf), DOCX, PPTX, XLSX (openpyxl), and plain text-like files
(txt, csv, md, json, xml, html, ics). Everything else is ``unsupported``.

Signature images and logos are ``skipped`` without being downloaded or kept
(Matthew, 2026-10-28): every inline image, and any image attachment of 100 KB
or less. Larger attached images (site photos) are kept as files, not read.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum

EXTRACTOR_VERSION = "extract-v1-no-ocr"
MAX_TEXT_CHARS = 200_000
TIMEOUT_SECONDS = 45


class ExtractionStatus(StrEnum):
    EXTRACTED = "extracted"
    NO_TEXT = "no_text"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"  # signature images and logos: not downloaded, not kept


@dataclass(frozen=True)
class Extraction:
    status: ExtractionStatus
    text: str = ""
    kind: str | None = None
    truncated: bool = False


SIGNATURE_IMAGE_MAX_BYTES = 100 * 1024
_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "emz", "wmz"}


def is_signature_image(
    name: str | None, content_type: str | None, size_bytes: int, is_inline: bool
) -> bool:
    """Signature images and logos: every inline image, and small attached images."""
    extension = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    image = (content_type or "").lower().startswith("image/") or extension in _IMAGE_EXTENSIONS
    return image and (is_inline or size_bytes <= SIGNATURE_IMAGE_MAX_BYTES)


_TEXT_EXTENSIONS = {"txt", "csv", "md", "json", "xml", "html", "htm", "ics", "log", "tsv"}
_OFFICE = {"docx", "pptx", "xlsx"}


def detect_kind(name: str | None, content_type: str | None, data: bytes) -> str | None:
    """Decide by actual bytes first; a filename or MIME claim alone is not trusted."""
    extension = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04") and extension in _OFFICE:
        return extension
    if extension in _TEXT_EXTENSIONS or (content_type or "").startswith("text/"):
        sample = data[:4096]
        if b"\x00" not in sample:
            return (
                "html" if extension in {"html", "htm"} or "html" in (content_type or "") else "text"
            )
    return None


# Runs in the child process. Self-contained: only stdlib, pypdf and openpyxl.
_WORKER = r"""
import html, io, json, re, sys, zipfile
kind, max_chars = sys.argv[1], int(sys.argv[2])
data = sys.stdin.buffer.read()
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
except Exception:
    pass

def office_xml(names_prefix, tag):
    z = zipfile.ZipFile(io.BytesIO(data))
    if sum(i.file_size for i in z.infolist()) > 300 * 1024**2 or len(z.infolist()) > 5000:
        raise ValueError("zip_too_large")
    parts = sorted(n for n in z.namelist() if n.startswith(names_prefix) and n.endswith(".xml"))
    out = []
    for n in parts:
        xml = z.read(n).decode("utf-8", "replace")
        xml = re.sub(r"</w:p>|</a:p>", "\n", xml)
        found = re.findall(r"<%s(?: [^>]*)?>([^<]*)</%s>" % (tag, tag), xml)
        out.extend(html.unescape(t) for t in found)
        out.append("\n")
    return "".join(out)

text = ""
if kind == "pdf":
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("encrypted")
    pages = []
    for i, page in enumerate(reader.pages[:300]):
        pages.append("[page %d]\n%s" % (i + 1, page.extract_text() or ""))
        if sum(map(len, pages)) > max_chars:
            break
    text = "\n".join(pages)
    if not re.sub(r"\[page \d+\]|\s", "", text):
        text = ""
elif kind == "docx":
    text = office_xml("word/document", "w:t")
elif kind == "pptx":
    text = office_xml("ppt/slides/slide", "a:t")
elif kind == "xlsx":
    office_xml("xl/", "x")  # zip-bomb check only
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows, cells = [], 0
    for ws in wb.worksheets:
        rows.append("[sheet %s]" % ws.title)
        for row in ws.iter_rows(values_only=True):
            values = [str(v) for v in row if v is not None]
            cells += len(values)
            if values:
                rows.append(" | ".join(values))
            if cells > 200000:
                break
    text = "\n".join(rows)
else:
    text = data.decode("utf-8", "replace")
    if kind == "html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
json.dump({"text": text[: max_chars + 1]}, sys.stdout)
"""


def extract(
    name: str | None, content_type: str | None, data: bytes, *, max_bytes: int
) -> Extraction:
    if len(data) > max_bytes:
        return Extraction(ExtractionStatus.TOO_LARGE)
    kind = detect_kind(name, content_type, data)
    if kind is None:
        return Extraction(ExtractionStatus.UNSUPPORTED)
    try:
        with tempfile.TemporaryDirectory() as empty:
            completed = subprocess.run(  # noqa: S603 - fixed interpreter and code, no shell
                [sys.executable, "-P", "-s", "-c", _WORKER, kind, str(MAX_TEXT_CHARS)],
                input=data,
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
                cwd=empty,
            )
    except subprocess.TimeoutExpired:
        return Extraction(ExtractionStatus.TIMEOUT, kind=kind)
    if completed.returncode != 0:
        return Extraction(ExtractionStatus.FAILED, kind=kind)
    text = str(json.loads(completed.stdout)["text"])
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return Extraction(ExtractionStatus.NO_TEXT, kind=kind)
    truncated = len(text) > MAX_TEXT_CHARS
    return Extraction(ExtractionStatus.EXTRACTED, text[:MAX_TEXT_CHARS], kind, truncated)
