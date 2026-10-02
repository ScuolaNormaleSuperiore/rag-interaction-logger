"""Fixtures for tests that talk to a real MySQL or MariaDB server.

Nothing here runs unless the server is described in the environment, and no
credential is ever written to a file:

    RIL_TEST_DB_HOST        host of the server (from the container: host.docker.internal)
    RIL_TEST_DB_PORT        port, default 3306
    RIL_TEST_DB_USER        a user that may create databases and users
    RIL_TEST_DB_PASSWORD    its password
    RIL_TEST_DB_REQUIRE_SSL "true" to require TLS; default "false" (a development server)
    RIL_TEST_DB_TLS         "yes" or "no": whether the server offers TLS; unset skips the TLS tests
    RIL_TEST_PROJECT_MD     a copy of DEV/AGENTS/PROJECT.md, whose analysis queries are run
                            (`run-tests.py --database` copies it into the container)

Every test gets its own scratch database, created and dropped here, so the
application database is never touched.
"""

import os
from pathlib import Path
import secrets
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from writer import Writer  # noqa: E402


MISSING = (
    "set RIL_TEST_DB_HOST, RIL_TEST_DB_USER and RIL_TEST_DB_PASSWORD (a user that can "
    "create databases) to run the database tests"
)


class Log:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("info", message))

    def warning(self, message):
        self.lines.append(("warning", message))

    def text(self):
        return [message for _, message in self.lines]


class Scratch:
    """A throwaway database on the server under test, and what tests need around it."""

    def __init__(self, admin, name, server):
        self.admin = admin
        self.name = name
        self.server = server
        self.users = []
        self.writers = []

    def settings(self, **overrides) -> dict:
        values = {
            "db_host": self.server["host"],
            "db_port": self.server["port"],
            "db_name": self.name,
            "db_user": self.server["user"],
            "db_password": self.server["password"],
            "db_require_ssl": self.server["require_ssl"],
            "create_table": True,
            "queue_size": 1000,
            "retention_days": 0,
            "log_tool_input": False,
            "log_tool_output": False,
            "tool_text_limit": 1000,
        }
        values.update(overrides)
        return values

    def execute(self, sql, params=None):
        with self.admin.cursor() as cursor:
            cursor.execute(sql, params)

    def rows(self, sql, params=None) -> tuple:
        with self.admin.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def table_rows(self, columns="*", where="") -> tuple:
        return self.rows(f"SELECT {columns} FROM `{self.name}`.ril_interactions {where} ORDER BY id")

    def writer(self, utcnow=None, clock=None, **overrides) -> Writer:
        settings = self.settings(**overrides)
        log = Log()
        extra = {"clock": clock} if clock is not None else {}
        writer = Writer(load_settings=lambda: dict(settings), log=log, utcnow=utcnow, **extra)
        writer.log = log
        writer.settings = settings
        writer.prepare()
        self.writers.append(writer)
        return writer

    @staticmethod
    def pump(writer, steps=80):
        for _ in range(steps):
            writer.step(timeout=0)

    def new_user(self, privileges="CREATE, INSERT, UPDATE, SELECT, DELETE") -> tuple[str, str]:
        """Create a user limited to this scratch database, as the DBA script of PROJECT.md does."""
        user = f"ril_t_{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(12)
        self.execute(f"CREATE USER '{user}'@'%' IDENTIFIED BY '{password}'")
        self.execute(f"GRANT {privileges} ON `{self.name}`.* TO '{user}'@'%'")
        self.users.append(user)
        return user, password


@pytest.fixture(scope="session")
def server():
    host = os.environ.get("RIL_TEST_DB_HOST")
    user = os.environ.get("RIL_TEST_DB_USER")
    if not host or not user:
        pytest.skip(MISSING)
    pymysql = pytest.importorskip("pymysql")
    config = {
        "host": host,
        "port": int(os.environ.get("RIL_TEST_DB_PORT", "3306")),
        "user": user,
        "password": os.environ.get("RIL_TEST_DB_PASSWORD", ""),
        "require_ssl": os.environ.get("RIL_TEST_DB_REQUIRE_SSL", "false").lower() == "true",
        "tls": os.environ.get("RIL_TEST_DB_TLS", "").lower(),
    }
    # A configured but unreachable server is a failure, not a skip.
    connection = pymysql.connect(
        host=config["host"], port=config["port"], user=config["user"],
        password=config["password"], charset="utf8mb4", autocommit=True, connect_timeout=5,
    )
    connection.close()
    return config


@pytest.fixture
def scratch(server):
    import pymysql

    admin = pymysql.connect(
        host=server["host"], port=server["port"], user=server["user"],
        password=server["password"], charset="utf8mb4", autocommit=True,
    )
    name = f"ril_test_{secrets.token_hex(4)}"
    with admin.cursor() as cursor:
        cursor.execute(
            f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    database = Scratch(admin, name, server)
    try:
        yield database
    finally:
        for writer in database.writers:
            writer._close_connection()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
            for user in database.users:
                cursor.execute(f"DROP USER IF EXISTS '{user}'@'%'")
        admin.close()
