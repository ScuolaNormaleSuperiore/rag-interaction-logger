"""Tests for the queue and worker; these need neither Cheshire Cat nor PyMySQL."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import writer as writer_module
from record import capture_generated, finalize_fast_reply, finalize_generated, resolve_turn, start_record
from schema import COLUMNS, INSERT_SQL, PURGE_SQL, UPDATE_COLUMNS, UPDATE_SQL
from writer import Writer


LEAK_MARKER = "s3cr3t-pa55"
DAY = 24 * 3600
SETTINGS = {
    "db_host": "db.example.org",
    "db_port": 3306,
    "db_name": "ril",
    "db_user": "ril_logger",
    "db_password": LEAK_MARKER,
    "db_require_ssl": True,
    "create_table": False,
    "queue_size": 1000,
    "retention_days": 0,
    "log_tool_input": False,
    "log_tool_output": False,
    "tool_text_limit": 1000,
}


class OperationalError(Exception):
    """Stands in for the driver error: a numeric code first, then free text."""


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.lastrowid = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.connection.statements.append((sql, params))
        failure = self.connection.fail_on.get(sql.split()[0])
        if failure is not None:
            raise failure
        if sql == INSERT_SQL:
            self.connection.next_id += 1
            self.lastrowid = self.connection.next_id
        if sql == PURGE_SQL:
            self.rowcount = self.connection.purge_results.pop(0) if self.connection.purge_results else 0

    def fetchone(self):
        return ("Ssl_cipher", "TLS_AES_256_GCM_SHA384")

    def close(self):
        pass


class FakeConnection:
    def __init__(self, fail_on=None):
        self.statements = []
        self.fail_on = fail_on or {}
        self.next_id = 100
        self.purge_results = []
        self.pings = 0
        self.ping_fails = False
        self.closed_in = []

    def cursor(self):
        return FakeCursor(self)

    def ping(self, reconnect=False):
        assert reconnect is False, "the deprecated reconnect argument must not be used"
        self.pings += 1
        if self.ping_fails:
            raise OperationalError(2006, "server has gone away")

    def close(self):
        self.closed_in.append(threading.current_thread().name)

    def of(self, sql):
        return [params for statement, params in self.statements if statement == sql]


class Log:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("info", message))

    def warning(self, message):
        self.lines.append(("warning", message))

    def text(self, level=None):
        return [message for kind, message in self.lines if level in (None, kind)]


class Harness:
    def __init__(self, settings=None, connection=None):
        self.settings = {**SETTINGS, **(settings or {})}
        self.connection = connection or FakeConnection()
        self.connections = [self.connection]
        self.connect_calls = []
        self.log = Log()
        self.now = 1000.0
        self.utc = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)
        self.writer = Writer(
            load_settings=lambda: dict(self.settings),
            log=self.log,
            connect=self._connect,
            clock=lambda: self.now,
            utcnow=lambda: self.utc,
        )
        self.writer.prepare()

    def _connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        return self.connections[min(len(self.connect_calls), len(self.connections)) - 1]

    def pump(self, steps=40):
        for _ in range(steps):
            self.writer.step(timeout=0)


def turn(local_id="a" * 32, guard="AB12", user="u1"):
    record = start_record(
        ts=datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc),
        started_ns=0,
        instance="pod",
        user_id=user,
        question=f"question {local_id[:4]}",
        guard_turn_id_at_start=None,
        local_turn_id=local_id,
    )
    return resolve_turn(record, guard)


def finished(record, answer="answer"):
    return finalize_generated(record, answer, None, 5_000_000)


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(writer_module, "POLL_SECONDS", 0.02)


@pytest.fixture
def harness():
    return Harness()


def test_start_then_finish_updates_the_row_the_start_inserted(harness):
    record = turn()
    harness.writer.submit_start(record)
    harness.writer.submit_finish(finished(record))

    harness.pump()

    connection = harness.connection
    assert len(connection.of(INSERT_SQL)) == 1
    (update,) = connection.of(UPDATE_SQL)
    assert update[-1] == 101
    assert "generated" in update and "answer" in update
    assert harness.writer.pending_count == 0


def test_a_start_without_a_finish_leaves_the_incomplete_row(harness):
    harness.writer.submit_start(turn())

    harness.pump()

    (row,) = harness.connection.of(INSERT_SQL)
    assert "incomplete" in row
    assert harness.connection.of(UPDATE_SQL) == []
    assert harness.writer.pending_count == 1


def test_a_failed_start_followed_by_a_finish_inserts_the_complete_row(harness):
    record = turn()
    harness.connection.fail_on = {"INSERT": OperationalError(2013, "lost")}
    harness.writer.submit_start(record)
    harness.pump()
    harness.connection.fail_on = {}
    harness.writer.submit_finish(finished(record))

    harness.pump()

    complete = harness.connection.of(INSERT_SQL)[-1]
    assert "generated" in complete and "answer" in complete
    assert harness.connection.of(UPDATE_SQL) == []


def test_fast_reply_turns_are_finalised_the_same_way(harness):
    record = turn()
    harness.writer.submit_start(record)
    harness.writer.submit_finish(finalize_fast_reply(record, "Request refused.", "prompt_injection", 1_000_000))

    harness.pump()

    (update,) = harness.connection.of(UPDATE_SQL)
    assert "fast_reply" in update and "prompt_injection" in update


def test_events_are_written_in_the_order_they_were_submitted(harness):
    first, second = turn("1" * 32), turn("2" * 32)
    for record in (first, second):
        harness.writer.submit_start(record)
    harness.writer.submit_finish(finished(second, "second"))
    harness.writer.submit_finish(finished(first, "first"))

    harness.pump()

    order = [params[-1] for params in harness.connection.of(UPDATE_SQL)]
    assert order == [102, 101]


def test_a_failed_update_is_not_retried_and_the_connection_is_reset(harness):
    record = turn()
    harness.writer.submit_start(record)
    harness.pump()
    harness.connection.fail_on = {"UPDATE": OperationalError(1142, f"denied {LEAK_MARKER}")}
    harness.writer.submit_finish(finished(record))

    harness.pump()

    assert len(harness.connection.of(UPDATE_SQL)) == 1
    assert harness.connection.closed_in
    assert harness.writer.pending_count == 0


def test_the_same_failure_is_logged_once_with_the_turn_and_no_secret(harness):
    harness.connection.fail_on = {"INSERT": OperationalError(1142, f"denied {LEAK_MARKER}")}
    first, second = turn("1" * 32), turn("2" * 32)
    for record in (first, second, first, second):
        harness.writer.submit_start(record)

    harness.pump()

    warnings = harness.log.text("warning")
    assert len(warnings) == 1
    assert f"turn={first.turn_id}" in warnings[0]
    assert "insert" in warnings[0] and "OperationalError(1142) db.example.org:3306" in warnings[0]
    assert all(LEAK_MARKER not in line for line in harness.log.text())


def test_recovery_after_a_failure_is_logged_once(harness):
    harness.connection.fail_on = {"INSERT": OperationalError(2013, "lost")}
    harness.writer.submit_start(turn("1" * 32))
    harness.pump()
    harness.connection.fail_on = {}
    harness.writer.submit_start(turn("2" * 32))
    harness.writer.submit_start(turn("3" * 32))

    harness.pump()

    infos = harness.log.text("info")
    assert len([line for line in infos if "recovered" in line]) == 1


def test_a_changed_configuration_reconnects_without_restarting(harness):
    second_connection = FakeConnection()
    harness.connections.append(second_connection)
    harness.writer.submit_start(turn("1" * 32))
    harness.pump()

    harness.settings["db_host"] = "other.example.org"
    harness.writer.submit_start(turn("2" * 32))
    harness.pump()

    assert [call["host"] for call in harness.connect_calls] == ["db.example.org", "other.example.org"]
    assert harness.connection.closed_in
    assert len(second_connection.of(INSERT_SQL)) == 1


TOOL_STEPS = [(("get_time", "Europe/Rome"), "tool output")]


def with_tool(record):
    return capture_generated(record, "answer", [], TOOL_STEPS)


def tool_input_of_insert(connection, index=-1):
    return connection.of(INSERT_SQL)[index][COLUMNS.index("tool_input")]


def tool_input_of_update(connection):
    return connection.of(UPDATE_SQL)[0][UPDATE_COLUMNS.index("tool_input")]


def test_the_tool_name_is_always_saved_and_the_input_only_when_asked():
    off, on = Harness(), Harness({"log_tool_input": True})
    for harness in (off, on):
        record = with_tool(turn())
        harness.writer.submit_start(turn())
        harness.writer.submit_finish(finished(record))
        harness.pump()

    for harness in (off, on):
        (update,) = harness.connection.of(UPDATE_SQL)
        assert update[UPDATE_COLUMNS.index("tools_used")] == "get_time"
    assert tool_input_of_update(off.connection) is None
    assert '"input":"Europe/Rome"' in tool_input_of_update(on.connection)
    assert "tool output" not in tool_input_of_update(on.connection)


def test_the_input_is_dropped_from_a_full_insert_too_unless_asked():
    off, on = Harness(), Harness({"log_tool_input": True})
    for harness in (off, on):
        harness.writer.submit_finish(finished(with_tool(turn())))
        harness.pump()

    assert tool_input_of_insert(off.connection) is None
    assert '"tool":"get_time"' in tool_input_of_insert(on.connection)


def test_the_option_is_read_when_the_row_is_written_so_a_change_applies_at_once():
    harness = Harness()
    first = with_tool(turn("1" * 32))
    harness.writer.submit_finish(finished(first))
    harness.pump()
    harness.settings["log_tool_input"] = True
    second = with_tool(turn("2" * 32))
    harness.writer.submit_finish(finished(second))
    harness.pump()

    assert tool_input_of_insert(harness.connection, 0) is None
    assert tool_input_of_insert(harness.connection, 1) is not None


def column_of_update(connection, column):
    return connection.of(UPDATE_SQL)[0][UPDATE_COLUMNS.index(column)]


def test_the_tool_output_is_saved_only_when_asked_independently_of_the_input():
    cases = {
        (False, False): (None, None),
        (True, False): ("input", None),
        (False, True): (None, "output"),
        (True, True): ("input", "output"),
    }
    for (log_input, log_output), (want_input, want_output) in cases.items():
        harness = Harness({"log_tool_input": log_input, "log_tool_output": log_output})
        record = with_tool(turn())
        harness.writer.submit_start(turn())
        harness.writer.submit_finish(finished(record))
        harness.pump()

        got_input = column_of_update(harness.connection, "tool_input")
        got_output = column_of_update(harness.connection, "tool_output")
        assert (got_input is not None, got_output is not None) == (want_input is not None, want_output is not None)
        if got_output:
            assert '"output":"tool output"' in got_output and "Europe/Rome" not in got_output


def test_the_tool_output_is_dropped_from_a_full_insert_too_unless_asked():
    off, on = Harness(), Harness({"log_tool_output": True})
    for harness in (off, on):
        harness.writer.submit_finish(finished(with_tool(turn())))
        harness.pump()

    assert off.connection.of(INSERT_SQL)[-1][COLUMNS.index("tool_output")] is None
    assert '"tool":"get_time"' in on.connection.of(INSERT_SQL)[-1][COLUMNS.index("tool_output")]


def test_the_recall_sources_are_always_saved():
    harness = Harness()
    record = capture_generated(turn(), "a", [{"id": "p1", "score": 0.8, "metadata": {"source": "guide.pdf"}}])
    harness.writer.submit_finish(finished(record))
    harness.pump()

    assert '"source":"guide.pdf"' in harness.connection.of(INSERT_SQL)[-1][COLUMNS.index("recall_sources")]


def test_the_tool_text_limit_follows_the_settings_the_worker_reads():
    harness = Harness()
    assert harness.writer.tool_text_limit == 1000

    harness.settings["tool_text_limit"] = 250
    harness.writer.submit_start(turn("1" * 32))
    harness.pump()
    assert harness.writer.tool_text_limit == 250

    for bad, expected in ((5, 100), (999999, 10000), ("abc", 1000), (None, 1000)):
        harness.settings["tool_text_limit"] = bad
        harness.writer.submit_start(turn("2" * 32))
        harness.pump()
        assert harness.writer.tool_text_limit == expected


def test_the_tool_text_limit_is_known_before_the_first_event():
    assert Harness({"tool_text_limit": 400}).writer.tool_text_limit == 400


def test_the_connection_is_reused_and_pinged_before_each_operation(harness):
    for index in range(3):
        harness.writer.submit_start(turn(str(index) * 32))

    harness.pump()

    assert len(harness.connect_calls) == 1
    assert harness.connection.pings == 2


def test_a_dropped_connection_is_replaced_without_losing_the_event():
    first, second = FakeConnection(), FakeConnection()
    harness = Harness(connection=first)
    harness.connections.append(second)
    harness.writer.submit_start(turn("1" * 32))
    harness.pump()
    first.ping_fails = True

    harness.writer.submit_start(turn("2" * 32))
    harness.pump()

    assert len(harness.connect_calls) == 2
    assert first.closed_in and len(first.of(INSERT_SQL)) == 1
    assert len(second.of(INSERT_SQL)) == 1
    assert not harness.log.text("warning")


def test_a_disabled_logger_drops_events_and_says_so_once(harness):
    harness.settings["db_host"] = ""
    for index in range(3):
        harness.writer.submit_start(turn(str(index) * 32))

    harness.pump()

    assert harness.connect_calls == []
    assert len([line for line in harness.log.text("info") if "disabled" in line]) == 1


def test_a_full_queue_loses_events_and_reports_the_episode_once():
    full = Harness({"queue_size": 2})
    for index in range(5):
        full.writer.submit_start(turn(str(index) * 32))

    full.writer.step(timeout=0)
    first_warnings = full.log.text("warning")
    full.pump()

    assert len(first_warnings) == 1 and "queue is full" in first_warnings[0]
    closing = full.log.text("warning")[1]
    assert "3 events" in closing
    assert len(full.log.text("warning")) == 2
    assert len(full.connection.of(INSERT_SQL)) == 2


def test_submit_never_raises_before_start_after_stop_or_for_a_malformed_record():
    harness = Harness()
    unstarted = Writer(load_settings=dict, log=Log())

    unstarted.submit_start(turn())
    unstarted.submit_finish(finished(turn()))
    harness.writer.submit_start(object())
    harness.writer.stop()
    harness.writer.submit_start(turn())


def test_a_malformed_record_is_reported_by_the_worker_without_data(harness):
    harness.writer.submit_start(object())

    harness.pump()

    warnings = harness.log.text("warning")
    assert len(warnings) == 1 and "AttributeError" in warnings[0]


def test_correlations_of_turns_that_never_finished_expire_after_a_day(harness):
    harness.writer.submit_start(turn("1" * 32))
    harness.pump()
    assert harness.writer.pending_count == 1

    harness.now += DAY + 120
    harness.writer.submit_start(turn("2" * 32))
    harness.pump()

    assert harness.writer.pending_count == 1


def test_retention_zero_never_purges(harness):
    harness.now += 10 * DAY
    harness.pump()

    assert harness.connect_calls == []
    assert harness.connection.of(PURGE_SQL) == []


def test_purge_uses_the_utc_day_cutoff_and_runs_once_a_day():
    harness = Harness({"retention_days": 7})
    harness.connection.purge_results = [10]

    harness.pump()
    harness.now += 3600
    harness.pump()

    (batch,) = harness.connection.of(PURGE_SQL)
    assert batch == (datetime(2026, 10, 3), 1000)
    harness.now += DAY
    harness.pump()
    assert len(harness.connection.of(PURGE_SQL)) == 2


def test_purge_runs_in_bounded_slices_interleaved_with_inserts(monkeypatch):
    monkeypatch.setattr(writer_module, "PURGE_BATCHES_PER_CYCLE", 2)
    harness = Harness({"retention_days": 7})
    harness.connection.purge_results = [1000, 1000, 1000, 1000, 1000, 3]
    harness.writer.submit_start(turn("1" * 32))
    harness.writer.submit_start(turn("2" * 32))

    harness.writer.step(timeout=0)
    assert len(harness.connection.of(INSERT_SQL)) == 1
    assert len(harness.connection.of(PURGE_SQL)) == 2
    harness.writer.step(timeout=0)
    assert len(harness.connection.of(INSERT_SQL)) == 2
    assert len(harness.connection.of(PURGE_SQL)) == 4
    harness.pump()

    assert len(harness.connection.of(PURGE_SQL)) == 6
    harness.pump()
    assert len(harness.connection.of(PURGE_SQL)) == 6


def test_a_failed_purge_does_not_block_inserts_and_waits_a_day_to_retry():
    harness = Harness({"retention_days": 7})
    harness.connection.fail_on = {"DELETE": OperationalError(1142, f"denied {LEAK_MARKER}")}
    harness.writer.submit_start(turn("1" * 32))

    harness.pump()

    assert len(harness.connection.of(INSERT_SQL)) == 1
    assert len(harness.connection.of(PURGE_SQL)) == 1
    warnings = harness.log.text("warning")
    assert len(warnings) == 1 and "purge" in warnings[0]
    assert all(LEAK_MARKER not in line for line in harness.log.text())
    harness.now += 3600
    harness.pump()
    assert len(harness.connection.of(PURGE_SQL)) == 1
    harness.now += DAY
    harness.connection.fail_on = {}
    harness.pump()
    assert len(harness.connection.of(PURGE_SQL)) == 2


def test_returning_retention_to_zero_stops_a_running_purge(monkeypatch):
    monkeypatch.setattr(writer_module, "PURGE_BATCHES_PER_CYCLE", 1)
    harness = Harness({"retention_days": 7})
    harness.connection.purge_results = [1000] * 10

    harness.writer.step(timeout=0)
    harness.settings["retention_days"] = 0
    harness.pump()

    assert len(harness.connection.of(PURGE_SQL)) == 1


def test_queue_size_is_read_when_the_worker_starts():
    harness = Harness({"queue_size": 7})

    assert harness.writer.queue_capacity == 7


def test_queue_size_is_capped_even_if_the_saved_value_is_larger():
    harness = Harness({"queue_size": 10**9})

    assert harness.writer.queue_capacity == writer_module.MAX_QUEUE_SIZE


def test_a_retention_older_than_the_calendar_does_not_stop_the_inserts():
    harness = Harness({"retention_days": 10**9})
    harness.writer.submit_start(turn())

    harness.pump()

    assert len(harness.connection.of(INSERT_SQL)) == 1
    assert harness.connection.of(PURGE_SQL) == []


def run_threaded(settings=None, connect=None):
    log = Log()
    connection = FakeConnection()
    writer = Writer(
        load_settings=lambda: {**SETTINGS, **(settings or {})},
        log=log,
        connect=connect or (lambda **kwargs: connection),
    )
    return writer, connection, log


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_start_is_idempotent_and_stop_lets_the_worker_close_its_own_connection():
    writer, connection, _ = run_threaded()

    assert writer.start() is True
    assert writer.start() is False
    assert len([t for t in threading.enumerate() if t.name == "ril-writer"]) == 1
    writer.submit_start(turn())
    assert wait_for(lambda: connection.of(INSERT_SQL))

    writer.stop()

    assert connection.closed_in == ["ril-writer"]
    assert not [t for t in threading.enumerate() if t.name == "ril-writer"]
    assert writer.start() is True
    writer.stop()


def test_submitting_stays_instant_while_the_database_is_unreachable():
    release = threading.Event()

    def stuck(**kwargs):
        release.wait(10)
        raise OperationalError(2003, "unreachable")

    writer, _, _ = run_threaded(connect=stuck)
    writer.start()
    try:
        began = time.monotonic()
        for index in range(50):
            writer.submit_start(turn(str(index % 10) * 32))
        assert time.monotonic() - began < 0.5
    finally:
        release.set()
        writer.stop()


def ril_threads():
    return [t for t in threading.enumerate() if t.name == "ril-writer"]


def test_a_start_while_a_stopped_worker_is_still_busy_is_taken_over_when_it_ends():
    release = threading.Event()
    connections = []

    def stuck(**kwargs):
        release.wait(10)
        connections.append(FakeConnection())
        return connections[-1]

    writer, _, _ = run_threaded(connect=stuck)
    writer.start()
    writer.submit_start(turn())
    time.sleep(0.1)

    writer.stop(timeout=0.05)
    assert writer.start() is False
    assert len(ril_threads()) == 1, "a second worker would share the connection"

    release.set()
    assert wait_for(lambda: writer.queue_capacity > 0), "the old worker did not start the new one"
    writer.submit_start(turn("b" * 32))
    assert wait_for(lambda: len(connections) >= 2 and connections[-1].of(INSERT_SQL))
    assert len(ril_threads()) == 1
    writer.stop()


def test_a_stopped_worker_that_nobody_asked_to_restart_just_ends():
    release = threading.Event()

    def stuck(**kwargs):
        release.wait(10)
        return FakeConnection()

    writer, _, _ = run_threaded(connect=stuck)
    writer.start()
    writer.submit_start(turn())
    time.sleep(0.1)

    writer.stop(timeout=0.05)
    release.set()

    assert wait_for(lambda: not ril_threads())
    time.sleep(0.2)
    assert not ril_threads()
    assert writer.start() is True
    writer.stop()


def test_a_start_followed_by_a_stop_does_not_leave_a_worker_behind():
    release = threading.Event()

    def stuck(**kwargs):
        release.wait(10)
        return FakeConnection()

    writer, _, _ = run_threaded(connect=stuck)
    writer.start()
    writer.submit_start(turn())
    time.sleep(0.1)

    writer.stop(timeout=0.05)
    assert writer.start() is False
    writer.stop(timeout=0.05)
    release.set()

    assert wait_for(lambda: not ril_threads())
    time.sleep(0.2)
    assert not ril_threads()


def test_stop_does_not_hold_the_caller_for_long_while_the_worker_is_stuck_in_the_database():
    release = threading.Event()

    def stuck(**kwargs):
        release.wait(10)
        return FakeConnection()

    writer, _, _ = run_threaded(connect=stuck)
    writer.start()
    writer.submit_start(turn())
    time.sleep(0.1)

    began = time.monotonic()
    writer.stop()
    waited = time.monotonic() - began
    release.set()

    assert waited < 1.5
    assert wait_for(lambda: not ril_threads())


def test_the_writer_reports_which_tool_texts_the_settings_ask_to_keep():
    harness = Harness({"log_tool_input": True, "log_tool_output": False})

    assert harness.writer.keeps_tool_input is True
    assert harness.writer.keeps_tool_output is False


def test_writer_module_does_not_import_cheshire_cat_or_the_driver():
    source = Path(writer_module.__file__).read_text(encoding="utf-8")

    assert "import cat" not in source and "from cat" not in source
    assert "pymysql" not in source.lower()


def test_the_worker_survives_an_unexpected_error_and_reports_it_once():
    writer, connection, log = run_threaded()
    real_step = writer.step
    failures = []

    def step_that_fails_twice(timeout=None):
        if len(failures) < 2:
            failures.append(1)
            raise ZeroDivisionError(LEAK_MARKER)
        real_step(timeout)

    writer.step = step_that_fails_twice
    writer.start()
    assert wait_for(lambda: len(failures) == 2)
    writer.submit_start(turn())

    assert wait_for(lambda: connection.of(INSERT_SQL))
    writer.stop()

    reports = [line for line in log.text("warning") if "unexpected error" in line]
    assert len(reports) == 1 and "ZeroDivisionError" in reports[0]
    assert LEAK_MARKER not in " ".join(log.text())


def test_stop_wakes_an_idle_worker_instead_of_waiting_for_its_poll(monkeypatch):
    monkeypatch.setattr(writer_module, "POLL_SECONDS", 30.0)
    writer, _, _ = run_threaded()
    writer.start()
    time.sleep(0.2)

    began = time.monotonic()
    writer.stop()

    assert time.monotonic() - began < 0.4
    assert not ril_threads()
