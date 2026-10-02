"""Every SQL statement of the logger, written to run unchanged on MySQL and MariaDB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# The Cat imports plugin files as `cat.plugins.<folder>.<module>`, so the
# relative import is the one that works at runtime; the absolute fallback is for
# the tests, which put the plugin folder on the path.
try:
    from .record import TOOLS_USED_WIDTH, InteractionRecord
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from record import TOOLS_USED_WIDTH, InteractionRecord


TABLE = "ril_interactions"

COLUMNS = (
    "ts",
    "duration_ms",
    "instance",
    "user_id",
    "turn_id",
    "outcome",
    "question",
    "llm_answer",
    "delivered",
    "guard_present",
    "input_verdict",
    "output_verdict",
    "other_plugin_reply",
    "recall_count",
    "recall_top_score",
    "tools_used",
    "tool_input",
    "tool_output",
    "recall_sources",
)

UPDATE_COLUMNS = (
    "duration_ms",
    "turn_id",
    "outcome",
    "llm_answer",
    "delivered",
    "guard_present",
    "input_verdict",
    "output_verdict",
    "other_plugin_reply",
    "recall_count",
    "recall_top_score",
    "tools_used",
    "tool_input",
    "tool_output",
    "recall_sources",
)

TOOL_INPUT_ROW_INDEX = COLUMNS.index("tool_input")
TOOL_INPUT_UPDATE_INDEX = UPDATE_COLUMNS.index("tool_input")
TOOL_OUTPUT_ROW_INDEX = COLUMNS.index("tool_output")
TOOL_OUTPUT_UPDATE_INDEX = UPDATE_COLUMNS.index("tool_output")

INSTANCE_WIDTH = USER_ID_WIDTH = 255
# Longest question, LLM answer or delivered answer kept, in characters. The queue is
# bounded by the number of events, so a cap on their text is what bounds its memory.
TEXT_LIMIT = 20_000
TURN_ID_WIDTH = 32
VERDICT_WIDTH = 64

CREATE_TABLE_SQL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
 id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 ts DATETIME(3) NOT NULL,
 duration_ms INT UNSIGNED NULL,
 instance VARCHAR({INSTANCE_WIDTH}) NOT NULL,
 user_id VARCHAR({USER_ID_WIDTH}) NOT NULL,
 turn_id VARCHAR({TURN_ID_WIDTH}) NULL,
 outcome ENUM('generated','fast_reply','incomplete') NOT NULL,
 question MEDIUMTEXT NULL,
 llm_answer MEDIUMTEXT NULL,
 delivered MEDIUMTEXT NULL,
 guard_present BOOLEAN NOT NULL,
 input_verdict VARCHAR({VERDICT_WIDTH}) NULL,
 output_verdict VARCHAR({VERDICT_WIDTH}) NULL,
 other_plugin_reply BOOLEAN NULL,
 recall_count SMALLINT UNSIGNED NULL,
 recall_top_score FLOAT NULL,
 tools_used VARCHAR({TOOLS_USED_WIDTH}) NULL,
 tool_input MEDIUMTEXT NULL,
 tool_output MEDIUMTEXT NULL,
 recall_sources TEXT NULL,
 KEY idx_ts (ts),
 KEY idx_user_ts (user_id, ts),
 KEY idx_input_verdict (input_verdict),
 KEY idx_output_verdict (output_verdict)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""

INSERT_SQL = (
    f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNS))})"
)

UPDATE_SQL = (
    f"UPDATE {TABLE} SET {', '.join(f'{column} = %s' for column in UPDATE_COLUMNS)} "
    "WHERE id = %s"
)

PURGE_SQL = f"DELETE FROM {TABLE} WHERE ts < %s LIMIT %s"

SELECT_ID_SQL = f"SELECT id FROM {TABLE} WHERE id = %s"
DELETE_ID_SQL = f"DELETE FROM {TABLE} WHERE id = %s"
START_TRANSACTION_SQL = "START TRANSACTION"
ROLLBACK_SQL = "ROLLBACK"
TLS_CIPHER_SQL = "SHOW SESSION STATUS LIKE 'Ssl_cipher'"


def to_row(record: InteractionRecord) -> tuple:
    """Return the values in `COLUMNS` order, sized for their columns.

    Strict SQL mode rejects an over-long value and the whole statement with it,
    so the identifiers are cut to the column width.
    """
    values = {
        "ts": _naive_utc(record.ts),
        "duration_ms": record.duration_ms,
        "instance": record.instance[:INSTANCE_WIDTH],
        "user_id": record.user_id[:USER_ID_WIDTH],
        "turn_id": _fit(record.turn_id, TURN_ID_WIDTH),
        "outcome": record.outcome,
        "question": cut_text(record.question),
        "llm_answer": cut_text(record.llm_answer),
        "delivered": cut_text(record.delivered),
        "guard_present": record.guard_present,
        "input_verdict": _fit(record.input_verdict, VERDICT_WIDTH),
        "output_verdict": _fit(record.output_verdict, VERDICT_WIDTH),
        "other_plugin_reply": record.other_plugin_reply,
        "recall_count": record.recall_count,
        "recall_top_score": record.recall_top_score,
        "tools_used": record.tools_used,
        "tool_input": record.tool_input,
        "tool_output": record.tool_output,
        "recall_sources": record.recall_sources,
    }
    return tuple(values[column] for column in COLUMNS)


def to_update_params(record: InteractionRecord, row_id: int) -> tuple:
    """Return the parameters of `UPDATE_SQL`: the finalisation fields, then the id."""
    row = dict(zip(COLUMNS, to_row(record), strict=True))
    return (*[row[column] for column in UPDATE_COLUMNS], row_id)


def blank_at(values: tuple, index: int) -> tuple:
    """Return `values` with one position emptied, for a column that is not to be saved."""
    return values[:index] + (None,) + values[index + 1 :]


def retention_cutoff(now: datetime, days: int) -> datetime | None:
    """Return the UTC instant before which rows are purged, or None to keep all.

    Whole UTC days: the cut falls on a UTC midnight, `days` days before today's.
    """
    if days <= 0:
        return None
    today = _naive_utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return today - timedelta(days=days)
    except OverflowError:
        # Further back than the calendar goes: no row is that old, so nothing is purged.
        return None


def cut_text(value: str | None, limit: int = TEXT_LIMIT) -> str | None:
    """Keep the first `limit` characters and say how long the text was."""
    if value is None or len(value) <= limit:
        return value
    return f"{value[:limit]}\n[cut: {len(value)} characters in total]"


def _naive_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def _fit(value: str | None, width: int) -> str | None:
    return None if value is None else value[:width]
