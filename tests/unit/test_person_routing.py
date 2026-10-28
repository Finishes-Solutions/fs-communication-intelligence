"""Step 2 of action items (per-person check), the Lab's routing mirror and calibration."""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ci_admin.calibration import MIN_SAMPLE, action_report, label_report
from ci_admin.lab import routing
from ci_processing.azure import AzureHttp, AzureOpenAIPersonRouter
from ci_processing.classification import LABELS, ClassificationInput, ClassificationResult
from ci_processing.policy import Disposition, Outcome
from ci_processing.routing import (
    NO_AT_OR_BELOW,
    YES_AT_OR_ABOVE,
    FakePersonRouter,
    PersonProfile,
    Route,
    rule_out,
)


class FakeCredential:
    def get_token(self, *scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token="test-token")  # noqa: S106


SR = PersonProfile(
    "chuck_sr",
    "Chuck Sr.",
    ("chuck.sr@finishes.example",),
    "Owner; approves draws and contracts",
    "Chuck Jr. (chuck.jr@finishes.example), operations; also called Chuck",
)


def result(**probabilities: float) -> ClassificationResult:
    base: dict[str, float | None] = {lb.name: 0.01 for lb in LABELS}
    return ClassificationResult("test", "p", "sha", base | probabilities)


ASKS = {"action_required": 0.9}


def item(
    sender: str = "vendor@builder.example",
    to: tuple[str, ...] = ("chuck.sr@finishes.example",),
    cc: tuple[str, ...] = (),
    body: str = "Chuck, please approve CO 8 by Friday.",
) -> ClassificationInput:
    return ClassificationInput("CO 8", body, sender=sender, to=to, cc=cc)


@pytest.mark.parametrize(
    ("probabilities", "email", "mailbox", "reason"),
    [
        ({}, item(), "", "no_request"),
        (ASKS | {"newsletter": 0.9}, item(), "", "fyi_only"),
        (ASKS | {"spam": 0.5}, item(), "", "fyi_only"),  # bulk not ruled out
        (ASKS, item(sender="no-reply@portal.example"), "", "automated"),
        (ASKS, item(sender="Notifications@bank.example"), "", "automated"),
        (ASKS, item(sender="chuck.sr@finishes.example"), "", "sent_by_person"),
        (ASKS, item(to=("chuck.jr@finishes.example",)), "", "not_a_recipient"),
        (ASKS, item(), "", None),
        ({"approval_required": 0.5}, item(cc=("chuck.sr@finishes.example",), to=()), "", None),
        # Bcc'd: not in to/cc, but the copy sits in his own mailbox.
        (ASKS, item(to=("someone@finishes.example",)), "chuck.sr@finishes.example", None),
    ],
)
def test_rules_before_the_model(
    probabilities: dict[str, float], email: ClassificationInput, mailbox: str, reason: str | None
) -> None:
    assert rule_out(result(**probabilities), email, SR, mailbox) == reason


@pytest.mark.parametrize(
    ("probability", "decision"),
    [(None, "unresolved"), (NO_AT_OR_BELOW, "no"), (0.5, "review"), (YES_AT_OR_ABOVE, "yes")],
)
def test_route_decisions_follow_thresholds(probability: float | None, decision: str) -> None:
    assert Route(probability, None, None, "m").decision.value == decision


def test_fake_router() -> None:
    fake = FakePersonRouter()
    assert fake.route(item(), SR).decision.value == "yes"  # sole "to" recipient
    group = item(to=("chuck.sr@finishes.example", "a@x.example"), body="Team, please review.")
    assert fake.route(group, SR).reason == "group_request"
    named = item(to=("chuck.sr@finishes.example", "a@x.example"))
    assert fake.route(named, SR).decision.value == "yes"  # greeting names him
    cc = item(to=("a@x.example",), cc=("chuck.sr@finishes.example",))
    assert fake.route(cc, SR).reason == "cc_only"


