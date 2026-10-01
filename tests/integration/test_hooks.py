"""Hook wiring tests; run inside a Cheshire Cat AI 1.9.2 container."""

import json
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logger = pytest.importorskip(
    "rag_interaction_logger",
    reason="requires Cheshire Cat AI; run python run-tests.py --integration",
)
settings = pytest.importorskip("settings")
CheckResult = pytest.importorskip("db").CheckResult


def test_the_four_observer_hooks_have_the_specified_names_and_priorities():
    hooks = {
        logger.start_interaction_record: ("fast_reply", 100),
        logger.finalize_fast_reply_record: ("fast_reply", -100),
        logger.capture_generated_answer: ("before_cat_sends_message", 100),
        logger.finalize_generated_record: ("before_cat_sends_message", -100),
    }

    for registered_hook, expected in hooks.items():
        assert (registered_hook.name, registered_hook.priority) == expected


def test_skeleton_hooks_are_observer_only():
    message = object()
    cat = object()

    for registered_hook in (
        logger.start_interaction_record,
        logger.finalize_fast_reply_record,
        logger.capture_generated_answer,
        logger.finalize_generated_record,
    ):
        assert registered_hook.function(message, cat) is None


def test_settings_model_registers_the_specified_defaults():
    model = settings.settings_model.function()
    defaults = model()

    assert defaults.db_host == ""
    assert defaults.db_port == 3306
    assert defaults.db_name == ""
    assert defaults.db_user == ""
    assert defaults.db_password == ""
    assert defaults.db_require_ssl is True
    assert defaults.create_table is True
    assert defaults.queue_size == 1000
    assert defaults.retention_days == 0


def test_password_setting_renders_as_a_masked_input():
    schema = settings.settings_model.function().model_json_schema()

    assert schema["properties"]["db_password"]["format"] == "password"


class LogRecorder:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("info", message))

    def warning(self, message):
        self.lines.append(("warning", message))

    def error(self, message):
        self.lines.append(("error", message))


LEAK_MARKER = "s3cr3t-pa55"
SAVED = {"db_host": "db.example.org", "db_port": 3306, "db_name": "ril", "db_user": "u", "db_password": LEAK_MARKER}


def test_save_settings_override_is_registered_under_the_core_name():
    assert settings.save_settings.name == "save_settings"


def test_write_settings_keeps_unknown_saved_keys_like_the_core_does(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"db_host": "old", "extra": 1}', encoding="utf-8")

    updated = settings.write_settings(path, {"db_host": "new", "db_port": 3307})

    assert updated == {"db_host": "new", "extra": 1, "db_port": 3307}
    assert json.loads(path.read_text(encoding="utf-8")) == updated


def test_write_settings_creates_the_file_and_returns_empty_when_it_cannot_write(tmp_path):
    created = tmp_path / "settings.json"

    assert settings.write_settings(created, {"db_host": "h"}) == {"db_host": "h"}
    assert settings.write_settings(tmp_path / "missing-dir" / "settings.json", {"a": 1}) == {}


def test_save_settings_writes_the_file_and_starts_the_check(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "check_in_background", started.append)

    returned = settings.save_settings.function(dict(SAVED))

    assert returned == SAVED
    assert started == [SAVED]


def test_save_settings_does_not_check_when_nothing_was_written(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "missing-dir" / "settings.json")
    monkeypatch.setattr(settings, "check_in_background", started.append)

    assert settings.save_settings.function(dict(SAVED)) == {}
    assert started == []


def run_check(monkeypatch, saved, check=None):
    recorder = LogRecorder()
    monkeypatch.setattr(settings, "log", recorder)
    if check is not None:
        monkeypatch.setattr(settings, "check_connection", check)
    settings.check_in_background(saved).join(timeout=5)
    return recorder.lines


def test_check_logs_that_an_empty_host_disables_logging(monkeypatch):
    lines = run_check(monkeypatch, {"db_host": ""})

    assert [level for level, _ in lines] == ["info"]
    assert "disabled" in lines[0][1]


def test_check_logs_success_as_info_and_failure_as_warning(monkeypatch):
    ok = CheckResult(True, "write", "db.example.org:3306")
    failed = CheckResult(False, "write", "OperationalError(1142) db.example.org:3306")

    assert run_check(monkeypatch, SAVED, lambda config, warn=None: ok)[0][0] == "info"
    lines = run_check(monkeypatch, SAVED, lambda config, warn=None: failed)

    assert lines[0][0] == "warning"
    assert "write" in lines[0][1] and "1142" in lines[0][1]


def test_check_never_raises_and_never_logs_the_password(monkeypatch):
    def broken(config, warn=None):
        raise RuntimeError(f"driver text with {LEAK_MARKER}")

    lines = run_check(monkeypatch, SAVED, broken)

    assert lines[0][0] == "warning"
    assert "RuntimeError" in lines[0][1]
    assert all(LEAK_MARKER not in message for _, message in lines)


def test_check_runs_in_a_daemon_thread_so_the_save_is_not_delayed(monkeypatch):
    release = settings.threading.Event()
    monkeypatch.setattr(settings, "log", LogRecorder())
    monkeypatch.setattr(
        settings, "check_connection", lambda config, warn=None: release.wait(5)
    )

    thread = settings.check_in_background(SAVED)

    assert thread.daemon is True
    assert thread.is_alive()
    release.set()
    thread.join(timeout=5)


def test_every_check_line_carries_the_plugin_prefix_including_the_tls_warning(monkeypatch):
    def check(config, warn=None):
        warn("TLS is not required for the database connection to db.example.org:3306")
        return CheckResult(True, "write", "db.example.org:3306")

    lines = run_check(monkeypatch, {**SAVED, "db_require_ssl": False}, check)

    assert len(lines) == 2
    assert all(message.startswith("RAG Interaction Logger: ") for _, message in lines)
