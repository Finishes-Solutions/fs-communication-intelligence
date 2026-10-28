"""Property/project and legal-entity names, the canonical-name registry, and --since."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from ci_core.operator import OperatorError
from ci_core.ops import MAX_REQUEST_HOURS, hours_since
from ci_email.capture import MAX_CAPTURE_HOURS, capture_window
from ci_processing.azure import AzureHttp, AzureOpenAIClassifier, _classifier_schema
from ci_processing.classification import (
    LABELS,
    MAX_ENTITIES,
    MAX_NAME_CHARS,
    ClassificationInput,
    FakeClassifier,
    KnownName,
    clean_names,
    normalize_names,
    with_known_names,
)

KNOWN = [
    KnownName("property", "Willow Creek Retail", ("WC Retail", "Willow Creek")),
    KnownName("entity", "Capital Company LLC", ("CapCo",)),
]


def test_fake_classifier_finds_written_names() -> None:
    text = (
        "Invoice from Acme Sitework LLC for Example Ridge Lot 12; copy to Northline Paving, "
        "Inc. and Building C. Capital Company LLC approves. The Smith Family Trust signs."
    )
    result = FakeClassifier().classify(ClassificationInput("Invoice", text))
    assert result.properties == ("Example Ridge Lot 12", "Building C")
    assert result.entities == (
        "Acme Sitework LLC",
        "Northline Paving, Inc.",
        "Capital Company LLC",
        "The Smith Family Trust",
    )


def test_names_are_cleaned_deduplicated_and_capped() -> None:
    names = ["  Capital   Company LLC ", "capital company llc", 7, "", "x" * 300]
    assert clean_names(names, 8) == ("Capital Company LLC", "x" * MAX_NAME_CHARS)
    assert len(clean_names([f"Fund {i} LP" for i in range(20)], MAX_ENTITIES)) == MAX_ENTITIES


def test_known_names_map_aliases_to_canonical_and_keep_the_rest() -> None:
    assert normalize_names(["wc retail", "Lakeview Lofts"], "property", KNOWN, 5) == (
        "Willow Creek Retail",
        "Lakeview Lofts",
    )
    # Two aliases of one property collapse to one canonical name.
    assert normalize_names(["Willow Creek", "WC RETAIL"], "property", KNOWN, 5) == (
        "Willow Creek Retail",
    )
    # Kinds do not mix: an entity alias does not rename a property.
    assert normalize_names(["CapCo"], "property", KNOWN, 5) == ("CapCo",)
    result = replace(FakeClassifier().classify(ClassificationInput("s", "b")), entities=("capco",))
    mapped = with_known_names(result, KNOWN)
    assert mapped.entities == ("Capital Company LLC",)


class Token:
    def get_token(self, *scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token="t", expires_on=10**10)  # noqa: S106


def test_azure_classifier_asks_for_cleans_and_uses_known_names() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = {
            "labels": {lb.name: 0.0 for lb in LABELS},
            "summary": "s",
            "action": None,
            "action_for": [],
            "due": None,
            "risk": None,
            "properties": ["Willow Creek ", "willow creek", "Lot 7"] + [f"P{i}" for i in range(9)],
            "entities": ["CapCo", 42, "Northline Paving, Inc."],
        }
        reply = {"choices": [{"message": {"content": json.dumps(content)}}]}
        return httpx.Response(200, json=reply)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    http = AzureHttp(credential=Token(), client=client)
    classifier = AzureOpenAIClassifier("https://aoai", "gpt", "v", http)
    item = ClassificationInput("Re: WC Retail", "Body")
    plain = classifier.classify(item)
    assert plain.properties == ("Willow Creek", "Lot 7", "P0", "P1", "P2")  # deduped, max 5
    assert plain.entities == ("CapCo", "Northline Paving, Inc.")
    assert "Known names" not in sent[0]["messages"][0]["content"]  # type: ignore[index]
    classifier.classify(item, KNOWN)
    system = sent[1]["messages"][0]["content"]  # type: ignore[index]
    assert "Willow Creek Retail (also: WC Retail, Willow Creek)" in system
    schema = _classifier_schema()
    assert {"properties", "entities"} <= set(schema["required"])


NOW = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)


def test_since_is_midnight_central_rounded_up() -> None:
    # 2026-09-01 00:00 CDT = 05:00 UTC; 57 days and 1 hour before NOW.
    assert hours_since("2026-09-01", NOW) == 57 * 24 + 1
    assert hours_since("2026-10-27", NOW) == 25  # 27 Oct 05:00 UTC -> 28 Oct 06:00 UTC
    assert hours_since("2026-10-27", NOW.replace(minute=1)) == 26  # rounded up
    for bad, message in (
        ("2026-10-29", "future"),
        ("2026-07-01", "93 days"),
        ("09/01/2026", "YYYY-MM-DD"),
    ):
        with pytest.raises(OperatorError, match=message):
            hours_since(bad, NOW)
    assert MAX_REQUEST_HOURS == MAX_CAPTURE_HOURS == 2232


def test_capture_refuses_more_than_93_days_before_touching_anything() -> None:
    def no_db(role: object = None) -> object:
        raise AssertionError("must not connect")

    with pytest.raises(ValueError, match="at most 2232"):
        capture_window(no_db, None, hours=2233, include_sent=True, profile={})  # type: ignore[arg-type]
