"""Open the database connection without exposing its credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# The Cat imports plugin files as `cat.plugins.<folder>.<module>`, so the
# relative import is the one that works at runtime; the absolute fallback is for
# the tests, which put the plugin folder on the path.
try:
    from .record import finalize_generated, start_record
    from .schema import (
        CREATE_TABLE_SQL,
        DELETE_ID_SQL,
        INSERT_SQL,
        ROLLBACK_SQL,
        SELECT_ID_SQL,
        START_TRANSACTION_SQL,
        TLS_CIPHER_SQL,
        UPDATE_SQL,
        to_row,
        to_update_params,
    )
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from record import finalize_generated, start_record
    from schema import (
        CREATE_TABLE_SQL,
        DELETE_ID_SQL,
        INSERT_SQL,
        ROLLBACK_SQL,
        SELECT_ID_SQL,
        START_TRANSACTION_SQL,
        TLS_CIPHER_SQL,
        UPDATE_SQL,
        to_row,
        to_update_params,
    )


CONNECT_TIMEOUT_SECONDS = 5
IO_TIMEOUT_SECONDS = 10

# PyMySQL treats falsy `ssl_verify_*` values as "no TLS options" and falls back
# to opportunistic TLS, which silently stays in clear text. A non-empty `ssl`
# mapping is what makes the driver ask for TLS; with no CA it does not verify
# the server certificate.
REQUIRED_TLS = {"check_hostname": False, "verify_mode": False}


class TlsRequiredError(RuntimeError):
    """The server did not encrypt a session that had to be encrypted."""


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """What the worker needs to reach the database; the password stays hidden."""

    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    require_ssl: bool = True
    create_table: bool = True

    @classmethod
    def from_settings(cls, settings: dict) -> ConnectionConfig | None:
        """Build the config from the plugin settings; no host means disabled."""
        host = str(settings.get("db_host") or "").strip()
        if not host:
            return None
        return cls(
            host=host,
            port=int(settings.get("db_port") or 3306),
            database=str(settings.get("db_name") or "").strip(),
            user=str(settings.get("db_user") or "").strip(),
            password=str(settings.get("db_password") or ""),
            require_ssl=settings.get("db_require_ssl", True) is not False,
            create_table=settings.get("create_table", True) is not False,
        )

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a connection check; `detail` never holds a secret."""

    ok: bool
    stage: str
    detail: str

    @property
    def message(self) -> str:
        if self.ok:
            return f"Database check passed for {self.detail}"
        return f"Database check failed at {self.stage}: {self.detail}"


def open_connection(config: ConnectionConfig, connect=None, warn=None):
    """Return an open autocommit connection, or raise.

    `connect` is `pymysql.connect` unless a test injects another; the driver is
    imported only here, so the other modules and the unit tests do not need it.
    """
    connection = _connect(config, connect)
    try:
        _secure(connection, config, warn)
        if config.create_table:
            ensure_table(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def check_connection(config: ConnectionConfig, connect=None, warn=None) -> CheckResult:
    """Try the whole write path once and leave nothing behind.

    Stages: `connect` (reach the server and log in), `tls`, `table` (create it
    if asked) and `write`, which inserts, updates, reads and deletes a probe row
    inside a transaction that is rolled back, so a missing privilege shows up
    here and not on the first real turn.
    """
    stage = "connect"
    connection = None
    try:
        connection = _connect(config, connect)
        stage = "tls"
        _secure(connection, config, warn)
        stage = "table"
        if config.create_table:
            ensure_table(connection)
        stage = "write"
        _probe_write(connection)
    except Exception as error:
        return CheckResult(False, stage, safe_error(error, config))
    finally:
        if connection is not None:
            connection.close()
    return CheckResult(True, "write", f"{config.address}: {_checked(config)}")


def ensure_table(connection) -> None:
    """Create the log table when it is missing; needs the CREATE privilege."""
    cursor = connection.cursor()
    try:
        cursor.execute(CREATE_TABLE_SQL)
    finally:
        cursor.close()


def error_code(error: BaseException) -> int | None:
    """Return the numeric server or driver error code, which carries no secret."""
    args = getattr(error, "args", ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        return args[0]
    return None


def safe_error(error: BaseException, config: ConnectionConfig | None) -> str:
    """Describe a failure with its class, code and host only; the text may hold secrets."""
    name = type(error).__name__
    code = error_code(error)
    if code is not None:
        name = f"{name}({code})"
    return name if config is None else f"{name} {config.address}"


def _checked(config: ConnectionConfig) -> str:
    """Name only the checks that actually ran for this configuration."""
    parts = ["connect"]
    parts.append("TLS" if config.require_ssl else "TLS not required")
    if config.create_table:
        parts.append("table")
    parts.append("write")
    return ", ".join(parts)


def _connect(config: ConnectionConfig, connect=None):
    if connect is None:
        import pymysql

        connect = pymysql.connect
    options = {}
    if config.require_ssl:
        options["ssl"] = dict(REQUIRED_TLS)
    return connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=IO_TIMEOUT_SECONDS,
        write_timeout=IO_TIMEOUT_SECONDS,
        **options,
    )


def _secure(connection, config: ConnectionConfig, warn) -> None:
    if config.require_ssl:
        _require_encrypted(connection, config)
    elif warn is not None:
        warn(f"TLS is not required for the database connection to {config.address}")


def _require_encrypted(connection, config: ConnectionConfig) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(TLS_CIPHER_SQL)
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row or not row[1]:
        raise TlsRequiredError(
            f"TLS is required but the session to {config.address} is not encrypted"
        )


def _probe_write(connection) -> None:
    record = start_record(
        ts=datetime.now(timezone.utc),
        started_ns=0,
        instance="ril-connection-check",
        user_id="ril-connection-check",
        question=None,
        guard_turn_id_at_start=None,
        local_turn_id="0" * 32,
    )
    record = finalize_generated(record, "probe", None, 0)
    cursor = connection.cursor()
    try:
        cursor.execute(START_TRANSACTION_SQL)
        try:
            cursor.execute(INSERT_SQL, to_row(record))
            row_id = cursor.lastrowid
            cursor.execute(UPDATE_SQL, to_update_params(record, row_id))
            cursor.execute(SELECT_ID_SQL, (row_id,))
            cursor.execute(DELETE_ID_SQL, (row_id,))
        finally:
            cursor.execute(ROLLBACK_SQL)
    finally:
        cursor.close()
