# RAG Interaction Logger

A [Cheshire Cat AI](https://cheshirecat.ai) plugin that records each Cat interaction
in a MySQL or MariaDB table for SQL analysis while a RAG chatbot is being tested.
It captures questions, generated and delivered answers, guard verdicts, recalled
documents and invoked tools.

**For testing and staging, not production.** It optionally enriches records with data
from `rag-guardrails` and `uptime_kuma_connector`, without depending on either.

It only observes. It never changes a message, a reply, the hook flow or another
plugin's state. Rows are written by a background thread, so a database failure never
breaks or slows a user turn.

## At a glance

- Stores one SQL row for each question-and-reply turn, including recall, guard verdicts
  and invoked tools.
- Sends records to an external MySQL or MariaDB database; it does not store them in
  Cheshire Cat AI.
- The recorded questions, answers and optional tool texts may contain personal data.

An optional WordPress backoffice and monitor for the table written by this plugin is
available at
<https://github.com/ScuolaNormaleSuperiore/rag-interaction-logger-monitor>.

## Quick start

1. Have the DBA prepare the database and the user ([Set up the database](#set-up-the-database)).
2. In the admin panel, open **Plugins**, find **RAG Interaction Logger** among the
   available plugins, and activate it.
3. Open the plugin settings, fill `db_host`, `db_port`, `db_name`, `db_user` and
   `db_password`, and save.
4. Read the Cat log: `RAG Interaction Logger: Database check passed` means it works
   ([Check the connection](#check-the-connection)).
5. Ask the Cat a question, then run
   `SELECT * FROM ril_interactions ORDER BY id DESC LIMIT 1;`.

## Requirements

- Cheshire Cat AI `1.9.2`.
- An **external** MySQL `8.0`/`8.4` or MariaDB `10.4+` server, reachable from the Cat
  container. The DBA creates the database and users; the plugin never creates either.
- Authentication must use `mysql_native_password`. MariaDB uses it by default. MySQL
  8.4 requires it to be enabled on the server; MySQL 9 is not supported.
- `PyMySQL>=1.1`, installed by the Cat from `requirements.txt`.


## Set up the database

Ask the DBA for a database and a dedicated logger user. The WordPress monitor user is
needed only if you use the optional monitor. The same commands work on MySQL and
MariaDB:

```sql
CREATE DATABASE `rag-interaction-logger-db`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

With the default `create_table` setting, the plugin creates `ril_interactions` at its
first successful connection. If the DBA manages the schema, set `create_table` off and
create the table before creating the logger user:

```sql
CREATE TABLE IF NOT EXISTS ril_interactions (
 id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 ts DATETIME(3) NOT NULL,
 duration_ms INT UNSIGNED NULL,
 instance VARCHAR(255) NOT NULL,
 user_id VARCHAR(255) NOT NULL,
 turn_id VARCHAR(32) NULL,
 outcome ENUM('generated','fast_reply','incomplete') NOT NULL,
 question MEDIUMTEXT NULL,
 llm_answer MEDIUMTEXT NULL,
 delivered MEDIUMTEXT NULL,
 guard_present BOOLEAN NOT NULL,
 input_verdict VARCHAR(64) NULL,
 output_verdict VARCHAR(64) NULL,
 other_plugin_reply BOOLEAN NULL,
 recall_count SMALLINT UNSIGNED NULL,
 recall_top_score FLOAT NULL,
 tools_used VARCHAR(255) NULL,
 tool_input MEDIUMTEXT NULL,
 tool_output MEDIUMTEXT NULL,
 recall_sources TEXT NULL,
 KEY idx_ts (ts),
 KEY idx_user_ts (user_id, ts),
 KEY idx_input_verdict (input_verdict),
 KEY idx_output_verdict (output_verdict)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

```sql
-- Cheshire Cat AI plugin: create and write ril_interactions.
CREATE USER 'ril_logger'@'%' IDENTIFIED BY '<password>' REQUIRE SSL;
GRANT CREATE, INSERT, UPDATE, SELECT, DELETE
  ON `rag-interaction-logger-db`.* TO 'ril_logger'@'%';
```

If you use the optional WordPress monitor, create its read-only user after the table:

```sql
-- WordPress monitor plugin: read ril_interactions only.
CREATE USER 'ril_monitor'@'%' IDENTIFIED BY '<password>' REQUIRE SSL;
GRANT SELECT
  ON `rag-interaction-logger-db`.ril_interactions TO 'ril_monitor'@'%';

-- Both users must be reported as mysql_native_password.
SELECT user, host, plugin FROM mysql.user WHERE user = 'ril_logger';
SELECT user, host, plugin FROM mysql.user WHERE user = 'ril_monitor';
```

- `CREATE` is needed only when `create_table` is on. `UPDATE` finalizes each initial
  row; `DELETE` serves retention; and `INSERT`, `UPDATE`, `SELECT` and `DELETE` are
  also used by the connection check.
- Drop `REQUIRE SSL` only for a local development server without TLS, and then turn
  `db_require_ssl` off in the plugin settings.

If a `SELECT` above shows a plugin other than `mysql_native_password` (a MySQL server
whose default is `caching_sha2_password`), change it on **MySQL only**; MariaDB rejects
this syntax and already uses `mysql_native_password` by default:

```sql
ALTER USER 'ril_logger'@'%' IDENTIFIED WITH mysql_native_password BY '<password>';
```

## Settings

Open the plugin in the admin panel.

| Setting | Default | Meaning |
| --- | --- | --- |
| `db_host` | empty | Database host. **Empty disables the logger.** |
| `db_port` | `3306` | TCP port. |
| `db_name` | empty | The database created above. |
| `db_user` | empty | The dedicated user. |
| `db_password` | empty | Database password. |
| `db_require_ssl` | on | Require TLS. Turn off only for local development. |
| `log_tool_input` | off | Also save the input the LLM gives to each tool. |
| `log_tool_output` | off | Also save the text each tool returns. |
| `tool_text_limit` | `1000` | Longest tool input or output saved, in characters (100–10000). |
| `create_table` | on | Create `ril_interactions` when it is missing. |
| `queue_size` | `1000` | Events waiting for the writer, 1–10000 (a turn queues two); when full, new events are lost. |
| `retention_days` | `0` | `0` keeps rows forever; a positive number (at most 3650) deletes rows older than that many whole UTC days. |

## Check the connection

The admin panel has no "test" button, so the plugin tests the connection **every time
you save the settings** and writes the result in the Cat log (for example
`docker logs <container>`). Look for lines starting with `RAG Interaction Logger:`.

```
RAG Interaction Logger: Database check passed for db.example.org:3306: connect, TLS, table, write
RAG Interaction Logger: Database check failed at write: OperationalError(1142) db.example.org:3306
```

The check has four stages: `connect` (reach the server and log in), `tls`, `table`
(create it if `create_table` is on) and `write`, which inserts, updates, reads and
deletes a probe row inside a transaction that is rolled back, so it leaves nothing
behind. A missing privilege therefore shows up when you save and not on the first
real turn. The message holds only the error class, the numeric code, the host and the
port, never the password or the driver's text. Common codes:

| Code | Meaning |
| --- | --- |
| `1045` | Wrong user or password |
| `1044`, `1142` | A privilege is missing (for example `UPDATE`) |
| `1049` | The database does not exist |
| `1054` | A column is missing: the table differs from the one in [The table](#the-table); add the missing column |
| `1146` | The table does not exist and `create_table` is off |
| `2003` | The server cannot be reached |
| `2026` | TLS is required but the server does not offer it |

With `db_require_ssl` off, the log also shows `TLS is not required for the database
connection to <host:port>` each time the writer connects: it is only a reminder.

## What is recorded

One row per turn in `ril_interactions` (a turn is a question and its reply; the Cat has
no sessions):

- when it arrived (UTC), how long it took, the Cat instance and the user;
- the question, the answer the LLM generated and the answer actually delivered, each
  cut at 20,000 characters and ending with `[cut: N characters in total]` when longer, so
  that a huge message cannot fill the memory of the queue;
- the outcome: `generated`, `fast_reply` (a plugin answered before the LLM) or
  `incomplete` (the turn started and never finished, for example the agent failed);
- when the `rag-guardrails` plugin is installed, its
  verdicts on the input and the output; without it those columns stay empty;
- how many documents the recall found, the best score, and for each document its id,
  source and score (never its text);
- the names of the tools and forms that ran (`tools_used`), and, only if you turn the
  options on, their inputs (`tool_input`) and results (`tool_output`).

`tool_input` and `tool_output` are JSON arrays, with at most ten objects such as
`{"tool":"get_time","input":"Europe/Rome"}`. A value longer than `tool_text_limit`
is shortened before it is stored and includes `"cut":true` and its original `"chars"`.

### The table

`ril_interactions` has one row per turn. `id` is generated by the database; the other
columns are:

| Column | Meaning |
| --- | --- |
| `ts` | When the question arrived (UTC, milliseconds). |
| `duration_ms` | How long the turn took; empty while `incomplete`. |
| `instance` | Host name of the Cat that handled the turn. |
| `user_id` | The Cat user. |
| `turn_id` | The `rag-guardrails` turn id if present, otherwise a random id. |
| `outcome` | `generated`, `fast_reply` or `incomplete`. |
| `question` | What the user typed. |
| `llm_answer` | What the LLM generated; empty for `fast_reply`. |
| `delivered` | What the user received. |
| `guard_present` | `1` when `rag-guardrails` handled the turn. |
| `input_verdict`, `output_verdict` | Verdicts of `rag-guardrails`; empty without it. |
| `other_plugin_reply` | `1` when a plugin other than the guard answered before the LLM. |
| `recall_count`, `recall_top_score` | Documents recalled and the best score. |
| `tools_used` | Comma-separated names of the tools and forms that ran. |
| `tool_input`, `tool_output` | JSON arrays, only with the options on. |
| `recall_sources` | JSON array of `{id, source, score}`, never the text. |

### Personal data

Rows contain what users type and what the Cat answers, so they may contain personal
data. Decide who can read the table, how it is backed up and on what legal basis it
is kept before using it beyond testing, and use `retention_days` to limit how long
rows stay. Tool inputs and results are **off by default** because no guard checks
them: the input is text the LLM built and the result comes from systems this plugin
does not know. Turn them on only when you need them.

## When the database is down

Hooks only put an event on a bounded in-memory queue; a background thread does all the
database work. If the database is slow, unreachable or the queue is full, the user turn
is not affected and the event is lost. The logger writes one warning when loss starts
and one when writes recover. Events still queued when the Cat stops are lost. There are
no retries.

## Analysing the data
A few starting points (all plain SQL for MySQL and MariaDB):

```sql
-- blocked turns by stage and verdict
SELECT 'input' AS stage, input_verdict AS verdict, COUNT(*) AS n
FROM ril_interactions WHERE input_verdict IS NOT NULL GROUP BY input_verdict
UNION ALL
SELECT 'output', output_verdict, COUNT(*)
FROM ril_interactions WHERE output_verdict IS NOT NULL GROUP BY output_verdict;

-- share of generated answers that had no recalled document
SELECT ROUND(100 * AVG(recall_count = 0), 1) AS empty_recall_pct
FROM ril_interactions WHERE outcome = 'generated';

-- turns that used a given tool
SELECT ts, question, tools_used FROM ril_interactions
WHERE FIND_IN_SET('service_status', tools_used) > 0 ORDER BY ts DESC;

-- turns that never finished, and turns that ran without the guard
SELECT outcome, guard_present, COUNT(*) FROM ril_interactions
WHERE outcome = 'incomplete' OR NOT guard_present GROUP BY outcome, guard_present;
```

## Tests

```bash
python run-tests.py              # unit, integration and, if configured, database tests
python run-tests.py --unit       # pure Python, no Cheshire Cat needed
python run-tests.py --database   # only the tests against a real MySQL or MariaDB server
```

Integration tests need the running Cat container. Database tests need a server and a user
that can create throwaway databases: copy `.ril-test.env.example` to `.ril-test.env`
(git-ignored) and fill it in. Without it, the runner skips database tests and says so.
For the complete setup and step-by-step commands, see the [test suite guide](.tests/README.md).

## License

GNU General Public License v3.0, see `LICENSE`.
