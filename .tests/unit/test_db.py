"""Tests for the connection module; these need neither Cheshire Cat nor PyMySQL."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import db
from db import (
    ConnectionConfig,
    TlsRequiredError,
    check_connection,
    ensure_table,
    error_code,
    open_connection,
    safe_error,
)
from schema import (
    CREATE_TABLE_SQL,
    DELETE_ID_SQL,
    INSERT_SQL,
    ROLLBACK_SQL,
    SELECT_ID_SQL,
    START_TRANSACTION_SQL,
    UPDATE_SQL,
)


LEAK_MARKER = "s3cr3t-pa55"


class OperationalError(Exception):
    """Stands in for the driver error: a numeric code first, then free text."""

SETTINGS = {
    "db_host": "db.example.org",
    "db_port": 3307,
    "db_name": "ril",
    "db_user": "ril_logger",
    "db_password": LEAK_MARKER,
    "db_require_ssl": True,
    "create_table": True,
}


def config(**overrides):
    return ConnectionConfig.from_settings({**SETTINGS, **overrides})


class FakeCursor:
    lastrowid = 7

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        self.connection.executed.append(sql)
        if sql == CREATE_TABLE_SQL and self.connection.fail_create:
            raise RuntimeError(f"CREATE denied for {LEAK_MARKER}")
        failure = self.connection.fail_on.get(sql.split()[0])
        if failure is not None:
            raise failure

    def fetchone(self):
        cipher = self.connection.cipher
        return None if cipher is None else ("Ssl_cipher", cipher)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cipher="TLS_AES_256_GCM_SHA384", fail_create=False, fail_on=None):
        self.cipher = cipher
        self.fail_create = fail_create
        self.fail_on = fail_on or {}
        self.executed = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def fake_connect(connection=None):
    connection = connection or FakeConnection()
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        return connection

    connect.calls = calls
    connect.connection = connection
    return connect


def test_empty_host_disables_the_logger():
    assert ConnectionConfig.from_settings({}) is None
    assert ConnectionConfig.from_settings({"db_host": "   "}) is None


def test_settings_become_a_connection_config():
    result = config()

    assert (result.host, result.port, result.database, result.user) == (
        "db.example.org",
        3307,
        "ril",
        "ril_logger",
    )
    assert result.password == LEAK_MARKER
    assert result.require_ssl is True
    assert result.create_table is True


def test_config_defaults_are_the_secure_ones():
    result = ConnectionConfig.from_settings({"db_host": "h"})

    assert result.port == 3306
    assert result.require_ssl is True
    assert result.create_table is True
    assert result.password == ""


def test_config_never_shows_the_password_and_cannot_change():
    result = config()

    assert LEAK_MARKER not in repr(result)
    assert LEAK_MARKER not in str(result)
    with pytest.raises(FrozenInstanceError):
        result.host = "elsewhere"


def test_same_settings_give_equal_configs_so_a_change_is_detectable():
    assert config() == config()
    assert config() != config(db_password="other")
    assert config() != config(db_name="other")


def test_connection_arguments():
    connect = fake_connect()

    open_connection(config(), connect)

    (kwargs,) = connect.calls
    assert kwargs["host"] == "db.example.org"
    assert kwargs["port"] == 3307
    assert kwargs["user"] == "ril_logger"
    assert kwargs["password"] == LEAK_MARKER
    assert kwargs["database"] == "ril"
    assert kwargs["charset"] == "utf8mb4"
    assert kwargs["autocommit"] is True
    assert 0 < kwargs["connect_timeout"] <= 10
    assert 0 < kwargs["read_timeout"] <= 30
    assert 0 < kwargs["write_timeout"] <= 30


def test_required_tls_asks_the_driver_for_tls_without_verifying_the_certificate():
    connect = fake_connect()

    open_connection(config(db_require_ssl=True), connect)

    (kwargs,) = connect.calls
    assert kwargs["ssl"] == {"check_hostname": False, "verify_mode": False}
    assert "ssl_disabled" not in kwargs


def test_optional_tls_leaves_the_driver_to_negotiate_it():
    connect = fake_connect(FakeConnection(cipher=""))

    open_connection(config(db_require_ssl=False), connect)

    (kwargs,) = connect.calls
    assert "ssl" not in kwargs
    assert "ssl_disabled" not in kwargs


@pytest.mark.parametrize("cipher", ["", None])
def test_required_tls_is_refused_when_the_session_is_not_encrypted(cipher):
    connection = FakeConnection(cipher=cipher)

    with pytest.raises(TlsRequiredError) as raised:
        open_connection(config(), fake_connect(connection))

    assert connection.closed is True
    assert LEAK_MARKER not in str(raised.value)
    assert "db.example.org:3307" in str(raised.value)


def test_required_tls_checks_the_cipher_of_the_session():
    connection = FakeConnection()

    open_connection(config(), fake_connect(connection))

    assert "SHOW SESSION STATUS LIKE 'Ssl_cipher'" in connection.executed
    assert connection.closed is False


def test_optional_tls_does_not_check_the_cipher_and_warns_once_per_connection():
    warnings = []
    connection = FakeConnection(cipher="")

    open_connection(
        config(db_require_ssl=False), fake_connect(connection), warn=warnings.append
    )

    assert "SHOW SESSION STATUS LIKE 'Ssl_cipher'" not in connection.executed
    assert len(warnings) == 1
    assert "db.example.org:3307" in warnings[0]
    assert LEAK_MARKER not in warnings[0]
    assert connection.closed is False


def test_required_tls_does_not_warn():
    warnings = []

    open_connection(config(), fake_connect(), warn=warnings.append)

    assert warnings == []


def test_table_is_created_only_when_asked():
    asked = FakeConnection()
    not_asked = FakeConnection()

    open_connection(config(create_table=True), fake_connect(asked))
    open_connection(config(create_table=False), fake_connect(not_asked))

    assert CREATE_TABLE_SQL in asked.executed
    assert CREATE_TABLE_SQL not in not_asked.executed


def test_ensure_table_runs_the_idempotent_ddl():
    connection = FakeConnection()

    ensure_table(connection)

    assert connection.executed == [CREATE_TABLE_SQL]


def test_failed_table_creation_closes_the_connection_and_propagates():
    connection = FakeConnection(fail_create=True)

    with pytest.raises(RuntimeError):
        open_connection(config(), fake_connect(connection))

    assert connection.closed is True


def test_default_connect_is_pymysql_imported_on_demand(monkeypatch):
    calls = []
    fake_driver = types.ModuleType("pymysql")
    fake_driver.connect = lambda **kwargs: calls.append(kwargs) or FakeConnection()
    monkeypatch.setitem(sys.modules, "pymysql", fake_driver)

    open_connection(config())

    assert len(calls) == 1
    assert not hasattr(db, "pymysql")


def test_safe_error_names_the_class_and_the_host_only():
    error = RuntimeError(f"Access denied for user 'x' using password {LEAK_MARKER}")

    message = safe_error(error, config())

    assert message == "RuntimeError db.example.org:3307"
    assert LEAK_MARKER not in message


def test_safe_error_without_a_config_names_only_the_class():
    assert safe_error(ValueError("boom"), None) == "ValueError"


def test_safe_error_adds_the_numeric_code_but_never_the_text():
    denied = OperationalError(1142, f"UPDATE command denied to user 'x' ({LEAK_MARKER})")

    message = safe_error(denied, config())

    assert message == "OperationalError(1142) db.example.org:3307"
    assert LEAK_MARKER not in message
    assert error_code(denied) == 1142
    assert error_code(RuntimeError("text only")) is None
    assert error_code(RuntimeError(True)) is None


def statements_after(connection, first):
    return connection.executed[connection.executed.index(first):]


def test_check_passes_after_a_full_write_cycle_that_is_rolled_back():
    connection = FakeConnection()

    result = check_connection(config(), fake_connect(connection))

    assert result.ok is True
    assert "db.example.org:3307" in result.message
    assert LEAK_MARKER not in result.message
    cycle = statements_after(connection, START_TRANSACTION_SQL)
    assert [sql.split()[0] for sql in cycle] == ["START", "INSERT", "UPDATE", "SELECT", "DELETE", "ROLLBACK"]
    assert cycle[1:5] == [INSERT_SQL, UPDATE_SQL, SELECT_ID_SQL, DELETE_ID_SQL]
    assert cycle[-1] == ROLLBACK_SQL
    assert connection.closed is True


def test_success_message_names_only_the_checks_that_ran():
    full = check_connection(config(), fake_connect(FakeConnection())).message
    no_tls = check_connection(
        config(db_require_ssl=False, create_table=False), fake_connect(FakeConnection(cipher=""))
    ).message

    assert full.endswith("db.example.org:3307: connect, TLS, table, write")
    assert no_tls.endswith("db.example.org:3307: connect, TLS not required, write")


def test_check_creates_the_table_only_when_asked_and_before_the_probe():
    asked = FakeConnection()
    not_asked = FakeConnection()

    check_connection(config(create_table=True), fake_connect(asked))
    check_connection(config(create_table=False), fake_connect(not_asked))

    assert asked.executed.index(CREATE_TABLE_SQL) < asked.executed.index(START_TRANSACTION_SQL)
    assert CREATE_TABLE_SQL not in not_asked.executed


def test_check_reports_a_server_that_cannot_be_reached():
    def connect(**kwargs):
        raise OperationalError(2003, f"Can't connect to MySQL server ({LEAK_MARKER})")

    result = check_connection(config(), connect)

    assert (result.ok, result.stage) == (False, "connect")
    assert "OperationalError(2003) db.example.org:3307" in result.message
    assert LEAK_MARKER not in result.message


def test_check_reports_a_session_that_is_not_encrypted():
    connection = FakeConnection(cipher="")

    result = check_connection(config(), fake_connect(connection))

    assert (result.ok, result.stage) == (False, "tls")
    assert "TlsRequiredError" in result.message
    assert connection.closed is True


def test_check_reports_a_table_that_cannot_be_created():
    connection = FakeConnection(fail_create=True)

    result = check_connection(config(), fake_connect(connection))

    assert (result.ok, result.stage) == (False, "table")
    assert LEAK_MARKER not in result.message
    assert connection.closed is True


def test_check_reports_a_missing_privilege_and_still_rolls_back():
    connection = FakeConnection(
        fail_on={"UPDATE": OperationalError(1142, f"UPDATE command denied ({LEAK_MARKER})")}
    )

    result = check_connection(config(), fake_connect(connection))

    assert (result.ok, result.stage) == (False, "write")
    assert "OperationalError(1142)" in result.message
    assert LEAK_MARKER not in result.message
    assert connection.executed[-1] == ROLLBACK_SQL
    assert connection.closed is True


def test_check_warns_when_tls_is_not_required():
    warnings = []

    check_connection(config(db_require_ssl=False), fake_connect(FakeConnection(cipher="")), warn=warnings.append)

    assert len(warnings) == 1


def test_db_module_does_not_import_cheshire_cat():
    source = Path(db.__file__).read_text(encoding="utf-8")

    assert "import cat" not in source and "from cat" not in source
