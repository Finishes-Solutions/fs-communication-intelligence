"""Attachment text extraction (no OCR) in a separate, time-limited process.

Approved exception to CLAUDE.md L11 (Matthew, 2026-10-28): files are parsed
without malware scanning for the first real-mail run. Containment instead:
a size cap, a separate child Python process with a timeout and, on Linux, a
memory limit, zip-bomb checks for Office files and zip archives, and no
execution of anything inside a file. The child runs with ``-P -s`` from an empty temporary directory
(nothing importable from the working directory or user site). It is handed the
folders that hold pypdf and openpyxl through ``PYTHONPATH``: Azure Functions
adds its package folder to the parent's ``sys.path`` at startup rather than to
the environment, so a child would not otherwise find them. Scanned
PDFs and images yield ``no_text`` or ``unsupported`` until OCR is added.

Supported: PDF (pypdf), DOCX, PPTX, XLSX (openpyxl), plain text-like files
(txt, csv, md, json, xml, html, ics) and ZIP archives (Matthew, 2026-11-18).
Everything else is ``unsupported``.

ZIP archives are opened in memory (nothing is written to disk, so names inside
cannot reach the file system) and every file inside is read like an attachment,
including ZIPs inside ZIPs up to ``ZIP_MAX_DEPTH`` levels. The text lists each
file by its path inside the archive, and names the files it could not read. A
depth limit alone does not stop a zip bomb (one level can expand to terabytes),
so the archive tree also has a budget the worker counts as it actually
decompresses, never trusting the sizes the archive claims: ``ZIP_MAX_TOTAL_BYTES``
in all, ``ZIP_MAX_MEMBER_BYTES`` per file, ``ZIP_MAX_MEMBERS`` files, plus the
child's memory limit and ``ZIP_TIMEOUT_SECONDS``. Encrypted files inside are
listed, not read. 7z and RAR archives are ``unsupported``.

Signature images and logos are ``skipped`` without being downloaded or kept
(Matthew, 2026-10-28): every inline image, and any image attachment of 100 KB
or less. Larger attached images (site photos) are kept as files, not read.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger(__name__)

EXTRACTOR_VERSION = "extract-v2-no-ocr-zip"
MAX_TEXT_CHARS = 200_000
TIMEOUT_SECONDS = 45
# ZIP archives (see the module docstring).
ZIP_MAX_DEPTH = 10  # a ZIP inside a ZIP inside ... ten levels at most
ZIP_MAX_TOTAL_BYTES = 300 * 1024 * 1024  # decompressed, across the whole archive tree
ZIP_MAX_MEMBER_BYTES = 50 * 1024 * 1024  # decompressed, one file inside
ZIP_MAX_MEMBERS = 2000  # files, across the whole archive tree
ZIP_TIMEOUT_SECONDS = 120


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
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")  # a ZIP with files; an empty ZIP


def detect_kind(name: str | None, content_type: str | None, data: bytes) -> str | None:
    """Decide by actual bytes first; a filename or MIME claim alone is not trusted."""
    extension = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04") and extension in _OFFICE:
        return extension
    if data.startswith(_ZIP_MAGIC) and (
        extension == "zip" or "zip" in (content_type or "").lower()
    ):
        return "zip"
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
zip_depth, zip_total, zip_member, zip_members = (int(v) for v in sys.argv[3:7])
data = sys.stdin.buffer.read()
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
except Exception:
    pass

TEXT_EXT = {"txt", "csv", "md", "json", "xml", "html", "htm", "ics", "log", "tsv"}
OFFICE = {"docx", "pptx", "xlsx"}

def office_xml(blob, names_prefix, tag):
    z = zipfile.ZipFile(io.BytesIO(blob))
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

def read_file(kind, blob):
    if kind == "pdf":
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(blob))
        if reader.is_encrypted:
            raise ValueError("encrypted")
        pages = []
        for i, page in enumerate(reader.pages[:300]):
            pages.append("[page %d]\n%s" % (i + 1, page.extract_text() or ""))
            if sum(map(len, pages)) > max_chars:
                break
        text = "\n".join(pages)
        return "" if not re.sub(r"\[page \d+\]|\s", "", text) else text
    if kind == "docx":
        return office_xml(blob, "word/document", "w:t")
    if kind == "pptx":
        return office_xml(blob, "ppt/slides/slide", "a:t")
    if kind == "xlsx":
        office_xml(blob, "xl/", "x")  # zip-bomb check only
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
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
        return "\n".join(rows)
    text = blob.decode("utf-8", "replace")
    if kind == "html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text

def member_kind(name, blob):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name.rsplit("/", 1)[-1] else ""
    if blob.startswith(b"%PDF-"):
        return "pdf"
    if blob.startswith(b"PK\x03\x04") and ext in OFFICE:
        return ext
    if blob.startswith((b"PK\x03\x04", b"PK\x05\x06")) and ext == "zip":
        return "zip"
    if ext in TEXT_EXT and b"\x00" not in blob[:4096]:
        return "html" if ext in ("html", "htm") else "text"
    return None

budget = {"bytes": zip_total, "members": zip_members}
out = []  # pieces of the archive's text
size = [0]

def emit(piece):
    out.append(piece)
    size[0] += len(piece)

def note(path, why):
    emit("[file %s: not read, %s]\n" % (path, why))

def walk(blob, prefix, depth):
    z = zipfile.ZipFile(io.BytesIO(blob))
    for info in z.infolist():
        if info.is_dir():
            continue
        path = prefix + re.sub(r"[\x00-\x1f]", " ", info.filename)
        if size[0] > max_chars:
            emit("[more files not listed: text limit reached]\n")
            return False
        if budget["members"] <= 0:
            emit("[more files not read: file count limit reached]\n")
            return False
        budget["members"] -= 1
        if info.flag_bits & 0x1:
            note(path, "encrypted")
            continue
        limit = min(zip_member, budget["bytes"])
        try:
            with z.open(info) as f:
                inner = f.read(limit + 1)  # what is really there, not what the header claims
        except Exception:
            note(path, "damaged")
            continue
        if len(inner) > limit:
            if limit < zip_member:
                emit("[file %s: not read, archive size limit reached]\n" % path)
                return False
            note(path, "too large")
            continue
        budget["bytes"] -= len(inner)
        kind = member_kind(info.filename, inner)
        if kind == "zip":
            if depth >= zip_depth:
                note(path, "nested too deep")
                continue
            emit("[archive %s]\n" % path)
            try:
                if walk(inner, path + "/", depth + 1) is False:
                    return False
            except zipfile.BadZipFile:
                note(path, "damaged")
            continue
        if kind is None:
            note(path, "unsupported type")
            continue
        try:
            text = read_file(kind, inner)
        except Exception:
            note(path, "could not be read")
            continue
        text = re.sub(r"[ \t]+", " ", text).strip()
        emit("[file %s]\n%s\n" % (path, text) if text else "[file %s: no text]\n" % path)
    return True

if kind == "zip":
    walk(data, "", 1)
    text = "".join(out)
else:
    text = read_file(kind, data)
json.dump({"text": text[: max_chars + 1]}, sys.stdout)
"""


