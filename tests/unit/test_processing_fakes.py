"""Fake screener/classifier/embedder and the section 7 decision rules.

The fixture expectations test the plumbing (thresholds, rule order,
restricted/review/held paths), not real classification quality.
"""

import math

import pytest

from ci_email.fixture import load_tenant
from ci_processing.classification import (
    LABELS,
    LABELS_BY_NAME,
    ClassificationInput,
    ClassificationResult,
    Decision,
    FakeClassifier,
)
from ci_processing.embedding import FakeEmbedder
from ci_processing.policy import Disposition, classification_outcome, screening_gate
from ci_processing.screening import FakeScreener

_, MESSAGES = load_tenant()
PROCESSED = [m for m in MESSAGES if m.fixture.expect.captured]


def outcome_for(subject: str, body: str, screener: FakeScreener | None = None) -> Disposition:
    gate = (screener or FakeScreener()).screen(f"{subject}\n{body}")
    stopped = screening_gate(gate)
    if stopped:
        return stopped.disposition
    return classification_outcome(
        FakeClassifier().classify(ClassificationInput(subject, body))
    ).disposition


@pytest.mark.parametrize("fixture", PROCESSED, ids=lambda m: m.name)
def test_fixture_reaches_expected_disposition(fixture) -> None:  # type: ignore[no-untyped-def]
    subject, body = fixture.message["subject"], fixture.message["body"]["content"]
    expect = fixture.fixture.expect
    screening = FakeScreener().screen(f"{subject}\n{body}")
    assert screening.flagged() == set(expect.screening)
    if not expect.screening:
        result = FakeClassifier().classify(ClassificationInput(subject, body))
        assert set(expect.labels_yes) <= result.yes_labels()
    assert outcome_for(subject, body).value == expect.disposition


def test_label_table_matches_claude_md() -> None:
    assert len(LABELS) == 21
    assert LABELS_BY_NAME["phishing"].no_at_or_below == 0.10
    assert LABELS_BY_NAME["phishing"].yes_at_or_above == 0.80
    assert all(lb.no_at_or_below < lb.yes_at_or_above for lb in LABELS)


@pytest.mark.parametrize(
    ("probability", "decision"),
    [
        (None, Decision.UNRESOLVED),
        (0.10, Decision.NO),
        (0.5, Decision.REVIEW),
        (0.75, Decision.YES),
        (0.0, Decision.NO),
    ],
)
def test_threshold_bands(probability: float | None, decision: Decision) -> None:
    assert LABELS_BY_NAME["sensitive_pii"].decide(probability) is decision


def _result(**probabilities: float | None) -> ClassificationResult:
    base: dict[str, float | None] = {lb.name: 0.0 for lb in LABELS}
    base.update(probabilities)
    return ClassificationResult("test", "p", "h", base)


def test_missing_answer_is_unresolved_not_zero() -> None:
    probabilities: dict[str, float | None] = {lb.name: 0.0 for lb in LABELS}
    del probabilities["credentials"]
    result = ClassificationResult("test", "p", "h", probabilities)
    assert result.decisions()["credentials"] is Decision.UNRESOLVED
    assert classification_outcome(result).disposition is Disposition.REVIEW


@pytest.mark.parametrize(
    ("probabilities", "disposition"),
    [
        ({"potential_phi": 0.9}, Disposition.RESTRICTED),
        ({"sensitive_pii": 0.5}, Disposition.REVIEW),
        ({"phishing": 0.5}, Disposition.REVIEW),
        ({"phishing": 0.9, "spam": 0.99}, Disposition.REVIEW),
        ({"credentials": 0.9, "phishing": 0.9}, Disposition.RESTRICTED),
        ({"newsletter": 0.9}, Disposition.NOT_INDEXED),
        ({"spam": 0.9}, Disposition.INDEX),  # below the .95 yes threshold
        ({}, Disposition.INDEX),
    ],
)
def test_rule_order(probabilities: dict[str, float], disposition: Disposition) -> None:
    assert classification_outcome(_result(**probabilities)).disposition is disposition


def test_screening_dependency_failure_holds_never_passes() -> None:
    clean = "Lunch order for Friday"
    assert outcome_for("s", clean, FakeScreener(unavailable={"pii"})) is Disposition.HELD
    gate = screening_gate(FakeScreener(unavailable={"prompt_shield"}).screen(clean))
    assert gate is not None and gate.reason_codes == ("screening_pending",)


def test_restricted_and_held_are_never_embedded() -> None:
    assert not Disposition.RESTRICTED.embedded
    assert not Disposition.HELD.embedded
    assert not Disposition.NOT_INDEXED.embedded
    assert Disposition.REVIEW.embedded


def test_fake_embedder_is_deterministic_normalized_and_lexical() -> None:
    embedder = FakeEmbedder(1536)
    a, b, c, empty = embedder.embed(
        ["Change order 7 framing", "change order 8 framing", "lunch order friday", ""]
    )
    assert len(a) == 1536
    assert embedder.embed(["Change order 7 framing"])[0] == a
    for v in (a, b, c, empty):
        assert math.isclose(math.fsum(x * x for x in v), 1.0)

    def cosine(x: list[float], y: list[float]) -> float:
        return math.fsum(p * q for p, q in zip(x, y, strict=True))

    assert cosine(a, b) > cosine(a, c)
