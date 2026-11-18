"""Download links for agents (ci_retrieval.file_links) and email exports
(ci_retrieval.exports): signing, expiry, tampering, and the documents produced."""

import io
import zipfile
from xml.dom import minidom

from ci_retrieval import exports, file_links
from ci_retrieval.identity import READER_ALL, Principal

KEY = b"k" * 32
WHO = Principal("reader-oid", "reader@finishessolutions.example", (READER_ALL,))


def link(**changes: object) -> file_links.Link:
    values: dict[str, object] = {
        "kind": "file",
        "communication_id": "00000000-0000-0000-0000-000000000001",
        "item": "a" * 64,
        "principal": WHO,
        "expires_at": 2_000,
    } | changes
    return file_links.Link(**values)  # type: ignore[arg-type]


def test_a_signed_link_round_trips_until_it_expires() -> None:
    token = file_links.sign(link(), KEY)
    assert file_links.verify(token, KEY, now=1_999) == link()
    assert file_links.verify(token, KEY, now=2_001) is None  # expired


def test_forged_or_altered_links_are_refused() -> None:
    token = file_links.sign(link(), KEY)
    body, mac = token.split(".")
    assert file_links.verify(token, b"x" * 32, now=0) is None  # another key
    other = file_links.sign(link(item="b" * 64), KEY).split(".")[0]
    assert file_links.verify(f"{other}.{mac}", KEY, now=0) is None  # payload swapped
    assert file_links.verify(body, KEY, now=0) is None
    for junk in ("", ".", "a.b", "not-a-link", "%%%.%%%"):
        assert file_links.verify(junk, KEY, now=0) is None
    # A validly signed link of an unknown kind or format is still refused.
    assert file_links.verify(file_links.sign(link(kind="folder"), KEY), KEY, now=0) is None
    email = link(kind="email", item="exe")
    assert file_links.verify(file_links.sign(email, KEY), KEY, now=0) is None
    ok = link(kind="email", item="docx")
    assert file_links.verify(file_links.sign(ok, KEY), KEY, now=0) == ok


EMAIL = {
    "subject": "Change Order 8 & <Building D>",
    "sender": "sam@northfield.example",
    "received_at": "2026-10-12T14:00:00+00:00",
    "mailbox": "pat@finishessolutions.example",
    "participants": [
        {"role": "to", "email_normalized": "pat@finishessolutions.example", "display_name": None},
        {"role": "cc", "email_normalized": "ap@finishessolutions.example", "display_name": None},
    ],
    "summary_ai": "Change order for the beam.",
    "properties_ai": ["Building D"],
    "body": "Please approve.\x07\nThanks",
    "attachments": [{"name": "CO-008.pdf", "size_bytes": 754, "attachment_id": "abc123"}],
}


def test_markdown_has_headers_ai_reading_body_and_attachments() -> None:
    text = exports.markdown(EMAIL)
    assert text.startswith("# Change Order 8 & <Building D>\n")
    for part in (
        "**From:** sam@northfield.example",
        "**Cc:** ap@finishessolutions.example",
        "## AI reading (a proposal, not a decision)",
        "**Summary:** Change order",
        "## Message",
        "Please approve.",
        "CO-008.pdf (754 bytes; attachment_id abc123)",
    ):
        assert part in text, part


def test_docx_is_a_word_package_with_the_same_text() -> None:
    package = zipfile.ZipFile(io.BytesIO(exports.docx(EMAIL)))
    assert set(package.namelist()) == {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
    for name in package.namelist():
        minidom.parseString(package.read(name))  # noqa: S318 - our own output; well-formed XML
    document = package.read("word/document.xml").decode()
    assert "Change Order 8 &amp; &lt;Building D&gt;" in document
    assert "Please approve." in document and "\x07" not in document


def test_export_names_and_types() -> None:
    data, media, name = exports.render(EMAIL, "md")
    assert media.startswith("text/markdown") and name == "2026-10-12-Change-Order-8-Building-D.md"
    assert data.decode().startswith("# Change Order 8")
    _, media, name = exports.render(EMAIL, "docx")
    assert media == exports.DOCX_TYPE and name.endswith(".docx")
