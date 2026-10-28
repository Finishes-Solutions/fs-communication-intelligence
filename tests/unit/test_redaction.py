"""Masking of credentials and sensitive numbers (fake and Azure redactors) and text
cleaning for PostgreSQL."""

import json
from types import SimpleNamespace

import httpx
import pytest

from ci_processing.azure import AzureHttp, AzureRedactor
from ci_processing.extraction import ExtractionStatus, clean_text, extract
from ci_processing.redaction import FakeRedactor, RedactionUnavailable, mask_credentials


@pytest.mark.parametrize(
    ("text", "masked", "count"),
    [
        ("password: hunter2 ok", "password: [credential] ok", 1),
        ("Your PIN is 4821.", "Your PIN is [credential].", 1),
        ("Use code 123456 to verify.", "Use code [credential] to verify.", 1),
        ("code: 4821", "code: [credential]", 1),
        ("zip code 77484", "zip code 77484", 0),
        ("api_key: sk-synthetic0000000000000000FAKE", "api_key: [credential]", 1),
        ("key AKIAABCDEFGHIJKLMNOP here", "key [credential] here", 1),
        (
            "reset at https://login.example/reset?token=abc&u=1 now",
            "reset at https://login.example/reset?[masked] now",
            1,
        ),
        ("see https://site.example/page ok", "see https://site.example/page ok", 0),
        ("Bearer eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeK", "Bearer [credential]", 1),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY----- done",
            "[credential] done",
            1,
        ),
    ],
)
def test_mask_credentials(text: str, masked: str, count: int) -> None:
    assert mask_credentials(text) == (masked, count)


def test_fake_redactor_masks_numbers_and_counts_categories_only() -> None:
    result = FakeRedactor().redact(
        ["SSN 900-00-0000", "card 4111 1111 1111 1111, routing number 021000021", "fine"]
    )
    assert result.texts == [
        "SSN [USSocialSecurityNumber]",
        "card [CreditCardNumber], routing number [USBankAccountNumber]",
        "fine",
    ]
    assert result.reason_code() == (
        "masked:CreditCardNumber=1,USBankAccountNumber=1,USSocialSecurityNumber=1"
    )
    assert FakeRedactor().redact(["nothing here"]).reason_code() is None


class Token:
    def get_token(self, *scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token="t", expires_on=10**10)  # noqa: S106


def redactor(handler: object) -> AzureRedactor:
    http = AzureHttp(
        credential=Token(),
        sleep=lambda _: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        max_retries=0,
    )
    return AzureRedactor("https://lang.example", http)


def test_azure_redactor_masks_by_offset_and_falls_back_to_text() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        docs = []
        for d in body["analysisInput"]["documents"]:
            text = d["text"]
            entities = []
            if "021000021" in text:  # correct offset
                at = text.index("021000021")
                entities.append(
                    {
                        "text": "021000021",
                        "category": "ABARoutingNumber",
                        "offset": at,
                        "length": 9,
                        "confidenceScore": 0.9,
                    }
                )
            if "4111111111111111" in text:  # wrong offset: falls back to the text
                entities.append(
                    {
                        "text": "4111111111111111",
                        "category": "CreditCardNumber",
                        "offset": 0,
                        "length": 16,
                        "confidenceScore": 0.8,
                    }
                )
            if "Pat" in text:  # routine contact details are not masked
                entities.append(
                    {
                        "text": "Pat",
                        "category": "Person",
                        "offset": text.index("Pat"),
                        "length": 3,
                        "confidenceScore": 0.99,
                    }
                )
            if "123-45-6789" in text:  # below the confidence floor
                entities.append(
                    {
                        "text": "123-45-6789",
                        "category": "USSocialSecurityNumber",
                        "offset": text.index("123-45-6789"),
                        "length": 11,
                        "confidenceScore": 0.3,
                    }
                )
            docs.append({"id": d["id"], "entities": entities})
        return httpx.Response(200, json={"results": {"documents": docs, "errors": []}})

    result = redactor(handler).redact(
        ["Hi Pat, routing 021000021", "card 4111111111111111 maybe 123-45-6789", "", "x" * 12000]
    )
    assert result.texts[0] == "Hi Pat, routing [ABARoutingNumber]"
    assert result.texts[1] == "card [CreditCardNumber] maybe 123-45-6789"
    assert result.texts[2] == "" and result.texts[3] == "x" * 12000
    assert result.counts == {"ABARoutingNumber": 1, "CreditCardNumber": 1}
    params = sent[0]["parameters"]
    assert isinstance(params, dict) and params["stringIndexType"] == "UnicodeCodePoint"
    # 2 short texts + 3 pieces of the long one; the empty text is not sent; 5 per call.
    assert [len(b["analysisInput"]["documents"]) for b in sent] == [5]  # type: ignore[index]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, json={"results": {"documents": [], "errors": [{"id": "0"}]}}),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
def test_azure_redactor_outage_is_unavailable_never_unmasked(response: httpx.Response) -> None:
    with pytest.raises(RedactionUnavailable):
        redactor(lambda request: response).redact(["routing 021000021"])


def test_text_is_cleaned_for_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    assert clean_text("a\x00b") == "ab"
    assert clean_text("bad \ud800 surrogate") == "bad ? surrogate"

    # PDF text can carry NUL and lone surrogates; extract() must hand back clean text.
    def child(*args: object, **kwargs: object) -> SimpleNamespace:
        out = json.dumps({"text": "Total\u0000 due \ud800"}).encode()
        return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    monkeypatch.setattr("ci_processing.extraction.subprocess.run", child)
    extracted = extract("a.pdf", "application/pdf", b"%PDF-1.4 x", max_bytes=1000)
    assert extracted.status is ExtractionStatus.EXTRACTED
    assert extracted.text == "Total due ?"
