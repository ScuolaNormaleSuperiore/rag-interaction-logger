"""Tests for pure record assembly; these must not import Cheshire Cat."""

from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from record import start_record


def test_start_record_is_incomplete_and_preserves_its_input():
    timestamp = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)

    record = start_record(
        ts=timestamp,
        instance="cat-test-1",
        user_id="user-42",
        question="How do I reset my password?",
    )

    assert record.ts is timestamp
    assert record.instance == "cat-test-1"
    assert record.user_id == "user-42"
    assert record.question == "How do I reset my password?"
    assert record.outcome == "incomplete"
    assert record.delivered is None
