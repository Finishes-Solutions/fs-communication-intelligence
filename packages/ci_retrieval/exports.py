"""Emails as documents agents can keep: Markdown text or a Word (.docx) file.

Matthew (2026-11-18): files and emails should come to agents "as a PDF or .MD or
Docx file via MCP". An email is exported with its headers, the AI's summary
(marked as such), the full stored body text (masked as stored) and its list of
attachments. Attachments themselves are separate files (original or extracted
text). PDF is not produced here: an agent can turn the Markdown or Word file
into a PDF itself.

The Word file is built from the Office Open XML parts directly (no extra
library): one paragraph per line, headings in bold, nothing executable.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any
from xml.sax.saxutils import escape

from ci_core.db import Connection
from ci_retrieval.identity import Principal
from ci_retrieval.search import get_email

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MARKDOWN_TYPE = "text/markdown; charset=utf-8"


def email_document(
    conn: Connection, principal: Principal, communication_id: str
) -> dict[str, Any] | None:
    """The email (get_email's fields) with its full body text, if the caller may
    read it; get_email authorizes and audits the read."""
    email = get_email(conn, principal, communication_id, 500)
    if email is None:
        return None
    row = conn.execute(
        "SELECT content FROM published_texts WHERE communication_id = %s::uuid"
        " AND text_kind = 'email_body' ORDER BY ref LIMIT 1",
        (communication_id,),
    ).fetchone()
    email["body"] = row["content"] if row else ""
    conn.commit()
    return email


def _people(email: dict[str, Any], role: str) -> str:
    return ", ".join(
        p["email_normalized"] or p["display_name"] or ""
        for p in email.get("participants", [])
        if p["role"] == role
    )


def file_stem(email: dict[str, Any]) -> str:
    """A safe file name for the email: its date and subject."""
    subject = "".join(
        c if c.isalnum() or c in " -_" else " " for c in (email.get("subject") or "email")
    )
    words = "-".join(subject.split())[:60] or "email"
    return f"{str(email.get('received_at', ''))[:10]}-{words}"


def markdown(email: dict[str, Any]) -> str:
    """The email as Markdown. Email text is copied as it is (untrusted content)."""
    lines = [f"# {email.get('subject') or '(no subject)'}", ""]
    for label, value in (
        ("From", email.get("sender") or ""),
        ("To", _people(email, "to")),
        ("Cc", _people(email, "cc")),
        ("Date", email.get("received_at") or ""),
        ("Mailbox", email.get("mailbox") or ""),
    ):
        if value:
            lines.append(f"- **{label}:** {value}")
    ai = [
        (label, email.get(key))
        for label, key in (
            ("Summary", "summary_ai"),
            ("Action", "action_ai"),
            ("Due", "due_ai"),
            ("Project risk", "risk_ai"),
        )
        if email.get(key)
    ]
    if email.get("properties_ai"):
        ai.append(("Properties", ", ".join(email["properties_ai"])))
    if ai:
        lines += ["", "## AI reading (a proposal, not a decision)", ""]
        lines += [f"- **{label}:** {value}" for label, value in ai]
    lines += ["", "## Message", "", email.get("body") or "(no body text)"]
    files = email.get("attachments") or []
    if files:
        lines += ["", "## Attachments", ""]
        for a in files:
            size = a.get("size_bytes") or 0
            lines.append(
                f"- {a.get('name') or '(no name)'} ({size:,} bytes; attachment_id"
                f" {a.get('attachment_id')})"
            )
    return "\n".join(lines).rstrip() + "\n"


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels"
 ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml"
 ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
 Target="word/document.xml"/>
</Relationships>"""
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _paragraph(text: str, *, bold: bool = False, size: int | None = None) -> str:
    props = ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size else "")
    run_props = f"<w:rPr>{props}</w:rPr>" if props else ""
    # Characters XML 1.0 does not allow (control codes) are dropped.
    clean = "".join(c for c in text if c in "\t\n" or ord(c) >= 32)
    return f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{escape(clean)}</w:t></w:r></w:p>'


def docx(email: dict[str, Any]) -> bytes:
    """The email as a Word document with the same content as ``markdown``."""
    body: list[str] = []
    for line in markdown(email).splitlines():
        if line.startswith("# "):
            body.append(_paragraph(line[2:], bold=True, size=32))
        elif line.startswith("## "):
            body.append(_paragraph(line[3:], bold=True, size=26))
        else:
            body.append(_paragraph(line.replace("**", "")))
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{"".join(body)}</w:body></w:document>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)
    return out.getvalue()


def render(email: dict[str, Any], fmt: str) -> tuple[bytes, str, str]:
    """(content, media type, file name) for an export format: md or docx."""
    if fmt == "md":
        return markdown(email).encode(), MARKDOWN_TYPE, f"{file_stem(email)}.md"
    if fmt == "docx":
        return docx(email), DOCX_TYPE, f"{file_stem(email)}.docx"
    raise ValueError("format must be md or docx")
