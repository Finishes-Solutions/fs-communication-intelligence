"""Source-neutral processing of one version: screen, classify, decide, chunk, embed, publish.

Every decision is written to ``policy_decisions`` (and classification to
``classification_runs``/``classification_labels``) as content-free codes, so
the outcome of any item can be explained from the database alone.

Version states (docs/DATABASE.md section 4):
normalized -> screened -> embedded -> published, or held / rejected with a
reason code. Publication is one transaction (the publication fence). When a
newer version of an item is held or rejected, the prior active version stops
being served, so the latest decision under the current policy always wins.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from ci_core.db import Connection, one, one_required, transaction, vector_literal
from ci_processing.chunking import Embedder, chunk_text
from ci_processing.classifier import Classifier, ClassifierInput
from ci_processing.labels import LABELS, POLICY_VERSION, decide, derive_outcome
from ci_processing.screening import Screener, ScreeningUnavailable

TERMINAL = ("published", "held", "rejected", "superseded", "withdrawn")
FinalState = Literal["published", "held", "rejected"]


class RetryableProcessingError(Exception):
    """The job should retry later; nothing was decided."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourceHold:
    """A source-specific decision taken before content leaves the source adapter."""

    check_name: str
    decision: Literal["allow", "hold"]
    reason_code: str | None
    policy_version: str


@dataclass(frozen=True)
class ProcessingInput:
    version_id: str
    communication_id: str
    text: str  # untrusted content to screen, classify and index
    classifier_input: ClassifierInput
    embedding_header: str  # context for the embedding only, never shown
    chunk_kind: str = "email_body"
    source_checks: tuple[SourceHold, ...] = ()


@dataclass(frozen=True)
class ProcessingDeps:
    screener: Screener
    classifier: Classifier
    embedder: Embedder


def _decision(
    conn: Connection,
    version_id: str,
    check_name: str,
    decision: str,
    reason_code: str | None,
    policy_version: str,
    service_ref: str | None,
) -> None:
    conn.execute(
        "INSERT INTO policy_decisions (version_id, check_name, decision, reason_code,"
        " policy_version, service_ref) VALUES (%s, %s, %s, %s, %s, %s)",
        (version_id, check_name, decision, reason_code, policy_version, service_ref),
    )


def _set_state(conn: Connection, version_id: str, state: str) -> None:
    conn.execute(
        "UPDATE communication_versions SET processing_state = %s WHERE id = %s",
        (state, version_id),
    )


def _finish_unpublished(
    conn: Connection, item: ProcessingInput, state: Literal["held", "rejected"], reason_code: str
) -> FinalState:
    conn.execute(
        "UPDATE communication_versions SET processing_state = %s, hold_reason_code = %s"
        " WHERE id = %s",
        (state, reason_code, item.version_id),
    )
    conn.execute(
        "UPDATE communication_versions SET processing_state = 'superseded'"
        " WHERE id = (SELECT active_version_id FROM communications WHERE id = %s)"
        " AND id <> %s",
        (item.communication_id, item.version_id),
    )
    conn.execute(
        "UPDATE communications SET active_version_id = NULL WHERE id = %s",
        (item.communication_id,),
    )
    return state


def _publish(conn: Connection, item: ProcessingInput, expected_dataset: str) -> FinalState:
    with transaction(conn, "ci_ingest"):
        mode = one(conn, "SELECT active_dataset() AS dataset")
        if mode is None or mode["dataset"] != expected_dataset:
            # Paused or switched since admission: publish nothing, retry later.
            raise RetryableProcessingError("mode_changed_before_publish")
        conn.execute(
            "UPDATE communication_versions SET processing_state = 'superseded'"
            " WHERE id = (SELECT active_version_id FROM communications WHERE id = %s)"
            " AND id <> %s",
            (item.communication_id, item.version_id),
        )
        conn.execute(
            "UPDATE communication_versions SET processing_state = 'published', published_at = now()"
            " WHERE id = %s",
            (item.version_id,),
        )
        conn.execute(
            "UPDATE communications SET active_version_id = %s WHERE id = %s",
            (item.version_id, item.communication_id),
        )
    return "published"


