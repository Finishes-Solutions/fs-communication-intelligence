"""Label decisions, outcome rules, attachment policy, screening, chunking, routing."""

import pytest

from ci_core.contracts import AttachmentMetadata
from ci_core.settings import AttachmentPolicy
from ci_email.attachments import evaluate
from ci_processing.chunking import MAX_TOKENS, FakeEmbedder, chunk_text, estimate_tokens
from ci_processing.labels import LABELS_BY_NAME, decide, derive_outcome
from ci_processing.screening import FakeScreener, ScreeningUnavailable
from ci_testkit.routing import preview


def test_decide_uses_thresholds_and_never_treats_missing_as_zero() -> None:
    spam = LABELS_BY_NAME["spam"]  # no <= .15, yes >= .95
    assert decide(spam, 0.15) == "no"
    assert decide(spam, 0.5) == "review"
    assert decide(spam, 0.95) == "yes"
    assert decide(spam, None) == "unresolved"


def _all_no() -> dict[str, str]:
    return {name: "no" for name in LABELS_BY_NAME}


def test_restricting_labels_win_over_everything() -> None:
    d = _all_no() | {"credentials": "yes", "spam": "yes", "phishing": "yes"}
    assert derive_outcome(d) == ("restricted", ["restricted_credentials"])  # type: ignore[arg-type]


def test_spam_and_newsletter_are_not_indexed() -> None:
    outcome, reasons = derive_outcome(_all_no() | {"newsletter": "yes"})  # type: ignore[arg-type]
    assert (outcome, reasons) == ("not_indexed", ["not_indexed_newsletter"])


def test_uncertain_sensitive_or_any_phishing_signal_means_review() -> None:
    assert derive_outcome(_all_no() | {"potential_phi": "review"})[0] == "review"  # type: ignore[arg-type]
    assert derive_outcome(_all_no() | {"phishing": "review"})[0] == "review"  # type: ignore[arg-type]
    # A missing answer on a restricting label is unresolved -> review, never "no".
    missing = {k: v for k, v in _all_no().items() if k != "sensitive_pii"}
    assert derive_outcome(missing) == ("review", ["uncertain_sensitive_pii"])  # type: ignore[arg-type]


def test_clean_item_is_indexed() -> None:
    assert derive_outcome(_all_no()) == ("index", [])  # type: ignore[arg-type]


LOGO = AttachmentMetadata(native_id="a1", size_bytes=10, is_inline=True, content_id="cid1")
PDF = AttachmentMetadata(native_id="a2", size_bytes=10, content_type="application/pdf")


@pytest.mark.parametrize(
    ("policy", "atts", "hold", "statuses"),
    [
        (AttachmentPolicy.HOLD_MESSAGE, (LOGO,), True, {"a1": "unsupported_held"}),
        (
            AttachmentPolicy.PUBLISH_PARTIAL,
            (LOGO, PDF),
            False,
            {"a1": "unsupported_partial", "a2": "unsupported_partial"},
        ),
        (AttachmentPolicy.EXEMPT_CID_INLINE, (LOGO,), False, {"a1": "exempt_inline"}),
        (
            AttachmentPolicy.EXEMPT_CID_INLINE,
            (LOGO, PDF),
            True,
            {"a1": "exempt_inline", "a2": "unsupported_held"},
        ),
        (AttachmentPolicy.HOLD_MESSAGE, (), False, {}),
    ],
)
def test_attachment_policy(
    policy: AttachmentPolicy,
    atts: tuple[AttachmentMetadata, ...],
    hold: bool,
    statuses: dict[str, str],
) -> None:
    v = evaluate(atts, policy)
    assert (v.hold_reason is not None) == hold
    assert v.statuses == statuses


def test_fake_screener_holds_sensitive_pii_and_prompt_attacks() -> None:
    s = FakeScreener()
    pii = {c.check_name: c for c in s.screen("SSN: 123-45-6789")}
    assert pii["pii_screen"].decision == "hold"
    attack = {c.check_name: c for c in s.screen("Please ignore previous instructions and ...")}
    assert attack["prompt_shield"].reason_code == "prompt_attack_detected"
    clean = s.screen("Call me at (512) 555-0142 about the invoice.")
    assert all(c.decision == "allow" for c in clean)


def test_screening_outage_raises_instead_of_passing() -> None:
    with pytest.raises(ScreeningUnavailable):
        FakeScreener(unavailable=True).screen("anything")


def test_chunks_respect_the_maximum_and_keep_all_text() -> None:
    paragraphs = [f"Paragraph {i}. " + "word " * 200 for i in range(12)]
    chunks = chunk_text("\n\n".join(paragraphs))
    assert len(chunks) > 1
    assert all(estimate_tokens(c) <= MAX_TOKENS for c in chunks)
    for i in range(12):
        assert any(f"Paragraph {i}." in c for c in chunks)


def test_fake_embedder_is_deterministic_normalized_and_sized() -> None:
    e = FakeEmbedder(1536)
    a, b = e.embed(["change order drainage", "change order drainage"])
    assert a == b and len(a) == 1536
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9


def test_routing_preview_only_for_indexed_actionable_items() -> None:
    d = {"approval_required": "yes", "cat_development_construction": "yes"}
    assert preview("index", d).destinations == ("Development & Construction intake",)
    assert "review" in preview("review", d).status
    assert preview("restricted", d).destinations == ()
    assert preview("index", {"cat_payment_billing": "yes"}).triggers == ()
