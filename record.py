"""Pure interaction-record state, independent from Cheshire Cat and MySQL."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(slots=True)
class InteractionRecord:
    """A turn observed by the logger before it is written asynchronously."""

    ts: datetime
    started_ns: int
    instance: str
    user_id: str
    question: str | None
    local_turn_id: str
    guard_turn_id_at_start: str | None = None
    turn_id: str | None = None
    guard_present: bool = False
    llm_answer: str | None = None
    delivered: str | None = None
    input_verdict: str | None = None
    output_verdict: str | None = None
    other_plugin_reply: bool | None = None
    recall_count: int | None = None
    recall_top_score: float | None = None
    duration_ms: int | None = None
    outcome: str = "incomplete"


def start_record(
    *,
    ts: datetime,
    started_ns: int,
    instance: str,
    user_id: str,
    question: str | None,
    guard_turn_id_at_start: str | None,
    local_turn_id: str,
) -> InteractionRecord:
    """Create the incomplete record that H1 stores in working memory.

    The guard has not run yet in H1, so any guard turn id already present
    belongs to an earlier turn; it is kept to recognise a stale attribute later.
    """
    return InteractionRecord(
        ts=ts,
        started_ns=started_ns,
        instance=instance,
        user_id=user_id,
        question=question,
        local_turn_id=local_turn_id,
        guard_turn_id_at_start=guard_turn_id_at_start or None,
        turn_id=local_turn_id,
    )


def resolve_turn(record: InteractionRecord, guard_turn_id: str | None) -> InteractionRecord:
    """Decide whether the guard handled this turn and fix the turn id.

    The guard attribute persists in working memory, so an id equal to the one
    seen in H1 was left by an earlier turn, or by a guard that is not running.
    """
    present = bool(guard_turn_id) and guard_turn_id != record.guard_turn_id_at_start
    return replace(
        record,
        guard_present=present,
        turn_id=guard_turn_id if present else record.local_turn_id,
    )


def capture_generated(
    record: InteractionRecord, text: str | None, declarative: list | None
) -> InteractionRecord:
    """Store the LLM answer and the recall summary before any rewrite (H3)."""
    if declarative is None:
        return replace(record, llm_answer=text)
    scores = [
        entry["score"]
        for entry in declarative
        if isinstance(entry, dict) and isinstance(entry.get("score"), (int, float))
    ]
    return replace(
        record,
        llm_answer=text,
        recall_count=len(declarative),
        recall_top_score=max(scores) if scores else None,
    )


def finalize_fast_reply(
    record: InteractionRecord, delivered: str | None, input_verdict: str | None, now_ns: int
) -> InteractionRecord:
    """Close a turn answered on `fast_reply`; only a present guard has a verdict."""
    verdict = input_verdict if record.guard_present else None
    return replace(
        record,
        outcome="fast_reply",
        llm_answer=None,
        delivered=delivered,
        input_verdict=verdict,
        other_plugin_reply=verdict is None if record.guard_present else None,
        duration_ms=_duration_ms(record, now_ns),
    )


def finalize_generated(
    record: InteractionRecord, delivered: str | None, output_verdict: str | None, now_ns: int
) -> InteractionRecord:
    """Close a turn whose answer was generated and then delivered (H4)."""
    return replace(
        record,
        outcome="generated",
        delivered=delivered,
        output_verdict=output_verdict if record.guard_present else None,
        other_plugin_reply=False if record.guard_present else None,
        duration_ms=_duration_ms(record, now_ns),
    )


def extract_reply_text(value) -> str | None:
    """Return the text of a `fast_reply` result that ends the turn, else None.

    Mirrors the core: a message object carries `text`, a dict carries `output`.
    """
    if isinstance(value, dict):
        return str(value["output"]) if "output" in value else None
    text = getattr(value, "text", None)
    return text if isinstance(text, str) else None


def _duration_ms(record: InteractionRecord, now_ns: int) -> int:
    return max(0, (now_ns - record.started_ns) // 1_000_000)
