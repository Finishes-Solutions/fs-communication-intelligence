"""Label quality from reviewer judgements ("Label check" in the admin console).

For each label, reviewer answers ("does it apply to this email?") are compared
with the model's current decision: precision and recall at today's thresholds,
how many judged emails sit in the uncertain band, and, once enough emails are
judged, the "yes" threshold that would have scored best (F1) on them. A
suggestion is advice for a person to apply, never applied automatically, and
it is only as good as the sample: small or one-sided samples get none.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from ci_processing.classification import LABELS, LABELS_BY_NAME

MIN_SAMPLE = 20  # judged emails per label before a threshold is suggested
MIN_EACH_CLASS = 5  # ...with at least this many "applies" and "doesn't apply"


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _best_threshold(points: list[tuple[float, bool]]) -> dict[str, Any] | None:
    """The yes-threshold (one of the observed probabilities) with the highest F1."""
    best: dict[str, Any] | None = None
    positives = sum(1 for _, applies in points if applies)
    for threshold in sorted({p for p, _ in points}):
        tp = sum(1 for p, a in points if p >= threshold and a)
        fp = sum(1 for p, a in points if p >= threshold and not a)
        if tp == 0:
            continue
        precision, recall = tp / (tp + fp), tp / positives
        f1 = 2 * precision * recall / (precision + recall)
        if best is None or f1 > best["f1"]:
            best = {
                "yes_at_or_above": round(threshold, 3),
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "f1": round(f1, 3),
            }
    return best


def label_report(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows``: label, applies, probability, decision (one per judged email and label)."""
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["label"] in LABELS_BY_NAME:
            by_label[row["label"]].append(row)
    report = []
    for label in LABELS:
        judged = by_label.get(label.name, [])
        counts = Counter(
            (
                "yes" if r["decision"] == "yes" else "no" if r["decision"] == "no" else "uncertain",
                bool(r["applies"]),
            )
            for r in judged
        )
        tp, fp = counts[("yes", True)], counts[("yes", False)]
        positives = sum(1 for r in judged if r["applies"])
        points = [
            (float(r["probability"]), bool(r["applies"]))
            for r in judged
            if r["probability"] is not None
        ]
        enough = (
            len(points) >= MIN_SAMPLE
            and positives >= MIN_EACH_CLASS
            and len(points) - positives >= MIN_EACH_CLASS
        )
        report.append(
            {
                "label": label.name,
                "judged": len(judged),
                "applies": positives,
                "precision": _ratio(tp, tp + fp),
                "recall": _ratio(tp, positives),
                "uncertain": counts[("uncertain", True)] + counts[("uncertain", False)],
                "no_at_or_below": label.no_at_or_below,
                "yes_at_or_above": label.yes_at_or_above,
                "suggestion": _best_threshold(points) if enough else None,
                "needed": max(0, MIN_SAMPLE - len(points)),
            }
        )
    return report


def action_report(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows``: person, applies, reason_code. How often a listed item was really theirs."""
    people: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = people.setdefault(
            row["person"], {"person": row["person"], "judged": 0, "correct": 0, "reasons": {}}
        )
        entry["judged"] += 1
        if row["applies"]:
            entry["correct"] += 1
        elif row["reason_code"]:
            entry["reasons"][row["reason_code"]] = entry["reasons"].get(row["reason_code"], 0) + 1
    for entry in people.values():
        entry["precision"] = _ratio(entry["correct"], entry["judged"])
    return sorted(people.values(), key=lambda e: -e["judged"])