def test_azure_person_router_request_and_parsing() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        content = {"probability": 1.7, "action": "Approve CO 8", "reason": "made_up_reason"}
        reply = {"choices": [{"message": {"content": json.dumps(content)}}]}
        return httpx.Response(200, json=reply)

    http = AzureHttp(
        credential=FakeCredential(),
        sleep=lambda _: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = AzureOpenAIPersonRouter("https://aoai", "gpt", "v", http).route(item(), SR)
    assert route.probability == 1.0 and route.decision.value == "yes"  # clamped
    assert route.reason is None  # unknown reason codes are dropped
    assert route.action == "Approve CO 8"
    system = sent["messages"][0]["content"]
    assert "Chuck Sr." in system and "Chuck Jr. (chuck.jr@finishes.example)" in system
    assert "untrusted" in system
    schema = sent["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert "named_directly" in schema["schema"]["properties"]["reason"]["enum"]
    user = json.loads(sent["messages"][1]["content"])
    assert user["to"] == ["chuck.sr@finishes.example"]


INDEX = Outcome(Disposition.INDEX)


@pytest.mark.parametrize(
    ("probabilities", "kwargs", "action_item", "why"),
    [
        (ASKS, {"sender": "v@x.example", "to": ["pat@f.example"]}, True, "sent to them alone"),
        (ASKS | {"newsletter": 0.5}, {"to": ["pat@f.example"]}, False, "bulk"),
        (ASKS, {"sender": "noreply@x.example", "to": ["pat@f.example"]}, False, "automated"),
        (ASKS, {"sender": "pat@f.example", "to": ["pat@f.example"]}, False, "they sent it"),
        (ASKS, {"to": ["pat@f.example", "b@f.example"]}, False, "not sent to them alone"),
    ],
)
def test_lab_routing_mirrors_the_rules(
    probabilities: dict[str, float], kwargs: dict[str, Any], action_item: bool, why: str
) -> None:
    out = routing(
        result(**probabilities), INDEX, ["pat@f.example"], received_by=["pat@f.example"], **kwargs
    )
    assert out["action_item"] is action_item
    assert any(why in w for w in out["why"]), out["why"]


def test_label_report_metrics_and_suggestion_gate() -> None:
    rows = [
        {"label": "spam", "applies": True, "probability": 0.97, "decision": "yes"},
        {"label": "spam", "applies": False, "probability": 0.96, "decision": "yes"},
        {"label": "spam", "applies": True, "probability": 0.5, "decision": "review"},
        {"label": "spam", "applies": False, "probability": 0.01, "decision": "no"},
        {"label": "not_a_label", "applies": True, "probability": 0.9, "decision": "yes"},
    ]
    spam = next(r for r in label_report(rows) if r["label"] == "spam")
    assert (spam["judged"], spam["applies"], spam["uncertain"]) == (4, 2, 1)
    assert spam["precision"] == 0.5 and spam["recall"] == 0.5
    assert spam["suggestion"] is None and spam["needed"] == MIN_SAMPLE - 4


def test_label_report_suggests_the_best_f1_threshold_once_there_is_enough() -> None:
    # Real "applies" sit at 0.6 and above, the model's yes point is 0.8.
    rows = [
        {
            "label": "project_risk",
            "applies": p >= 0.6,
            "probability": p,
            "decision": "yes" if p >= 0.75 else "review" if p > 0.2 else "no",
        }
        for p in [i / 20 for i in range(20)]
    ]
    risk = next(r for r in label_report(rows) if r["label"] == "project_risk")
    assert risk["suggestion"]["yes_at_or_above"] == 0.6
    assert risk["suggestion"]["precision"] == 1.0 and risk["suggestion"]["recall"] == 1.0
    one_sided = [dict(r, applies=True) for r in rows]
    assert (
        next(r for r in label_report(one_sided) if r["label"] == "project_risk")["suggestion"]
        is None
    )  # fewer than 5 "doesn't apply"


def test_action_report() -> None:
    rows = [
        {"person": "chuck_sr", "applies": True, "reason_code": None},
        {"person": "chuck_sr", "applies": False, "reason_code": "fyi_only"},
        {"person": "chuck_sr", "applies": False, "reason_code": "fyi_only"},
        {"person": "chuck_sr", "applies": False, "reason_code": None},
    ]
    (sr,) = action_report(rows)
    assert (sr["judged"], sr["correct"], sr["precision"]) == (4, 1, 0.25)
    assert sr["reasons"] == {"fyi_only": 2}