def clean_text(text: str) -> str:
    """Text PostgreSQL will store: no NUL characters, no lone surrogates (PDFs have both)."""
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def child_environment() -> dict[str, str]:
    """The parent's environment plus the folders holding the parser packages."""
    folders: list[str] = []
    for package in ("pypdf", "openpyxl"):
        spec = importlib.util.find_spec(package)
        for location in (spec.submodule_search_locations or []) if spec else []:
            folders.append(os.path.dirname(location))
    existing = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(dict.fromkeys([*folders, *existing]))}


def _error_class(stderr: bytes) -> str:
    """The exception class from a child traceback; never its message (could quote a file)."""
    lines = [ln for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    head = lines[-1].split(":", 1)[0].strip() if lines else ""
    return head if re.fullmatch(r"[A-Za-z_][\w.]{0,80}", head) else "unknown"


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
                [
                    sys.executable,
                    "-P",
                    "-s",
                    "-c",
                    _WORKER,
                    kind,
                    str(MAX_TEXT_CHARS),
                    str(ZIP_MAX_DEPTH),
                    str(ZIP_MAX_TOTAL_BYTES),
                    str(ZIP_MAX_MEMBER_BYTES),
                    str(ZIP_MAX_MEMBERS),
                ],
                input=data,
                capture_output=True,
                timeout=ZIP_TIMEOUT_SECONDS if kind == "zip" else TIMEOUT_SECONDS,
                check=False,
                cwd=empty,
                env=child_environment(),
            )
    except subprocess.TimeoutExpired:
        return Extraction(ExtractionStatus.TIMEOUT, kind=kind)
    if completed.returncode != 0:
        log.warning("extraction failed kind=%s error=%s", kind, _error_class(completed.stderr))
        return Extraction(ExtractionStatus.FAILED, kind=kind)
    text = clean_text(str(json.loads(completed.stdout)["text"]))
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return Extraction(ExtractionStatus.NO_TEXT, kind=kind)
    truncated = len(text) > MAX_TEXT_CHARS
    return Extraction(ExtractionStatus.EXTRACTED, text[:MAX_TEXT_CHARS], kind, truncated)
