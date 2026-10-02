"""Tests for pure record assembly; these must not import Cheshire Cat."""

from datetime import datetime, timezone
import json
from pathlib import Path
import pickle
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import record as record_module
from record import (
    TOOLS_USED_WIDTH,
    TOOL_TEXT_LIMIT,
    capture_generated,
    extract_reply_text,
    finalize_fast_reply,
    finalize_generated,
    join_tools,
    resolve_turn,
    start_record,
    tool_calls,
    tool_input_json,
    tool_names,
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


class Action:
    """Stands in for an agent action object that carries the tool name."""

    def __init__(self, tool):
        self.tool = tool


def test_tool_names_keeps_only_the_name_of_each_step_in_order():
    steps = [
        (("get_time", {"zone": "Europe/Rome"}), "12:00"),
        (("send_email", "to alice, subject hi"), "ok"),
    ]

    assert tool_names(steps) == ["get_time", "send_email"]


def test_tool_names_accepts_a_form_step_and_a_step_made_of_lists():
    assert tool_names([(("pizza_order", ""), "Which size?")]) == ["pizza_order"]
    assert tool_names([[["lookup", "x"], "out"]]) == ["lookup"]


def test_tool_names_accepts_action_objects_and_dict_actions():
    assert tool_names([(Action("a_tool"), "out"), ({"tool": "b_tool"}, "out")]) == ["a_tool", "b_tool"]


def test_tool_names_keeps_every_invocation_even_when_repeated():
    steps = [(("t", ""), "1"), (("t", ""), "2")]

    assert tool_names(steps) == ["t", "t"]


def test_tool_names_replaces_a_comma_so_the_list_stays_parsable():
    assert tool_names([(("a,b", ""), "out")]) == ["a_b"]


@pytest.mark.parametrize(
    "steps",
    [None, [], (), "not a list", 42, [None], [()], [("x",)], [((),)], [((1, ""), "o")], [(("", ""), "o")], [(("  ", ""), "o")], [{"tool": "x"}]],
)
def test_tool_names_ignores_anything_that_is_not_a_step_with_a_name(steps):
    assert tool_names(steps) == []


def test_join_tools_is_a_comma_list_or_none():
    assert join_tools(["a", "b"]) == "a,b"
    assert join_tools([]) is None


def test_join_tools_keeps_only_whole_names_that_fit():
    names = ["a" * 100, "b" * 100, "c" * 100]

    joined = join_tools(names)

    assert joined == "a" * 100 + "," + "b" * 100
    assert len(joined) <= TOOLS_USED_WIDTH
    assert join_tools(["x" * 300]) is None
    assert join_tools(["a" * 254, "b"]) == "a" * 254


def test_a_generated_turn_records_the_tools_that_ran():
    record = capture_generated(started(), "answer", [], [(("get_time", {}), "12:00")])

    assert record.tools_used == "get_time"


def test_tools_used_is_null_without_steps_or_without_tools():
    assert capture_generated(started(), "a", [], None).tools_used is None
    assert capture_generated(started(), "a", [], []).tools_used is None
    assert capture_generated(started(), "a", [], [("odd",)]).tools_used is None
    assert capture_generated(started(), "a", []).tools_used is None


def test_tools_used_survives_the_finalisation_and_a_fast_reply_has_none():
    record = capture_generated(resolve_turn(started(), "AB12"), "a", [], [(("t", ""), "o")])

    assert finalize_generated(record, "b", None, NOW_NS).tools_used == "t"
    assert finalize_fast_reply(started(), "x", None, NOW_NS).tools_used is None


def test_tool_calls_pair_each_name_with_its_input_and_output_text():
    steps = [
        (("get_time", "Europe/Rome"), "12:00"),
        (("lookup", {"id": 7, "q": "città"}), {"found": True}),
        (("noop", None), None),
        (("bare",), "out"),
        (Action("obj"), "out"),
        (("no_output", "x"),),
    ]

    assert tool_calls(steps) == [
        ("get_time", "Europe/Rome", "12:00"),
        ("lookup", '{"id": 7, "q": "città"}', '{"found": true}'),
        ("noop", "", ""),
        ("bare", "", "out"),
        ("obj", "", "out"),
        ("no_output", "x", ""),
    ]


def test_tool_input_json_is_an_array_of_name_and_input_in_order():
    calls = [
        ("get_time", "Europe/Rome", "12:00"),
        ("send_email", 'a, b "quoted" città 😀\nsecond line', "sent"),
    ]

    text = tool_input_json(calls)

    assert json.loads(text) == [
        {"tool": "get_time", "input": "Europe/Rome"},
        {"tool": "send_email", "input": 'a, b "quoted" città 😀\nsecond line'},
    ]
    assert "città" in text and "😀" in text


def test_tool_input_json_cuts_each_input_keeps_the_json_valid_and_records_the_length():
    calls = [("a", "x" * (TOOL_TEXT_LIMIT + 50), ""), ("b", "short", "")]

    items = json.loads(tool_input_json(calls))

    assert items[0] == {
        "tool": "a",
        "input": "x" * TOOL_TEXT_LIMIT,
        "cut": True,
        "chars": TOOL_TEXT_LIMIT + 50,
    }
    assert items[1] == {"tool": "b", "input": "short"}


def test_tool_input_json_is_none_when_no_tool_ran():
    assert tool_input_json([]) is None


def test_a_generated_turn_keeps_the_input_next_to_the_name():
    record = capture_generated(started(), "a", [], [(("get_time", "Europe/Rome"), "tool output")])

    assert record.tools_used == "get_time"
    assert json.loads(record.tool_input) == [{"tool": "get_time", "input": "Europe/Rome"}]
    assert "tool output" not in record.tool_input


def test_the_name_in_the_json_keeps_its_comma_while_the_list_replaces_it():
    record = capture_generated(started(), "a", [], [(("a,b", "x"), "o")])

    assert record.tools_used == "a_b"
    assert json.loads(record.tool_input)[0]["tool"] == "a,b"


def test_tool_input_is_null_without_steps_or_without_tools():
    assert capture_generated(started(), "a", [], None).tool_input is None
    assert capture_generated(started(), "a", [], []).tool_input is None


def test_record_module_does_not_import_cheshire_cat():
    source = Path(record_module.__file__).read_text(encoding="utf-8")

    assert "import cat" not in source and "from cat" not in source
