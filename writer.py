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
        UPDATE_SQL,
        retention_cutoff,
        to_row,
        to_update_params,
    )
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from db import ConnectionConfig, open_connection, safe_error
    from schema import (
        INSERT_SQL,
        PURGE_SQL,
        UPDATE_SQL,
        retention_cutoff,
        to_row,
        to_update_params,
    )


PREFIX = "RAG Interaction Logger: "
DEFAULT_QUEUE_SIZE = 1000
POLL_SECONDS = 1.0
STOP_TIMEOUT_SECONDS = 5.0
CORRELATION_TTL_SECONDS = 24 * 3600
CORRELATION_SWEEP_SECONDS = 60
PURGE_INTERVAL_SECONDS = 24 * 3600
PURGE_RECHECK_SECONDS = 3600
PURGE_BATCH_SIZE = 1000
PURGE_BATCHES_PER_CYCLE = 5


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
        self._queue = queue.Queue(maxsize=self._configured_queue_size())

    def start(self) -> bool:
        """Start the worker; return False if one is already running."""
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.prepare()
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._loop, args=(self._stop,), name="ril-writer", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Ask the worker to end; it closes its own connection. Queued events are lost."""
        with self._lifecycle:
            self._stop.set()
            self._queue = None
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize if self._queue is not None else 0

    @property
    def pending_count(self) -> int:
        """Turns whose start was written and whose finish has not arrived."""
        return len(self._ids)

    # -- the worker ----------------------------------------------------------

    def _loop(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                self.step()
        finally:
            self._close_connection()

    def step(self, timeout: float | None = None) -> None:
        """Run one worker iteration: report losses, handle one event, advance the purge."""
        target = self._queue
        if target is None:
            return
        self._report_losses(target)
        wait = POLL_SECONDS if timeout is None else timeout
        try:
            event = target.get(timeout=0 if self._purge_cutoff is not None else wait)
        except queue.Empty:
            event = None
        if event is not None:
            self._handle(event)
        self._maybe_purge()

    def _handle(self, event: Event) -> None:
        try:
            config = ConnectionConfig.from_settings(self._load_settings())
        except Exception as error:
            self._fail("settings", event.turn_id, error, None)
            return
        if config is None:
            if not self._disabled_noted:
                self._info("db_host is empty, logging is disabled")
                self._disabled_noted = True
            return
        self._disabled_noted = False
        operation = "insert"
        try:
            connection = self._connection_for(config)
            now = self._clock()
            self._sweep(now)
            cursor = connection.cursor()
            try:
                if event.kind == "start":
                    cursor.execute(INSERT_SQL, event.row)
                    if cursor.lastrowid:
                        self._ids[event.key] = (cursor.lastrowid, now)
                elif event.key in self._ids:
                    operation = "update"
                    row_id, _ = self._ids.pop(event.key)
                    cursor.execute(UPDATE_SQL, (*event.update_values, row_id))
                else:
                    cursor.execute(INSERT_SQL, event.row)
            finally:
                cursor.close()
            self._succeeded()
        except Exception as error:
            self._fail(operation, event.turn_id, error, config)

    def _connection_for(self, config: ConnectionConfig):
        if self._connection is not None and self._config == config:
            self._connection.ping(reconnect=True)
            return self._connection
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

    def _configured_queue_size(self) -> int:
        try:
            return max(1, int(self._load_settings().get("queue_size") or DEFAULT_QUEUE_SIZE))
        except Exception:
            return DEFAULT_QUEUE_SIZE
