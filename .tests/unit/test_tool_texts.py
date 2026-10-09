"""Tool outputs, the text limit and the recalled documents: pure functions only."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from record import (
    RECALL_SOURCE_LIMIT,
    RECALL_LABEL_LIMIT,
    RECALL_SOURCES_BYTES,
    RECALL_TITLE_LIMIT,
    RECALL_URL_LIMIT,
    RECALL_SOURCES_MAX,
    TOOL_CALLS_MAX,
    TOOL_TEXT_LIMIT,
    TOOL_TEXT_LIMIT_MAX,
    TOOL_TEXT_LIMIT_MIN,
    capture_generated,
    recall_sources_json,
    start_record,
    tool_calls,
    tool_output_json,
    tool_text_limit_from,
    tool_texts_json,
)
from datetime import datetime, timezone


def started():
    return start_record(
        ts=datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc),
        started_ns=0,
        instance="i",
        user_id="u",
        question="q",
        guard_turn_id_at_start=None,
        local_turn_id="a" * 32,
    )


def test_tool_output_json_pairs_the_name_with_the_output_text():
    calls = tool_calls([(("get_time", "Europe/Rome"), "12:00"), (("send_email", "x"), "sent")])

    assert json.loads(tool_output_json(calls)) == [
        {"tool": "get_time", "output": "12:00"},
        {"tool": "send_email", "output": "sent"},
    ]


def test_a_cut_output_carries_the_flag_and_the_original_length():
    calls = [("big", "in", "o" * 1500)]

    (item,) = json.loads(tool_output_json(calls, limit=1000))

    assert item == {"tool": "big", "output": "o" * 1000, "cut": True, "chars": 1500}


def test_a_text_exactly_at_the_limit_is_not_cut():
    (item,) = json.loads(tool_output_json([("t", "", "o" * 300)], limit=300))

    assert "cut" not in item and len(item["output"]) == 300


def test_the_limit_is_in_characters_so_an_emoji_is_never_split():
    (item,) = json.loads(tool_output_json([("t", "", "😀" * 10)], limit=3))

    assert item["output"] == "😀😀😀" and item["chars"] == 10


def test_the_json_stays_valid_whatever_the_text_holds():
    nasty = 'quote " backslash \\ newline \n tab \t nul \x00 città 😀'

    for field in ("input", "output"):
        text = tool_texts_json([("t", nasty, nasty)], field)

        assert json.loads(text)[0][field] == nasty


def test_at_most_ten_calls_go_into_each_array():
    calls = [(f"t{i}", "in", "out") for i in range(TOOL_CALLS_MAX + 5)]

    assert len(json.loads(tool_texts_json(calls, "input"))) == TOOL_CALLS_MAX


def test_no_calls_or_an_unknown_field_give_none():
    assert tool_texts_json([], "output") is None
    assert tool_texts_json([("t", "i", "o")], "other") is None


def test_the_limit_given_to_capture_generated_cuts_both_texts():
    record = capture_generated(
        started(), "a", [], [(("t", "i" * 300), "o" * 300)], tool_limit=100
    )

    assert json.loads(record.tool_input)[0]["chars"] == 300
    assert len(json.loads(record.tool_output)[0]["output"]) == 100


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, TOOL_TEXT_LIMIT),
        ({"tool_text_limit": 250}, 250),
        ({"tool_text_limit": "400"}, 400),
        ({"tool_text_limit": 1}, TOOL_TEXT_LIMIT_MIN),
        ({"tool_text_limit": 10**9}, TOOL_TEXT_LIMIT_MAX),
        ({"tool_text_limit": "abc"}, TOOL_TEXT_LIMIT),
        ({"tool_text_limit": None}, TOOL_TEXT_LIMIT),
    ],
)
def test_the_limit_is_read_from_the_settings_and_kept_in_range(settings, expected):
    assert tool_text_limit_from(settings) == expected


def test_the_limit_of_a_missing_settings_object_is_the_default():
    assert tool_text_limit_from(None) == TOOL_TEXT_LIMIT


DOCS = [
    {"id": "p-1", "score": 0.834, "page_content": "SECRET TEXT", "type": "x",
     "metadata": {"source": "guida_badge.pdf", "when": 1790000000}},
    {"id": 42, "score": 0.7, "page_content": "MORE TEXT", "metadata": {"source": "https://example.org/a"}},
]


def test_recall_sources_keep_id_source_and_score_in_the_recall_order():
    assert json.loads(recall_sources_json(DOCS)) == [
        {"id": "p-1", "type": "x", "source": "guida_badge.pdf", "score": 0.834},
        {"id": "42", "source": "https://example.org/a", "score": 0.7},
    ]


def test_the_text_of_a_recalled_document_never_reaches_the_result():
    text = recall_sources_json(DOCS)

    assert "TEXT" not in text and "page_content" not in text and "when" not in text


def test_a_document_without_metadata_or_source_still_counts_by_its_id():
    docs = [{"id": "a", "score": 0.5}, {"id": "b", "score": 0.4, "metadata": None},
            {"id": "c", "metadata": {"source": ""}}]

    assert json.loads(recall_sources_json(docs)) == [
        {"id": "a", "score": 0.5}, {"id": "b", "score": 0.4}, {"id": "c"},
    ]


def test_entries_with_neither_id_nor_source_or_not_dicts_are_ignored():
    docs = ["odd", None, 5, {"score": 0.9}, {"id": None, "metadata": {}}, {"id": "ok"}]

    assert json.loads(recall_sources_json(docs)) == [{"id": "ok"}]


def test_a_long_source_is_cut_before_serialising_and_flagged():
    (item,) = json.loads(recall_sources_json([{"id": "a", "metadata": {"source": "s" * 500}}]))

    assert item["source"] == "s" * RECALL_SOURCE_LIMIT and item["cut"] is True


def test_the_identifying_metadata_is_kept_but_never_the_text_or_other_keys():
    docs = [{"id": "w1", "type": "Document", "score": 0.87, "page_content": "SECRET TEXT",
             "metadata": {"origin": "WordPress", "url": "https://sitoict.local/servizi/wifi-sns/",
                          "title": "WiFi SNS", "wp_id": "414", "source": "user",
                          "when": 1785754139.88, "internal": "HIDDEN"}}]

    text = recall_sources_json(docs)

    assert json.loads(text) == [{
        "id": "w1", "type": "Document", "source": "user", "origin": "WordPress", "wp_id": "414",
        "url": "https://sitoict.local/servizi/wifi-sns/", "title": "WiFi SNS", "score": 0.87,
    }]
    assert "SECRET" not in text and "HIDDEN" not in text and "when" not in text


def test_a_numeric_wp_id_is_kept_and_a_boolean_or_empty_label_is_not():
    docs = [{"id": "a", "type": "", "metadata": {"wp_id": 414, "origin": True}},
            {"id": "b", "type": None, "metadata": {"wp_id": ["x"], "title": ""}}]

    assert json.loads(recall_sources_json(docs)) == [{"id": "a", "wp_id": "414"}, {"id": "b"}]


def test_a_long_title_is_cut_and_flagged_and_labels_are_cut_silently():
    (item,) = json.loads(recall_sources_json([{
        "id": "a", "type": "T" * 200, "metadata": {"title": "t" * 500, "origin": "o" * 200}}]))

    assert len(item["title"]) == RECALL_TITLE_LIMIT and item["title_cut"] is True
    assert len(item["type"]) == RECALL_LABEL_LIMIT and len(item["origin"]) == RECALL_LABEL_LIMIT


def test_a_missing_or_invalid_url_is_not_an_error_and_a_url_alone_does_not_make_an_entry():
    docs = [{"id": "a", "metadata": {"url": ""}}, {"id": "b", "metadata": {"url": 5}},
            {"id": "c", "metadata": {"url": None}}, {"metadata": {"url": "https://x.org"}}]

    assert json.loads(recall_sources_json(docs)) == [{"id": "a"}, {"id": "b"}, {"id": "c"}]


def test_a_long_url_is_cut_before_serialising_and_flagged():
    (item,) = json.loads(recall_sources_json([{"id": "a", "metadata": {"url": "https://x.org/" + "u" * 900}}]))

    assert len(item["url"]) == RECALL_URL_LIMIT and item["url_cut"] is True and "cut" not in item


def test_the_largest_recall_text_fits_the_text_column_even_with_four_byte_characters():
    docs = [{"id": "😀" * 64, "type": "😀" * 64, "score": 0.123456,
             "metadata": {"source": "😀" * 400, "url": "😀" * 900, "title": "😀" * 400,
                          "origin": "😀" * 64, "wp_id": "😀" * 64}} for _ in range(RECALL_SOURCES_MAX)]

    text = recall_sources_json(docs)

    assert len(text.encode("utf-8")) <= RECALL_SOURCES_BYTES < 65535
    assert 0 < len(json.loads(text)) < RECALL_SOURCES_MAX  # the last documents were dropped
    assert json.loads(text)[0]["id"] == "😀" * 64


def test_a_normal_recall_is_never_shortened_by_the_size_guard():
    docs = [{"id": str(i), "type": "Document", "score": 0.5,
             "metadata": {"source": "user", "url": "https://x.org/" + "p" * 80, "title": "T" * 60,
                          "origin": "WordPress", "wp_id": str(i)}} for i in range(RECALL_SOURCES_MAX)]

    assert len(json.loads(recall_sources_json(docs))) == RECALL_SOURCES_MAX


def test_a_long_id_is_cut_and_a_boolean_score_is_not_a_score():
    (item,) = json.loads(recall_sources_json([{"id": "x" * 200, "score": True}]))

    assert len(item["id"]) == 64 and "score" not in item


def test_at_most_twenty_documents_are_kept():
    docs = [{"id": str(i), "score": 0.5} for i in range(RECALL_SOURCES_MAX + 7)]

    assert len(json.loads(recall_sources_json(docs))) == RECALL_SOURCES_MAX


def test_the_score_is_rounded_to_six_decimals():
    (item,) = json.loads(recall_sources_json([{"id": "a", "score": 0.123456789}]))

    assert item["score"] == 0.123457


@pytest.mark.parametrize("recalled", [None, [], (), "text", 5, [{}], [None]])
def test_nothing_recalled_gives_none(recalled):
    assert recall_sources_json(recalled) is None


def test_capture_generated_records_the_sources_next_to_the_count():
    record = capture_generated(started(), "a", DOCS)

    assert record.recall_count == 2
    assert json.loads(record.recall_sources)[0]["source"] == "guida_badge.pdf"
    assert capture_generated(started(), "a", None).recall_sources is None
    assert capture_generated(started(), "a", []).recall_sources is None


class Unconvertible:
    """Stands for a huge tool result: converting it to text must not happen."""

    def __str__(self):
        raise AssertionError("a tool text that is not saved was converted")


def test_texts_that_are_not_to_be_saved_are_never_converted():
    steps = [(("service_status", Unconvertible()), Unconvertible())]

    record = capture_generated(started(), "answer", [], steps, include_tool_input=False, include_tool_output=False)

    assert record.tools_used == "service_status"
    assert record.tool_input is None and record.tool_output is None


def test_only_the_requested_text_is_converted():
    steps = [(("service_status", "kto"), Unconvertible())]

    record = capture_generated(started(), "answer", [], steps, include_tool_output=False)

    assert json.loads(record.tool_input) == [{"tool": "service_status", "input": "kto"}]
    assert record.tool_output is None


def test_both_texts_are_kept_by_default():
    steps = [(("service_status", "kto"), "up")]

    record = capture_generated(started(), "answer", [], steps)

    assert json.loads(record.tool_input)[0]["input"] == "kto"
    assert json.loads(record.tool_output)[0]["output"] == "up"
