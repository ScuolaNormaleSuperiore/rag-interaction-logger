"""Bounded queue and worker thread that write turns to the database.

Hooks only enqueue; the worker owns the connection and does every statement, so
a slow or unreachable database never touches a user turn.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# The Cat imports plugin files as `cat.plugins.<folder>.<module>`, so the
# relative import is the one that works at runtime; the absolute fallback is for
# the tests, which put the plugin folder on the path.
try:
    from .db import ConnectionConfig, open_connection, safe_error
    from .schema import (
        INSERT_SQL,
        PURGE_SQL,
        TOOL_INPUT_ROW_INDEX,
        TOOL_INPUT_UPDATE_INDEX,
        TOOL_OUTPUT_ROW_INDEX,
        TOOL_OUTPUT_UPDATE_INDEX,
        UPDATE_SQL,
        blank_at,
        retention_cutoff,
        to_row,
        to_update_params,
    )
    from .record import TOOL_TEXT_LIMIT, tool_text_limit_from
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from db import ConnectionConfig, open_connection, safe_error
    from schema import (
        INSERT_SQL,
        PURGE_SQL,
        TOOL_INPUT_ROW_INDEX,
        TOOL_INPUT_UPDATE_INDEX,
        TOOL_OUTPUT_ROW_INDEX,
        TOOL_OUTPUT_UPDATE_INDEX,
        UPDATE_SQL,
        blank_at,
        retention_cutoff,
        to_row,
        to_update_params,
    )
    from record import TOOL_TEXT_LIMIT, tool_text_limit_from


PREFIX = "RAG Interaction Logger: "
DEFAULT_QUEUE_SIZE = 1000
MAX_QUEUE_SIZE = 10000
POLL_SECONDS = 1.0
STOP_TIMEOUT_SECONDS = 0.5
CORRELATION_TTL_SECONDS = 24 * 3600
CORRELATION_SWEEP_SECONDS = 60
PURGE_INTERVAL_SECONDS = 24 * 3600
PURGE_RECHECK_SECONDS = 3600
PURGE_BATCH_SIZE = 1000
PURGE_BATCHES_PER_CYCLE = 5
PURGE_PAUSE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class Event:
    """What a hook hands to the worker: plain values, nothing from the Cat."""

    kind: str
    key: str
    turn_id: str | None
    row: tuple
    update_values: tuple


class Writer:
    """Queue plus worker; every dependency is injected so tests need no thread."""

    def __init__(self, *, load_settings, log, connect=None, clock=time.monotonic, utcnow=None):
        self._load_settings = load_settings
        self._log = log
        self._connect = connect
        self._clock = clock
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()
        self._lock = threading.Lock()
        self._dropped = 0
        self._episode_open = False
        self._submit_error: str | None = None
        self._connection = None
        self._config: ConnectionConfig | None = None
        self._ids: dict[str, tuple[int, float]] = {}
        self._last_sweep = 0.0
        self._last_failure: tuple | None = None
        self._disabled_noted = False
        self._next_purge_check = 0.0
        self._purge_cutoff: datetime | None = None
        self._purge_resume = 0.0
        self._tool_text_limit = TOOL_TEXT_LIMIT
        self._keeps_tool_input = False
        self._keeps_tool_output = False
        self._resume = False
        self._loop_error: str | None = None

    # -- called from the hooks: never block, never raise ---------------------

    def submit_start(self, record) -> None:
        """Queue the insert of a turn that has just begun."""
        self._submit("start", record)

    def submit_finish(self, record) -> None:
        """Queue the update (or the full insert) of a turn that has ended."""
        self._submit("finish", record)

    def _submit(self, kind: str, record) -> None:
        try:
            target = self._queue
            if target is None:
                return
            event = Event(
                kind=kind,
                key=record.local_turn_id,
                turn_id=record.turn_id,
                row=to_row(record),
                update_values=to_update_params(record, 0)[:-1] if kind == "finish" else (),
            )
            target.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._dropped += 1
        except Exception as error:
            self._submit_error = type(error).__name__

    # -- lifecycle -----------------------------------------------------------

    def prepare(self) -> None:
        """Create the queue, sized from the settings at this moment."""
        settings = self._safe_settings()
        self._remember_tool_text_limit(settings)
        self._queue = queue.Queue(maxsize=self._queue_size_from(settings))

    def start(self) -> bool:
        """Start the worker; return False if one is already running.

        A worker that was stopped but is still finishing a database call is not
        replaced at once (two workers would share one connection): this call leaves
        a request and the old worker starts the new one when it ends.
        """
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    self._resume = True
                return False
            self._launch()
            return True

    def _launch(self) -> None:
        self.prepare()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, args=(self._stop,), name="ril-writer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Ask the worker to end; it closes its own connection. Queued events are lost.

        The wait is short on purpose: the Cat calls this on the event loop, and a
        worker stuck in a database call would otherwise hold every request for as long.
        """
        with self._lifecycle:
            self._stop.set()
            waiting, self._queue = self._queue, None
            self._resume = False
            thread = self._thread
        if waiting is not None:
            try:
                waiting.put_nowait(None)  # wakes an idle worker; `step` ignores it
            except queue.Full:
                pass  # a busy worker sees the stop flag on its next turn of the loop
        if thread is not None:
            thread.join(timeout)

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize if self._queue is not None else 0

    @property
    def tool_text_limit(self) -> int:
        """Longest tool input or output to keep, as the worker last read it.

        The hooks cannot read the settings (no I/O in a turn), so they take the
        value the worker keeps current: it reads the settings for the start event,
        which is queued long before the answer is captured.
        """
        return self._tool_text_limit

    @property
    def keeps_tool_input(self) -> bool:
        """Whether `log_tool_input` was on when the worker last read the settings."""
        return self._keeps_tool_input

    @property
    def keeps_tool_output(self) -> bool:
        """Whether `log_tool_output` was on when the worker last read the settings."""
        return self._keeps_tool_output

    @property
    def pending_count(self) -> int:
        """Turns whose start was written and whose finish has not arrived."""
        return len(self._ids)

    # -- the worker ----------------------------------------------------------

    def _loop(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.step()
                    self._loop_error = None
                except Exception as error:
                    # Nothing may end the worker: with it gone no row is written and
                    # nothing reports it. Report once per kind of error and go on.
                    self._loop_failed(error)
                    stop.wait(POLL_SECONDS)
        finally:
            self._close_connection()
            self._take_over()

    def _take_over(self) -> None:
        """At the end of a stopped worker, start the one a later `start()` asked for."""
        with self._lifecycle:
            if self._thread is threading.current_thread():
                self._thread = None
            if self._resume:
                self._resume = False
                try:
                    self._launch()
                except Exception as error:
                    self._warn(f"the writer could not be restarted ({type(error).__name__})")

    def _loop_failed(self, error: BaseException) -> None:
        name = type(error).__name__
        if name != self._loop_error:
            self._loop_error = name
            try:
                self._warn(f"the writer hit an unexpected error ({name}) and keeps running")
            except Exception:
                pass

    def step(self, timeout: float | None = None) -> None:
        """Run one worker iteration: report losses, handle one event, advance the purge."""
        target = self._queue
        if target is None:
            return
        self._report_losses(target)
        wait = POLL_SECONDS if timeout is None else timeout
        if self._purge_cutoff is not None:
            # A purge in progress wakes the worker when its pause ends, or at once for an event.
            wait = min(wait, max(0.0, self._purge_resume - self._clock()))
        try:
            event = target.get(timeout=wait)
        except queue.Empty:
            event = None
        if event is not None:
            self._handle(event)
        self._maybe_purge()

    def _handle(self, event: Event) -> None:
        try:
            settings = self._load_settings()
            config = ConnectionConfig.from_settings(settings)
        except Exception as error:
            self._fail("settings", event.turn_id, error, None)
            return
        if config is None:
            if not self._disabled_noted:
                self._info("db_host is empty, logging is disabled")
                self._disabled_noted = True
            return
        self._disabled_noted = False
        self._remember_tool_text_limit(settings)
        row, update_values = event.row, event.update_values
        # Tool inputs and outputs can hold personal data and no guard checks them:
        # each is saved only on request, decided here with the settings as they are
        # when the row is written.
        for option, row_index, update_index in (
            ("log_tool_input", TOOL_INPUT_ROW_INDEX, TOOL_INPUT_UPDATE_INDEX),
            ("log_tool_output", TOOL_OUTPUT_ROW_INDEX, TOOL_OUTPUT_UPDATE_INDEX),
        ):
            if settings.get(option) is not True:
                row = blank_at(row, row_index)
                if update_values:
                    update_values = blank_at(update_values, update_index)
        operation = "insert"
        try:
            connection = self._connection_for(config)
            now = self._clock()
            self._sweep(now)
            cursor = connection.cursor()
            try:
                if event.kind == "start":
                    cursor.execute(INSERT_SQL, row)
                    if cursor.lastrowid:
                        self._ids[event.key] = (cursor.lastrowid, now)
                elif event.key in self._ids:
                    operation = "update"
                    row_id, _ = self._ids.pop(event.key)
                    cursor.execute(UPDATE_SQL, (*update_values, row_id))
                else:
                    cursor.execute(INSERT_SQL, row)
            finally:
                cursor.close()
            self._succeeded()
        except Exception as error:
            self._fail(operation, event.turn_id, error, config)

    def _connection_for(self, config: ConnectionConfig):
        if self._connection is not None and self._config == config:
            try:
                # The driver's own `reconnect` is deprecated since 1.2 and its default
                # changed between versions: a dropped connection is replaced here.
                self._connection.ping(reconnect=False)
                return self._connection
            except Exception:
                pass
        self._close_connection()
        self._connection = open_connection(
            config, self._connect, warn=lambda message: self._warn(message)
        )
        self._config = config
        return self._connection

    def _close_connection(self) -> None:
        connection, self._connection, self._config = self._connection, None, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < CORRELATION_SWEEP_SECONDS:
            return
        self._last_sweep = now
        self._ids = {
            key: value
            for key, value in self._ids.items()
            if now - value[1] <= CORRELATION_TTL_SECONDS
        }

    # -- the purge -----------------------------------------------------------

    def _maybe_purge(self) -> None:
        now = self._clock()
        if self._purge_cutoff is None:
            if now < self._next_purge_check:
                return
            self._purge_cutoff = self._start_purge(now)
            if self._purge_cutoff is None:
                return
        elif now < self._purge_resume:
            return  # pausing between two cycles of batches, so the database gets a rest
        try:
            config, days = self._purge_settings()
            if config is None or days <= 0:
                self._end_purge(now, PURGE_RECHECK_SECONDS)
                return
            connection = self._connection_for(config)
            for _ in range(PURGE_BATCHES_PER_CYCLE):
                if self._delete_batch(connection) < PURGE_BATCH_SIZE:
                    self._end_purge(now, PURGE_INTERVAL_SECONDS)
                    return
            self._purge_resume = now + PURGE_PAUSE_SECONDS
            self._succeeded()
        except Exception as error:
            self._fail("purge", None, error, self._config)
            self._end_purge(now, PURGE_INTERVAL_SECONDS)

    def _start_purge(self, now: float) -> datetime | None:
        try:
            config, days = self._purge_settings()
        except Exception:
            config, days = None, 0
        cutoff = retention_cutoff(self._utcnow(), days) if config is not None else None
        if cutoff is None:
            self._next_purge_check = now + PURGE_RECHECK_SECONDS
        return cutoff

    def _purge_settings(self) -> tuple[ConnectionConfig | None, int]:
        settings = self._load_settings()
        return ConnectionConfig.from_settings(settings), int(settings.get("retention_days") or 0)

    def _delete_batch(self, connection) -> int:
        cursor = connection.cursor()
        try:
            cursor.execute(PURGE_SQL, (self._purge_cutoff, PURGE_BATCH_SIZE))
            return cursor.rowcount
        finally:
            cursor.close()

    def _end_purge(self, now: float, delay: float) -> None:
        self._purge_cutoff = None
        self._purge_resume = 0.0
        self._next_purge_check = now + delay

    # -- reporting: only the worker writes log lines -------------------------

    def _report_losses(self, target: queue.Queue) -> None:
        if self._submit_error is not None:
            error, self._submit_error = self._submit_error, None
            self._warn(f"an event could not be prepared ({error})")
        with self._lock:
            dropped = self._dropped
        if dropped and not self._episode_open:
            self._episode_open = True
            self._warn("the queue is full, events are being lost")
        if self._episode_open and target.empty():
            with self._lock:
                lost, self._dropped = self._dropped, 0
            self._episode_open = False
            self._warn(f"the queue is draining again: {lost} events were lost")

    def _fail(self, operation: str, turn_id, error: BaseException, config) -> None:
        self._close_connection()
        detail = safe_error(error, config)
        marker = (operation, detail)
        if marker == self._last_failure:
            return
        self._last_failure = marker
        where = f" for turn={turn_id}" if turn_id else ""
        self._warn(f"{operation} failed{where}: {detail}")

    def _succeeded(self) -> None:
        if self._last_failure is not None:
            self._last_failure = None
            self._info("database writes recovered")

    def _warn(self, message: str) -> None:
        self._log.warning(f"{PREFIX}{message}")

    def _info(self, message: str) -> None:
        self._log.info(f"{PREFIX}{message}")

    def _safe_settings(self) -> dict:
        try:
            settings = self._load_settings()
        except Exception:
            return {}
        return settings if isinstance(settings, dict) else {}

    def _remember_tool_text_limit(self, settings: dict) -> None:
        self._tool_text_limit = tool_text_limit_from(settings)
        self._keeps_tool_input = settings.get("log_tool_input") is True
        self._keeps_tool_output = settings.get("log_tool_output") is True

    @staticmethod
    def _queue_size_from(settings: dict) -> int:
        try:
            size = int(settings.get("queue_size") or DEFAULT_QUEUE_SIZE)
            return max(1, min(MAX_QUEUE_SIZE, size))
        except (TypeError, ValueError):
            return DEFAULT_QUEUE_SIZE
