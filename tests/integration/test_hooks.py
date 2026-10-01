"""Hook wiring tests; run inside a Cheshire Cat AI 1.9.2 container."""

from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logger = pytest.importorskip(
    "rag_interaction_logger",
    reason="requires Cheshire Cat AI; run python run-tests.py --integration",
)
settings = pytest.importorskip("settings")


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
