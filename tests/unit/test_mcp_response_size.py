"""Tool results stay short enough for agent platforms (Copilot set aside a long
list_actionable result and could not read it); trimmed lists say how to get the rest."""

import json

from ci_retrieval.mcp_server import MAX_RESPONSE_CHARS, fit, page


def emails(n: int, prefix: str = "e") -> list[dict[str, str]]:
    return [
        {
            "communication_id": f"{prefix}{i}",
            "subject": "Lien notice " * 10,
            "summary_ai": "x" * 400,
        }
        for i in range(n)
    ]


def test_short_results_are_untouched() -> None:
    data = {"note": "n", "actions": emails(3)}
    assert fit(data) == data


def test_long_lists_are_trimmed_from_the_end_with_paging_hints() -> None:
    data = {"note": "n", "actions": emails(120, "a"), "possible": [], "risks": emails(40, "r")}
    out = fit(data)
    assert len(json.dumps(out, separators=(",", ":"))) <= MAX_RESPONSE_CHARS
    # The longest list loses most; what stays is the newest (the first) of each.
    assert out["actions"] == data["actions"][: len(out["actions"])]
    assert out["risks"] == data["risks"][: len(out["risks"])]
    trimmed = out["trimmed"]
    assert trimmed["actions"]["left_out"] == 120 - len(out["actions"])
    assert trimmed["actions"]["next_offset"] == len(out["actions"])
    assert "possible" not in trimmed and "offset" in out["trimmed_note"]


def test_paging_skips_what_was_shown() -> None:
    data = {"actions": emails(30, "a"), "risks": emails(5, "r"), "note": "n"}
    second = page(data, ("actions", "risks"), offset=10, limit=10)
    assert [e["communication_id"] for e in second["actions"]] == [f"a{i}" for i in range(10, 20)]
    assert second["risks"] == [] and second["offset"] == 10
    later = fit({**page(data | {"actions": emails(200, "a")}, ("actions",), 50, 100)})
    assert later["trimmed"]["actions"]["next_offset"] == 50 + len(later["actions"])
