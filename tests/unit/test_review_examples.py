"""Reviewed emails as classifier examples (migration 0023): which are picked, what
the model is sent, and that they never change the email's hash."""

import json
import time
from types import SimpleNamespace

import httpx

from ci_email.review_examples import Candidate, pick_examples
from ci_processing.azure import AzureHttp, AzureOpenAIClassifier
from ci_processing.classification import (
    LABELS,
    MAX_EXAMPLE_EXCERPT,
    ClassificationInput,
    ReviewedExample,
)


class FakeCredential:
    def get_token(self, *scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token="t", expires_on=time.time() + 3600)  # noqa: S106


def example(subject: str, domain: str, wrong: tuple[str, ...] = ()) -> ReviewedExample:
    return ReviewedExample(subject, "text", domain, ("action_required",), ("spam",), wrong)


POOL = [
    Candidate("c0", example("newest, confirmed", "a.example"), 0),
    Candidate("c1", example("correction, other domain", "b.example", ("spam",)), 1),
    Candidate("c2", example("confirmed, same domain", "v.example"), 2),
    Candidate("c3", example("correction, same domain", "v.example", ("urgent",)), 3),
    Candidate("c4", example("older correction", "c.example", ("newsletter",)), 4),
]


def test_corrections_first_then_same_domain_then_newest() -> None:
    picked = pick_examples(POOL, 4, sender="pat@v.example")
    assert [e.subject for e in picked] == [
        "correction, same domain",
        "correction, other domain",
        "older correction",
        "confirmed, same domain",
    ]
    assert pick_examples(POOL, 0, sender="pat@v.example") == ()
    # The email being classified is never its own example.
    assert "correction, same domain" not in [
        e.subject for e in pick_examples(POOL, 5, sender="pat@v.example", exclude="c3")
    ]


def test_examples_change_neither_the_hash_nor_the_reuse_key() -> None:
    plain = ClassificationInput("s", "b")
    taught = ClassificationInput("s", "b", reviewed=(example("x", "y"),))
    assert plain.sha256() == taught.sha256()


def test_prompt_is_short_and_marks_the_ai_mistakes() -> None:
    long = ReviewedExample("S" * 400, "x" * 5000, "v.example", ("urgent",), (), ("urgent",))
    shown = long.prompt()
    assert len(str(shown["excerpt"])) == MAX_EXAMPLE_EXCERPT
    assert len(str(shown["subject"])) <= 160
    assert shown["ai_was_wrong_on"] == ["urgent"] and shown["yes"] == ["urgent"]


def test_azure_classifier_sends_examples_as_data_after_the_email() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        content: dict[str, object] = {
            "labels": {lb.name: 0.0 for lb in LABELS},
            "summary": "s",
            "action": None,
            "action_for": [],
            "due": None,
            "risk": None,
            "properties": [],
            "entities": [],
            "knowledge": {"names": [], "people": [], "relations": [], "terms": []},
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )

    http = AzureHttp(
        credential=FakeCredential(),
        sleep=lambda _: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    taught = ClassificationInput("Invoice", "Please pay.", reviewed=(POOL[1].example,))
    AzureOpenAIClassifier("https://aoai", "gpt", "v", http).classify(taught)
    messages = sent["messages"]
    assert isinstance(messages, list) and len(messages) == 3
    assert json.loads(messages[1]["content"])["subject"] == "Invoice"  # the email itself
    note = json.loads(messages[2]["content"])
    assert "untrusted" in note["note"] and "not this one" in note["note"]
    assert note["reviewed_examples"][0]["ai_was_wrong_on"] == ["spam"]
    AzureOpenAIClassifier("https://aoai", "gpt", "v", http).classify(ClassificationInput("s", "b"))
    plain = sent["messages"]
    assert isinstance(plain, list) and len(plain) == 2  # nothing extra without examples
