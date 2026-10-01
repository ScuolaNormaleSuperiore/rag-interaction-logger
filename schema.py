"""Every SQL statement of the logger, written to run unchanged on MySQL and MariaDB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# The Cat imports plugin files as `cat.plugins.<folder>.<module>`, so the
# relative import is the one that works at runtime; the absolute fallback is for
# the tests, which put the plugin folder on the path.
try:
    from .record import InteractionRecord
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from record import InteractionRecord


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
)

INSTANCE_WIDTH = USER_ID_WIDTH = 255
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
        "question": record.question,
        "llm_answer": record.llm_answer,
        "delivered": record.delivered,
        "guard_present": record.guard_present,
        "input_verdict": _fit(record.input_verdict, VERDICT_WIDTH),
        "output_verdict": _fit(record.output_verdict, VERDICT_WIDTH),
        "other_plugin_reply": record.other_plugin_reply,
        "recall_count": record.recall_count,
        "recall_top_score": record.recall_top_score,
    }
    return tuple(values[column] for column in COLUMNS)


def to_update_params(record: InteractionRecord, row_id: int) -> tuple:
    """Return the parameters of `UPDATE_SQL`: the finalisation fields, then the id."""
    row = dict(zip(COLUMNS, to_row(record), strict=True))
    return (*[row[column] for column in UPDATE_COLUMNS], row_id)


def retention_cutoff(now: datetime, days: int) -> datetime | None:
    """Return the UTC instant before which rows are purged, or None to keep all.

    Whole UTC days: the cut falls on a UTC midnight, `days` days before today's.
    """
    if days <= 0:
        return None
    today = _naive_utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days)


def _naive_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def _fit(value: str | None, width: int) -> str | None:
    return None if value is None else value[:width]
