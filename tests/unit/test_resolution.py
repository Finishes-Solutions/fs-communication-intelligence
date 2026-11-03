"""Action-item resolution: the fake resolver's rules, input hashing and the parsing
helpers the Azure resolver relies on. No database."""

from ci_processing.resolution import (
    MAX_REPLIES,
    FakeActionResolver,
    Reply,
    ResolutionInput,
    clean_index,
    clean_reason,
)


def thread(*bodies: str) -> ResolutionInput:
    replies = tuple(
        Reply(f"c{i}", "pat@example.test", f"2026-10-1{i}T10:00:00+00:00", "RE: CO 8", body)
        for i, body in enumerate(bodies)
    )
    return ResolutionInput("CO 8", "Please review and approve by Friday.", "approve CO 8", replies)


def test_fake_resolver_reads_the_newest_settling_reply() -> None:
    resolver = FakeActionResolver()
    assert resolver.resolve(thread()).status == "open"
    assert resolver.resolve(thread("Got it, will review tomorrow.")).status == "in_progress"
    done = resolver.resolve(thread("Will review.", "Approved, signed copy attached."))
    assert (done.status, done.resolver_index) == ("resolved", 1)
    later = resolver.resolve(thread("Approved.", "Thanks, one more question on the beam."))
    assert later.status == "resolved" and later.resolver_index == 0


def test_hash_covers_the_request_and_the_last_replies_only() -> None:
    base = thread("Will review.")
    assert base.sha256() == thread("Will review.").sha256()
    assert base.sha256() != thread("Approved.").sha256()
    many = thread(*["noise"] * (MAX_REPLIES + 3))
    fewer = ResolutionInput(many.subject, many.body_text, many.action_text, many.replies[3:])
    assert many.sha256() == fewer.sha256()  # only the newest MAX_REPLIES count


def test_cleaning_bounds_the_reason_and_the_reply_index() -> None:
    assert clean_reason("  done  \n by Pat ") == "done by Pat"
    assert clean_reason("x" * 500) is not None and len(clean_reason("x" * 500) or "") == 160
    assert clean_reason(None) is None
    assert clean_index(1, 3) == 1
    assert clean_index(-1, 3) is None and clean_index(3, 3) is None
    assert clean_index(True, 3) is None and clean_index("0", 3) is None
