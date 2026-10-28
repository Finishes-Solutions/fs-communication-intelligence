"""The bulk pre-filter's rules (ci_processing.prefilter) and which text Prompt Shields
sees when masking is on (ci_email.process._screen_text)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from ci_admin import lab
from ci_email.process import (
    CLASSIFY_ATTACHMENT_CHARS,
    CLASSIFY_BODY_CHARS,
    CLASSIFY_MAX_ATTACHMENTS,
    Backends,
    _screen_text,
)
from ci_processing.classification import ClassificationInput, FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.prefilter import allow_domains, bulk_skip_reason
from ci_processing.redaction import FakeRedactor
from ci_processing.screening import FakeScreener, ScreeningResult

FOOTER = "\n\nYou are receiving this because you subscribed. Unsubscribe <https://x.example/u>"


def skip(sender: str, subject: str = "Weekly update", body: str = "", **kw: Any) -> str | None:
    return bulk_skip_reason(
        sender=sender, subject=subject, body=body, has_real_attachment=False, **kw
    )


@pytest.mark.parametrize(
    "sender",
    [
        "no-reply@portal.example",
        "noreply@bank.example",
        "donotreply@city.example",
        "notifications@tool.example",
        "alerts@monitor.example",
        "mailer-daemon@mail.example",
    ],
)
def test_automated_senders_are_skipped(sender: str) -> None:
    assert skip(sender) == "automated_sender"


def test_mailing_list_with_unsubscribe_is_skipped() -> None:
    assert skip("news@digest.example", body="Market notes." + FOOTER) == "bulk_unsubscribe"
    assert (
        skip("news@digest.example", body="Update your email preferences here.")
        == "bulk_unsubscribe"
    )


@pytest.mark.parametrize("subject", ["RE: Weekly digest", "FW: Weekly digest", "Fwd: digest"])
def test_a_forwarded_or_replied_newsletter_is_processed(subject: str) -> None:
    body = "Can you look at item 3 before Friday?\n\n> Market notes." + FOOTER
    assert skip("pat@vendor.example", subject=subject, body=body) is None


def test_ordinary_business_mail_is_processed() -> None:
    body = "The tenant wants to opt out of the renewal option. Please call me."
    assert skip("pat@vendor.example", subject="Lease renewal", body=body) is None
    assert skip("") is None  # no sender recorded: let the classifier decide


def test_mail_with_a_real_attachment_is_processed() -> None:
    assert (
        bulk_skip_reason(
            sender="no-reply@billing.example",
            subject="Invoice 1042",
            body=FOOTER,
            has_real_attachment=True,
        )
        is None
    )


def test_allowed_domains_and_their_subdomains_are_processed() -> None:
    allowed = allow_domains(" PM-Tool.example, @sign.example ,,")
    assert allowed == frozenset({"pm-tool.example", "sign.example"})
    assert skip("no-reply@pm-tool.example", allowed_domains=allowed) is None
    assert skip("no-reply@us.pm-tool.example", allowed_domains=allowed) is None
    assert skip("no-reply@notpm-tool.example", allowed_domains=allowed) == "automated_sender"


def backends(*, masking: bool) -> Backends:
    return Backends(
        FakeScreener(check_pii=not masking),
        FakeClassifier(),
        FakeEmbedder(),
        redactor=FakeRedactor() if masking else None,
    )


def test_with_masking_prompt_shields_sees_only_what_the_classifier_reads() -> None:
    job = {"title": "TITLE"}
    body = "b" * (CLASSIFY_BODY_CHARS + 5_000)
    attachments = [{"content": "a" * (CLASSIFY_ATTACHMENT_CHARS * 3)} for _ in range(8)]
    text = _screen_text(job, body, attachments, backends(masking=True))
    assert text.count("b") == CLASSIFY_BODY_CHARS
    assert text.count("a") == CLASSIFY_ATTACHMENT_CHARS * CLASSIFY_MAX_ATTACHMENTS
    # Without masking the PII check runs too, so it must see all indexed text.
    full = _screen_text(job, body, attachments, backends(masking=False))
    assert full.count("b") == len(body) and full.count("a") == 8 * CLASSIFY_ATTACHMENT_CHARS * 3


class CountingScreener(FakeScreener):
    calls = 0

    def screen(self, text: str) -> ScreeningResult:
        CountingScreener.calls += 1
        return super().screen(text)


def item(sender: str, body: str) -> ClassificationInput:
    return ClassificationInput(
        subject="Weekly digest",
        body_text=body,
        sender=sender,
        to=("pat@finishes.example",),
        cc=(),
        sent=datetime(2026, 10, 28, tzinfo=UTC).isoformat(),
    )


def test_lab_shows_the_pre_filter_and_calls_no_service() -> None:
    screener = CountingScreener(check_pii=False)
    backends = lab.LabBackends(screener, FakeClassifier(), FakeRedactor(), prefilter=True)
    out = lab.run(backends, item("news@digest.example", "Notes." + FOOTER), [])
    assert out["prefilter"] == "bulk_unsubscribe"
    assert out["classified"] is False and out["screening"] == []
    assert out["routing"]["disposition"] == "not_indexed"
    assert "pre-filter" in out["routing"]["why"][0]
    assert CountingScreener.calls == 0
    # The footer past the classifier's cut still counts, as in processing.
    long_body = "x" * CLASSIFY_BODY_CHARS
    out = lab.run(
        backends,
        item("news@digest.example", long_body),
        [],
        full_body=long_body + FOOTER,
    )
    assert out["prefilter"] == "bulk_unsubscribe"
    # Off (BULK_PREFILTER_ENABLED=false): screened and classified as before.
    off = lab.LabBackends(screener, FakeClassifier(), FakeRedactor())
    out = lab.run(off, item("news@digest.example", "Notes." + FOOTER), [])
    assert out["prefilter"] is None and out["classified"] is True
    assert CountingScreener.calls == 1
