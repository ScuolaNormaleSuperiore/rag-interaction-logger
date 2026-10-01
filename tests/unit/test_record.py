"""Tests for pure record assembly; these must not import Cheshire Cat."""

from datetime import datetime, timezone
from pathlib import Path
import pickle
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import record as record_module
from record import (
    capture_generated,
    extract_reply_text,
    finalize_fast_reply,
    finalize_generated,
    resolve_turn,
    start_record,
)


START_NS = 1_000_000_000
NOW_NS = START_NS + 250_000_000
LOCAL_ID = "0123456789abcdef0123456789abcdef"


def started(**overrides):
    values = dict(
        ts=datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc),
        started_ns=START_NS,
        instance="cat-test-1",
        user_id="user-42",
        question="How do I reset my password?",
        guard_turn_id_at_start=None,
        local_turn_id=LOCAL_ID,
    )
    values.update(overrides)
    return start_record(**values)


def test_start_record_is_incomplete_and_preserves_its_input():
    timestamp = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)

    record = started(ts=timestamp)

    assert record.ts is timestamp
    assert record.instance == "cat-test-1"
    assert record.user_id == "user-42"
    assert record.question == "How do I reset my password?"
    assert record.outcome == "incomplete"
    assert record.turn_id == LOCAL_ID
    assert record.llm_answer is None
    assert record.delivered is None
    assert record.duration_ms is None
    assert record.guard_present is False


def test_functions_return_new_records_and_never_mutate_their_input():
    original = started()

    resolve_turn(original, "AB12")
    capture_generated(original, "answer", [{"score": 0.9}])
    finalize_generated(original, "delivered", None, NOW_NS)
    finalize_fast_reply(original, "delivered", None, NOW_NS)

    assert original == started()


def test_record_survives_pickle_for_the_file_system_cache():
    record = capture_generated(resolve_turn(started(), "AB12"), "a", [{"score": 0.5}])

    assert pickle.loads(pickle.dumps(record)) == record


def test_turn_id_comes_from_the_guard_when_it_changed_since_h1():
    record = resolve_turn(started(guard_turn_id_at_start="ZZ99"), "AB12")

    assert record.guard_present is True
    assert record.turn_id == "AB12"


def test_guard_id_already_present_at_h1_is_a_stale_attribute():
    record = resolve_turn(started(guard_turn_id_at_start="AB12"), "AB12")

    assert record.guard_present is False
    assert record.turn_id == LOCAL_ID


def test_id_left_by_a_failed_turn_does_not_count_for_a_turn_without_guard():
    failed_turn_left = "AB12"

    record = resolve_turn(started(guard_turn_id_at_start=failed_turn_left), failed_turn_left)

    assert record.guard_present is False


def test_guard_that_runs_after_a_failed_turn_is_recognised():
    record = resolve_turn(started(guard_turn_id_at_start="AB12"), "AB13")

    assert record.guard_present is True
    assert record.turn_id == "AB13"


def test_missing_guard_keeps_the_local_turn_id():
    for absent in (None, ""):
        record = resolve_turn(started(), absent)

        assert record.guard_present is False
        assert record.turn_id == LOCAL_ID


def test_generated_turn_passed_by_the_guard():
    record = resolve_turn(started(), "AB12")
    record = capture_generated(record, "LLM answer", [{"score": 0.71}, {"score": 0.84}])
    record = finalize_generated(record, "LLM answer", None, NOW_NS)

    assert record.outcome == "generated"
    assert record.llm_answer == "LLM answer"
    assert record.delivered == "LLM answer"
    assert record.output_verdict is None
    assert record.input_verdict is None
    assert record.other_plugin_reply is False
    assert record.recall_count == 2
    assert record.recall_top_score == 0.84
    assert record.duration_ms == 250


def test_generated_turn_blocked_on_output_keeps_both_answers():
    record = resolve_turn(started(), "AB12")
    record = capture_generated(record, "Call me on 333 1234567", [])
    record = finalize_generated(record, "I cannot share that.", "personal_data", NOW_NS)

    assert record.llm_answer == "Call me on 333 1234567"
    assert record.delivered == "I cannot share that."
    assert record.output_verdict == "personal_data"
    assert record.other_plugin_reply is False


def test_fast_reply_blocked_on_input_by_the_guard():
    record = resolve_turn(started(), "AB12")
    record = finalize_fast_reply(record, "Request refused.", "prompt_injection", NOW_NS)

    assert record.outcome == "fast_reply"
    assert record.llm_answer is None
    assert record.delivered == "Request refused."
    assert record.input_verdict == "prompt_injection"
    assert record.other_plugin_reply is False


def test_fast_reply_by_another_plugin_with_the_guard_present():
    record = resolve_turn(started(), "AB12")
    record = finalize_fast_reply(record, "Slow down.", None, NOW_NS)

    assert record.outcome == "fast_reply"
    assert record.input_verdict is None
    assert record.other_plugin_reply is True


def test_fast_reply_without_the_guard_cannot_tell_who_answered():
    record = resolve_turn(started(), None)
    record = finalize_fast_reply(record, "Slow down.", None, NOW_NS)

    assert record.guard_present is False
    assert record.other_plugin_reply is None


def test_verdicts_of_an_absent_guard_are_not_recorded():
    stale = resolve_turn(started(guard_turn_id_at_start="AB12"), "AB12")

    fast = finalize_fast_reply(stale, "reply", "prompt_injection", NOW_NS)
    generated = finalize_generated(stale, "reply", "personal_data", NOW_NS)

    assert fast.input_verdict is None
    assert generated.output_verdict is None
    assert generated.other_plugin_reply is None


def test_a_turn_that_is_never_finalised_stays_incomplete():
    record = resolve_turn(started(), "AB12")
    record = capture_generated(record, "partial", [])

    assert record.outcome == "incomplete"
    assert record.delivered is None
    assert record.duration_ms is None


def test_recall_is_null_when_unknowable_and_zero_when_empty():
    unknown = capture_generated(started(), "a", None)
    empty = capture_generated(started(), "a", [])

    assert (unknown.recall_count, unknown.recall_top_score) == (None, None)
    assert (empty.recall_count, empty.recall_top_score) == (0, None)


def test_recall_tolerates_entries_without_a_numeric_score():
    record = capture_generated(started(), "a", [{"score": "x"}, {}, "odd", {"score": 0.7}])

    assert record.recall_count == 4
    assert record.recall_top_score == 0.7


def test_duration_is_never_negative():
    record = finalize_generated(started(), "a", None, START_NS - 5)

    assert record.duration_ms == 0


def test_extract_reply_text_reads_a_message_or_an_output_dict():
    class Message:
        text = "from a CatMessage"

    assert extract_reply_text(Message()) == "from a CatMessage"
    assert extract_reply_text({"output": "from a dict"}) == "from a dict"
    assert extract_reply_text({"output": 42}) == "42"
    assert extract_reply_text({}) is None
    assert extract_reply_text(None) is None
    assert extract_reply_text({"other": 1}) is None
    assert extract_reply_text(object()) is None


def test_record_module_does_not_import_cheshire_cat():
    source = Path(record_module.__file__).read_text(encoding="utf-8")

    assert "import cat" not in source and "from cat" not in source
