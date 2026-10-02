"""Tests for the portable SQL module; these must not import Cheshire Cat."""

from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import schema
from record import (
    InteractionRecord,
    capture_generated,
    finalize_generated,
    resolve_turn,
    start_record,
)
from schema import (
    COLUMNS,
    CREATE_TABLE_SQL,
    DELETE_ID_SQL,
    INSERT_SQL,
    PURGE_SQL,
    ROLLBACK_SQL,
    SELECT_ID_SQL,
    START_TRANSACTION_SQL,
    TLS_CIPHER_SQL,
    TOOL_INPUT_ROW_INDEX,
    TOOL_INPUT_UPDATE_INDEX,
    TOOL_OUTPUT_ROW_INDEX,
    TOOL_OUTPUT_UPDATE_INDEX,
    UPDATE_COLUMNS,
    UPDATE_SQL,
    blank_at,
    retention_cutoff,
    to_row,
    to_update_params,
)


LOCAL_ID = "0123456789abcdef0123456789abcdef"
START_NS = 1_000_000_000
ALL_SQL = (
    CREATE_TABLE_SQL,
    INSERT_SQL,
    UPDATE_SQL,
    PURGE_SQL,
    SELECT_ID_SQL,
    DELETE_ID_SQL,
    START_TRANSACTION_SQL,
    ROLLBACK_SQL,
    TLS_CIPHER_SQL,
)

# Syntax or collations that exist on only one of MySQL and MariaDB.
ENGINE_SPECIFIC = ("0900", "IDENTIFIED", " VIA ", "RETURNING", "ON CONFLICT", "ILIKE")


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


def test_create_table_is_idempotent_and_uses_the_portable_collation():
    assert CREATE_TABLE_SQL.startswith("CREATE TABLE IF NOT EXISTS ril_interactions")
    assert "utf8mb4_unicode_ci" in CREATE_TABLE_SQL
    assert "CHARSET=utf8mb4" in CREATE_TABLE_SQL
    assert "ENGINE=InnoDB" in CREATE_TABLE_SQL
    assert re.findall(r"KEY (\w+)", CREATE_TABLE_SQL) == [
        "idx_ts",
        "idx_user_ts",
        "idx_input_verdict",
        "idx_output_verdict",
    ]


def test_create_table_declares_every_column_in_the_insert_order():
    declared = re.findall(r"^\s+(\w+)\s+(?:BIGINT|DATETIME|INT|VARCHAR|ENUM|MEDIUMTEXT|TEXT|BOOLEAN|SMALLINT|FLOAT)\b",
                          CREATE_TABLE_SQL, flags=re.MULTILINE)

    assert declared == ["id", *COLUMNS]


def test_no_statement_uses_syntax_found_in_only_one_engine():
    for statement in ALL_SQL:
        for token in ENGINE_SPECIFIC:
            assert token not in statement.upper(), token


def test_columns_match_the_record_fields_that_belong_in_the_table():
    record_only = {"started_ns", "local_turn_id", "guard_turn_id_at_start"}
    record_fields = {field.name for field in fields(InteractionRecord)}

    assert set(COLUMNS) == record_fields - record_only
    assert len(COLUMNS) == 19


def test_tool_and_recall_columns_come_last_in_the_table_the_insert_and_the_update():
    added = ("tools_used", "tool_input", "tool_output", "recall_sources")

    assert COLUMNS[-4:] == added
    assert set(added) <= set(UPDATE_COLUMNS)
    assert "tools_used VARCHAR(255) NULL," in CREATE_TABLE_SQL
    assert "tool_input MEDIUMTEXT NULL," in CREATE_TABLE_SQL
    assert "tool_output MEDIUMTEXT NULL," in CREATE_TABLE_SQL
    assert "recall_sources TEXT NULL," in CREATE_TABLE_SQL
    positions = [CREATE_TABLE_SQL.index(name) for name in ("recall_top_score", *added)]
    assert positions == sorted(positions)


def test_to_row_carries_the_tool_names():
    record = capture_generated(started(), "a", [], [(("get_time", {}), "12:00")])

    assert dict(zip(COLUMNS, to_row(record), strict=True))["tools_used"] == "get_time"
    assert dict(zip(COLUMNS, to_row(started()), strict=True))["tools_used"] is None


def test_blank_at_empties_only_the_chosen_value_in_a_row_and_in_the_update_values():
    record = capture_generated(started(), "a", [], [(("get_time", "Europe/Rome"), "12:00")])
    row = to_row(record)
    update_values = to_update_params(record, 5)[:-1]

    for row_index, update_index, column in (
        (TOOL_INPUT_ROW_INDEX, TOOL_INPUT_UPDATE_INDEX, "tool_input"),
        (TOOL_OUTPUT_ROW_INDEX, TOOL_OUTPUT_UPDATE_INDEX, "tool_output"),
    ):
        without_row = blank_at(row, row_index)
        without_update = blank_at(update_values, update_index)

        assert row[row_index] is not None
        assert without_row[row_index] is None
        assert [a for a, b in zip(row, without_row) if a != b] == [row[row_index]]
        assert without_update[update_index] is None
        assert len(without_row) == len(row) and len(without_update) == len(update_values)
        assert UPDATE_COLUMNS[update_index] == COLUMNS[row_index] == column


