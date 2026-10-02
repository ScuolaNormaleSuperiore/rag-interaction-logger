# RAG Interaction Logger

A [Cheshire Cat AI](https://cheshirecat.ai) plugin that records every interaction
of a Cat instance (the question, the answer the LLM generated, the answer the user
received, whether a guard blocked it, which documents were recalled and which tools
ran) in a MySQL or MariaDB table, so it can be analysed with SQL while a RAG chatbot
is being tested.

It only observes. It never changes a message, a reply, the hook flow or another
plugin's state, and a database failure never breaks or slows a user turn: rows are
written by a background thread.

A WordPress plugin that works as backoffice and monitor for the table this plugin
writes is available at
<https://github.com/ScuolaNormaleSuperiore/rag-interaction-logger-monitor>.

## Requirements

- Cheshire Cat AI `1.9.2`.
- An **external** MySQL `8.0`/`8.4` or MariaDB `10.4+` server, reachable from the Cat
  container. The database is created by hand; the plugin does not create databases or
  users. Authentication must be `mysql_native_password`.
- `PyMySQL>=1.1`, installed by the Cat from `requirements.txt`.

Verified so far: MariaDB `10.4.32`. MySQL `8.0`/`8.4` and other MariaDB versions are
supported by design (the SQL is portable) but have **not** been verified yet. MySQL `9`
is not supported (it removed `mysql_native_password`).

## Set up the database

Ask the DBA for a database and a dedicated user with the minimum privileges. The same
script works on MySQL and MariaDB:

```sql
CREATE DATABASE ril CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'ril_logger'@'%' IDENTIFIED BY '<password>' REQUIRE SSL;
GRANT CREATE, INSERT, UPDATE, SELECT, DELETE ON ril.* TO 'ril_logger'@'%';
-- the plugin must be reported as mysql_native_password
SELECT user, host, plugin FROM mysql.user WHERE user = 'ril_logger';
```

- `CREATE` lets the plugin create the table `ril_interactions` when it is missing. With
  `create_table` turned off the DBA creates it and `CREATE` is not needed.
- `UPDATE` is needed because each turn is written twice: a row is inserted when the
  question arrives and updated when the turn ends. `DELETE` and `SELECT` serve the
  retention purge.
- Drop `REQUIRE SSL` only for a local development server without TLS, and then turn
  `db_require_ssl` off in the plugin settings.

## Settings

Open the plugin in the admin panel.

| Setting | Default | Meaning |
| --- | --- | --- |
| `db_host` | empty | Database host. **Empty disables the logger.** |
| `db_port` | `3306` | TCP port. |
| `db_name` | empty | The database created above. |
| `db_user` | empty | The dedicated user. |
| `db_password` | empty | Its password. |
| `db_require_ssl` | on | Require TLS. Turn off only for local development. |
| `log_tool_input` | off | Also save the input the LLM gives to each tool. |
| `log_tool_output` | off | Also save the text each tool returns. |
| `tool_text_limit` | `1000` | Longest tool input or output saved, in characters (100–10000). |
| `create_table` | on | Create `ril_interactions` when it is missing. |
| `queue_size` | `1000` | Events waiting for the writer; when full, new events are lost. |
| `retention_days` | `0` | `0` keeps rows forever; a positive number deletes rows older than that many whole UTC days. |

> **The password is saved in plain text in `settings.json`**, inside the plugin folder
> (the admin panel only masks it on screen, and its settings API returns it). Protect
> that file and do not commit it. TLS is encrypted but the server certificate is not
> verified.

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
| `1054` | A column is missing: the table predates a plugin update, see below |
| `1146` | The table does not exist and `create_table` is off |
| `2003` | The server cannot be reached |
| `2026` | TLS is required but the server does not offer it |

The same line `TLS is not required for the database connection` is only a reminder that
`db_require_ssl` is off.

## What is recorded

One row per turn in `ril_interactions` (a turn is a question and its reply; the Cat has
no sessions):

- when it arrived (UTC), how long it took, the Cat instance and the user;
- the question, the answer the LLM generated and the answer actually delivered;
- the outcome: `generated`, `fast_reply` (a plugin answered before the LLM) or
  `incomplete` (the turn started and never finished, for example the agent failed);
- when the `rag-guardrails` plugin is installed, its
  verdicts on the input and the output; without it those columns stay empty;
- how many documents the recall found, the best score, and for each document its id,
  source and score (never its text);
- the names of the tools and forms that ran (`tools_used`), and, only if you turn the
  options on, their inputs (`tool_input`) and results (`tool_output`).

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
is not affected: the event is lost and a single warning is logged (and one line when
writes recover). Events still queued when the Cat stops are lost. There are no retries.

## Updating an existing table

`CREATE TABLE IF NOT EXISTS` never adds columns. If the table was created by an earlier
version, add the missing ones once (valid on MySQL and MariaDB); until then inserts fail
with code `1054` and the check on save reports it:

```sql
ALTER TABLE ril_interactions
  ADD COLUMN tools_used VARCHAR(255) NULL AFTER recall_top_score,
  ADD COLUMN tool_input MEDIUMTEXT NULL AFTER tools_used,
  ADD COLUMN tool_output MEDIUMTEXT NULL AFTER tool_input,
  ADD COLUMN recall_sources TEXT NULL AFTER tool_output;
```

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

The test suite is split by runtime dependency:

```bash
python run-tests.py --unit          # pure Python; no Cheshire Cat required
python run-tests.py --integration   # hook adapters; requires the running Cat container
python run-tests.py                 # unit and integration tests in the container
python run-tests.py --detailed      # same suite, listing each test
python run-tests.py --database      # only the tests against a real MySQL or MariaDB server
```

The tests live in `.tests/`; the name starts with a dot on purpose, so that Cheshire Cat,
which imports every `.py` file in a plugin folder, does not import them.
Unit tests cover pure record assembly, metadata and release packaging.
Integration tests cover Cheshire Cat hook registration, priorities and the
observer contract. Database tests need a server and a user that can create databases (they work in
throwaway databases and never touch the application database). Describe it once:
copy `.ril-test.env.example` to `.ril-test.env` (git-ignored) and fill it in; the runner
reads it, and `python run-tests.py` then runs the database tests too. The same
`RIL_TEST_DB_*` variables in the environment work and win over the file. Without a server
the database tests are skipped, and the runner says so. The integration
suite is skipped on a local interpreter where Cheshire Cat is unavailable.

## License

GNU General Public License v3.0, see `LICENSE`.
