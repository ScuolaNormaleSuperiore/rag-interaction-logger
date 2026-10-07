# Testing RAG Interaction Logger

This guide describes how to run the test suite after cloning
[`rag-interaction-logger`](https://github.com/ScuolaNormaleSuperiore/rag-interaction-logger).
The tests are split into three scopes:

| Scope | Directory | What it needs |
| --- | --- | --- |
| Unit | `.tests/unit` | Python and `pytest` |
| Integration | `.tests/integration` | A running Cheshire Cat AI 1.9.2 Docker container |
| Database | `.tests/database` | The running Cat container and a disposable MySQL or MariaDB server |

Run every command from the plugin root, unless stated otherwise. Use the same Python
interpreter for installing dependencies and running the tests.

## 1. Get the source

For unit tests, clone the repository anywhere:

```bash
git clone https://github.com/ScuolaNormaleSuperiore/rag-interaction-logger.git
cd rag-interaction-logger
```

Integration and database tests run inside the Cheshire Cat container. Clone the plugin
in the Cat core checkout at `cat/plugins/rag-interaction-logger`, so that
`.tests/run-tests.py` can find the core `compose.yml` and the container can load the plugin.
Use Cheshire Cat AI 1.9.2.

## 2. Prepare the local Python environment

Create and activate a virtual environment, then install the runtime dependency and
`pytest`:

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS or Linux
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pytest
```

On Windows, use `py -3` in place of `python` if that is how Python is installed.

## 3. Run unit tests

Unit tests exercise pure Python code and do not need Docker, Cheshire Cat, or a
database:

```bash
python .tests/run-tests.py --unit
```

For individual test names and full pytest output:

```bash
python .tests/run-tests.py --unit --detailed
```

## 4. Run integration tests

Integration tests load the plugin in a running Cheshire Cat container and verify hook
wiring, priorities, defaults, and its observer contract.

1. Place the clone in the Cheshire Cat core layout described in [Get the source](#1-get-the-source).
2. Install and start Cheshire Cat AI 1.9.2 from the core checkout. Its Compose service
   must be named `cheshire-cat-core`.
3. Confirm that the container is running:

   ```bash
   docker compose ps cheshire-cat-core
   ```

4. Return to the plugin root and run:

   ```bash
   python .tests/run-tests.py --integration
   ```

Add `--detailed` for verbose test output. If Docker Compose or the container is not
available, the runner stops with an actionable error; unit tests still work locally.

## 5. Run database tests

Database tests run the plugin against a real MySQL or MariaDB server. They create and
drop uniquely named `ril_test_*` databases and temporary users; they never use the
application database. Use a dedicated disposable server or an account explicitly
authorized to create and drop databases and users.

1. Complete the integration-test setup and leave `cheshire-cat-core` running.
2. Ensure the database server is reachable **from inside the Cat container**. For a
   database running on the Docker host, the usual host name is `host.docker.internal`.
3. Copy the untracked configuration template:

   ```bash
   # Windows PowerShell
   Copy-Item .ril-test.env.example .ril-test.env
   # macOS or Linux
   cp .ril-test.env.example .ril-test.env
   ```

4. Edit `.ril-test.env` with the test server and an administrative account:

   ```text
   RIL_TEST_DB_HOST=host.docker.internal
   RIL_TEST_DB_PORT=3306
   RIL_TEST_DB_USER=<administrator>
   RIL_TEST_DB_PASSWORD=<password>
   RIL_TEST_DB_REQUIRE_SSL=false
   RIL_TEST_DB_TLS=
   ```

   Set `RIL_TEST_DB_REQUIRE_SSL=true` only when the server offers TLS. Set
   `RIL_TEST_DB_TLS=yes` or `no` to run the TLS behaviour test; leave it empty to skip
   that one test. Environment variables with the same names override the file.

5. Run the database suite:

   ```bash
   python .tests/run-tests.py --database
   ```

Never commit `.ril-test.env` or put its credentials in shell history, documentation,
or logs.

## Run the usual suite

Without a scope option, the runner executes integration tests in the running Cat
container and also includes database tests when `RIL_TEST_DB_HOST` and
`RIL_TEST_DB_USER` are configured. Otherwise it explicitly reports that database tests
were skipped:

```bash
python .tests/run-tests.py
```

## Why one guide

Keep this single README for now. The three suites share the same runner, repository
layout, and escalation path from unit tests to container and database tests. A README
in each subdirectory would repeat prerequisites and risks without helping a developer
choose the right command. Add a local README only if a suite gains independent setup or
maintenance instructions.