def test_insert_names_every_column_with_one_placeholder_each():
    assert INSERT_SQL == (
        "INSERT INTO ril_interactions (" + ", ".join(COLUMNS) + ") VALUES ("
        + ", ".join(["%s"] * len(COLUMNS)) + ")"
    )


def test_update_sets_only_the_finalisation_fields_by_id():
    assert UPDATE_SQL.endswith(" WHERE id = %s")
    assert UPDATE_SQL.count("%s") == len(UPDATE_COLUMNS) + 1
    assert set(UPDATE_COLUMNS) <= set(COLUMNS)
    assert not {"ts", "instance", "user_id", "question"} & set(UPDATE_COLUMNS)
    assert {"outcome", "turn_id", "llm_answer", "delivered", "duration_ms"} <= set(UPDATE_COLUMNS)
    for column in UPDATE_COLUMNS:
        assert f"{column} = %s" in UPDATE_SQL


def test_purge_deletes_old_rows_in_bounded_batches():
    assert PURGE_SQL == "DELETE FROM ril_interactions WHERE ts < %s LIMIT %s"


def test_to_row_follows_the_column_order_and_converts_ts_to_naive_utc():
    local = timezone(timedelta(hours=2))
    record = resolve_turn(started(ts=datetime(2026, 10, 1, 11, 30, tzinfo=local)), "AB12")
    record = capture_generated(record, "a", [{"score": 0.5}])
    record = finalize_generated(record, "b", None, START_NS + 250_000_000)

    row = dict(zip(COLUMNS, to_row(record), strict=True))

    assert row["ts"] == datetime(2026, 10, 1, 9, 30)
    assert row["ts"].tzinfo is None
    assert row["turn_id"] == "AB12"
    assert row["outcome"] == "generated"
    assert row["llm_answer"] == "a"
    assert row["delivered"] == "b"
    assert row["guard_present"] is True
    assert row["recall_count"] == 1
    assert row["duration_ms"] == 250


def test_to_row_of_a_fresh_record_is_a_valid_incomplete_insert():
    row = dict(zip(COLUMNS, to_row(started()), strict=True))

    assert row["outcome"] == "incomplete"
    assert row["turn_id"] == LOCAL_ID
    assert row["guard_present"] is False
    assert row["question"] == "How do I reset my password?"
    assert row["llm_answer"] is row["delivered"] is row["duration_ms"] is None


def test_to_row_keeps_a_naive_ts_as_it_is():
    naive = datetime(2026, 10, 1, 9, 30)

    assert dict(zip(COLUMNS, to_row(started(ts=naive))))["ts"] == naive


def test_to_row_truncates_values_to_their_column_width():
    record = resolve_turn(started(instance="i" * 300, user_id="u" * 300), "T" * 40)
    record = finalize_generated(record, "reply", "v" * 100, START_NS)

    row = dict(zip(COLUMNS, to_row(record), strict=True))

    assert len(row["instance"]) == len(row["user_id"]) == 255
    assert len(row["turn_id"]) == 32
    assert len(row["output_verdict"]) == 64


def test_update_params_end_with_the_row_id_in_set_order():
    record = finalize_generated(resolve_turn(started(), "AB12"), "b", None, START_NS)

    params = to_update_params(record, 77)
    row = dict(zip(COLUMNS, to_row(record), strict=True))

    assert params == (*[row[column] for column in UPDATE_COLUMNS], 77)


def test_retention_zero_never_deletes():
    assert retention_cutoff(datetime(2026, 10, 1, 12, tzinfo=timezone.utc), 0) is None


def test_retention_keeps_whole_utc_days():
    now = datetime(2026, 10, 3, 15, 45, 12, tzinfo=timezone.utc)

    assert retention_cutoff(now, 1) == datetime(2026, 10, 2)
    assert retention_cutoff(now, 30) == datetime(2026, 9, 3)


def test_retention_cutoff_is_computed_in_utc_not_local_time():
    local = timezone(timedelta(hours=2))
    just_after_local_midnight = datetime(2026, 10, 3, 1, 0, tzinfo=local)

    assert retention_cutoff(just_after_local_midnight, 1) == datetime(2026, 10, 1)


def test_retention_cutoff_is_naive_to_match_the_column():
    assert retention_cutoff(datetime(2026, 10, 3, 15, tzinfo=timezone.utc), 7).tzinfo is None


def test_schema_module_does_not_import_cheshire_cat_or_the_driver():
    source = Path(schema.__file__).read_text(encoding="utf-8")

    assert "import cat" not in source and "from cat" not in source
    assert "pymysql" not in source.lower()


def test_the_table_in_the_readme_is_the_one_the_plugin_creates():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```sql\n(.*?)```", readme, flags=re.DOTALL)
    ddl = [block for block in blocks if block.startswith("CREATE TABLE IF NOT EXISTS")]

    assert len(ddl) == 1
    assert " ".join(ddl[0].replace(";", "").split()) == " ".join(CREATE_TABLE_SQL.split())
