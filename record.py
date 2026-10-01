"""Pure interaction-record state, independent from Cheshire Cat and MySQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class InteractionRecord:
    """A turn observed by the logger before it is written asynchronously."""

    ts: datetime
    instance: str
    user_id: str
    question: str | None
    turn_id: str | None = None
    guard_present: bool = False
    llm_answer: str | None = None
    delivered: str | None = None
    input_verdict: str | None = None
    output_verdict: str | None = None
    outcome: str = "incomplete"


def start_record(
    *, ts: datetime, instance: str, user_id: str, question: str | None
) -> InteractionRecord:
    """Create the incomplete record that H1 stores in working memory."""
    return InteractionRecord(
        ts=ts,
        instance=instance,
        user_id=user_id,
        question=question,
    )
