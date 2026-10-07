# RAG Interaction Logger

A [Cheshire Cat AI](https://cheshirecat.ai) plugin that records RAG interactions in
an external MySQL or MariaDB table for SQL analysis. It captures questions, generated
and delivered answers, guard verdicts, recalled documents, and invoked tools.

**For testing and staging, not production.** The plugin is an observer: it never
changes a message, reply, hook flow, or another plugin's state. Database writes happen
in a background thread, so a database failure does not slow or break a user turn.

## At a glance

- One row is recorded for each question-and-reply turn.
- `rag-guardrails` is optional enrichment; the logger has no runtime dependency on it.
- Questions, answers, and optional tool texts may contain personal data.

An optional ***WordPress backoffice*** and monitor is available at
<https://github.com/ScuolaNormaleSuperiore/rag-interaction-logger-monitor>.

## Quick start

1. Ask the DBA to prepare the database and logger user; see [database setup](DOC/database.md).
2. In Cheshire Cat AI, activate **RAG Interaction Logger** from **Plugins**.
3. Set `db_host`, `db_port`, `db_name`, `db_user`, and `db_password`, then save.
4. Check the Cat log for `RAG Interaction Logger: Database check passed`.
5. Ask a question and run `SELECT * FROM ril_interactions ORDER BY id DESC LIMIT 1;`.

## Requirements

- Cheshire Cat AI `1.9.2`.
- An external MySQL `8.0`/`8.4` or MariaDB `10.4+` server, reachable from the Cat
  container. The DBA creates databases and users.
- `mysql_native_password` authentication. MySQL 8.4 needs it enabled; MySQL 9 is not
  supported.
- `PyMySQL>=1.1`, installed from `requirements.txt`.

Verified so far: MariaDB `10.4.32`. MySQL `8.0`/`8.4` and other MariaDB versions are
supported by design but have not yet been verified.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `db_host` | empty | Database host. Empty disables the logger. |
| `db_port` | `3306` | TCP port. |
| `db_name` | empty | The dedicated database. |
| `db_user` | empty | The dedicated logger user. |
| `db_password` | empty | Database password. |
| `db_require_ssl` | on | Require TLS; disable only for local development. |
| `log_tool_input` | off | Save the LLM input sent to each tool. |
| `log_tool_output` | off | Save the text returned by each tool. |
| `tool_text_limit` | `1000` | Maximum saved characters per tool input or output. |
| `create_table` | on | Create `ril_interactions` if it is missing. |
| `queue_size` | `1000` | Buffered write events; new events are lost when full. Applies when the plugin is reactivated or the Cat restarts; the other settings apply from the next event. |
| `retention_days` | `0` | `0` keeps rows forever; otherwise delete older whole UTC days. |

## Data, reliability, and operations

The plugin stores turn timing, user and instance identifiers, question and answer text,
Guardrails verdicts, recall metadata, and tool activity. See the concise
[data reference](DOC/data.md) for fields, limits, JSON formats, and privacy notes.

Events are queued in memory and written asynchronously. If the queue is full, the
database is unavailable, or the process stops, some events can be lost; there are no
retries. See [operations and analysis](DOC/operations.md) for connection checks,
diagnostics, and SQL examples.

## Tests

```bash
python .tests/run-tests.py                  # all tests in the Cat container; database tests only when configured
python .tests/run-tests.py --unit           # pure Python tests, no Docker (-u)
python .tests/run-tests.py --integration    # Cat integration tests in the container (-i)
python .tests/run-tests.py --database       # tests against MySQL or MariaDB (-b)
python .tests/run-tests.py --detailed       # list every test; combine with any option above (-d)
```

`--unit`, `--integration` and `--database` are mutually exclusive.

To build the release ZIP: `python .tools/package-plugin.py`.

See the [test suite guide](.tests/README.md) for setup and prerequisites.

## License

GNU General Public License v3.0, see `LICENSE`.