def process(
    conn: Connection, item: ProcessingInput, deps: ProcessingDeps, *, expected_dataset: str
) -> FinalState:
    """Run one version to a final state. Idempotent for versions already finished."""
    with transaction(conn, "ci_ingest"):
        row = one(
            conn,
            "SELECT processing_state FROM communication_versions WHERE id = %s",
            (item.version_id,),
        )
        if row is None:
            raise ValueError("unknown version")
        if row["processing_state"] in TERMINAL:
            state: FinalState = row["processing_state"]
            return state

    # 1. Source-specific checks (e.g. the email attachment policy, D-E1).
    with transaction(conn, "ci_ingest"):
        for check in item.source_checks:
            _decision(
                conn,
                item.version_id,
                check.check_name,
                check.decision,
                check.reason_code,
                check.policy_version,
                None,
            )
        hold = next((c for c in item.source_checks if c.decision == "hold"), None)
        if hold is not None:
            return _finish_unpublished(conn, item, "held", hold.reason_code or hold.check_name)

    # 2. Screening. A dependency failure is pending, never pass.
    try:
        checks = deps.screener.screen(item.text)
    except ScreeningUnavailable as exc:
        with transaction(conn, "ci_ingest"):
            _decision(conn, item.version_id, "screening", "pending", exc.code, "screening.v1", None)
        raise RetryableProcessingError(exc.code) from exc
    with transaction(conn, "ci_ingest"):
        for c in checks:
            _decision(
                conn,
                item.version_id,
                c.check_name,
                c.decision,
                c.reason_code,
                "screening.v1",
                c.service_ref,
            )
        held = next((c for c in checks if c.decision == "hold"), None)
        if held is not None:
            return _finish_unpublished(conn, item, "held", held.reason_code or held.check_name)
        _set_state(conn, item.version_id, "screened")

    # 3. Classification: proposals, never approvals.
    output = deps.classifier.classify(item.classifier_input)
    decisions = {spec.name: decide(spec, output.probabilities.get(spec.name)) for spec in LABELS}
    outcome, reasons = derive_outcome(decisions)
    with transaction(conn, "ci_ingest"):
        run = one_required(
            conn,
            "INSERT INTO classification_runs (version_id, classifier_backend, model,"
            " policy_version, input_sha256, outcome, outcome_reasons)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (version_id, policy_version, input_sha256) DO UPDATE"
            " SET outcome = EXCLUDED.outcome RETURNING id",
            (
                item.version_id,
                output.backend.value,
                output.model,
                POLICY_VERSION,
                item.classifier_input.sha256(),
                outcome,
                reasons,
            ),
        )
        conn.execute("DELETE FROM classification_labels WHERE run_id = %s", (run["id"],))
        for spec in LABELS:
            prob = output.probabilities.get(spec.name)
            conn.execute(
                "INSERT INTO classification_labels (run_id, label, probability, no_threshold,"
                " yes_threshold, decision) VALUES (%s, %s, %s, %s, %s, %s)",
                (run["id"], spec.name, prob, spec.no, spec.yes, decisions[spec.name]),
            )
        verdict = {"restricted": "hold", "not_indexed": "reject"}.get(outcome, "allow")
        _decision(
            conn,
            item.version_id,
            "classification_outcome",
            verdict,
            reasons[0] if reasons else outcome,
            POLICY_VERSION,
            output.model,
        )
        if outcome == "restricted":
            return _finish_unpublished(conn, item, "held", reasons[0])
        if outcome == "not_indexed":
            return _finish_unpublished(conn, item, "rejected", reasons[0])

    # 4. Chunk and embed. The header gives the embedding context but is not evidence.
    texts = chunk_text(item.text) or [item.classifier_input.subject or "(no text)"]
    inputs = [f"{item.embedding_header}\n\n{t}" for t in texts]
    vectors = deps.embedder.embed(inputs)
    with transaction(conn, "ci_ingest"):
        conn.execute("DELETE FROM chunks WHERE version_id = %s", (item.version_id,))
        for seq, (text, emb_input, vec) in enumerate(zip(texts, inputs, vectors, strict=True)):
            conn.execute(
                "INSERT INTO chunks (version_id, communication_id, chunk_kind, seq, display_text,"
                " embedding_input_sha256, embedding_model, embedding)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                (
                    item.version_id,
                    item.communication_id,
                    item.chunk_kind,
                    seq,
                    text,
                    hashlib.sha256(emb_input.encode()).hexdigest(),
                    deps.embedder.model,
                    vector_literal(vec),
                ),
            )
        _set_state(conn, item.version_id, "embedded")

    # 5. Publish (re-checks the mode inside the same transaction).
    return _publish(conn, item, expected_dataset)
