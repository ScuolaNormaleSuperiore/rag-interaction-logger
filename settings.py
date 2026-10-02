"""Settings exposed by Cheshire Cat AI's plugin administration panel."""

import json
import threading
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from cat.log import log
from cat.mad_hatter.decorators import plugin

# The Cat imports plugin files as `cat.plugins.<folder>.<module>`, so the
# relative import is the one that works at runtime; the absolute fallback is for
# the tests, which put the plugin folder on the path.
try:
    from .db import ConnectionConfig, check_connection
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from db import ConnectionConfig, check_connection

SETTINGS_PATH = Path(__file__).with_name("settings.json")


class RagInteractionLoggerSettings(BaseModel):
    """Configuration defaults for the interaction logger."""

    db_host: str = Field(
        default="",
        description="External MySQL or MariaDB host. Leave empty to disable logging.",
    )
    db_port: int = Field(default=3306, ge=1, le=65535, description="MySQL or MariaDB TCP port.")
    db_name: str = Field(default="", description="Database created manually by the DBA.")
    db_user: str = Field(default="", description="Dedicated database user (mysql_native_password).")
    db_password: str = Field(
        default="",
        description="Database password. Stored in plain text in settings.json.",
        json_schema_extra={"format": "password"},
    )
    db_require_ssl: bool = Field(
        default=True,
        description="Require TLS to the database. Disable only for local development.",
    )
    log_tool_input: bool = Field(
        default=False,
        description=(
            "Also save the input the LLM gives to each tool. It is free text that "
            "no guard checks and may contain personal data."
        ),
    )
    log_tool_output: bool = Field(
        default=False,
        description=(
            "Also save the text each tool returns. It comes from systems the logger "
            "does not know, no guard checks it, and it may contain personal data."
        ),
    )
    tool_text_limit: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Longest tool input or output saved, in characters; longer text is cut.",
    )
    create_table: bool = Field(
        default=True,
        description="Create ril_interactions when it is missing.",
    )
    queue_size: int = Field(
        default=1000,
        ge=1,
        description="Maximum completed records waiting for database insertion.",
    )
    retention_days: int = Field(
        default=0,
        ge=0,
        description="0 keeps records forever; a positive value keeps whole UTC days.",
    )

    @field_validator("db_host", "db_name", "db_user")
    @classmethod
    def strip_identifier(cls, value: str) -> str:
        return value.strip()


@plugin
def settings_model():
    """Return the model Cheshire Cat uses to render plugin settings."""
    return RagInteractionLoggerSettings


def write_settings(path: Path, settings: dict) -> dict:
    """Merge the new values over the saved ones and write them, as the core does.

    An override of `save_settings` replaces the core's own, so this repeats its
    behaviour: keep unknown saved keys, write `settings.json`, return the result
    or an empty dict when the file cannot be written.
    """
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        updated = {**saved, **settings}
        path.write_text(json.dumps(updated, indent=4), encoding="utf-8")
        return updated
    except Exception as error:
        log.error(f"RAG Interaction Logger: unable to save settings ({type(error).__name__})")
        return {}


def check_in_background(settings: dict) -> threading.Thread:
    """Try the saved connection settings and log the outcome without blocking the save."""

    def run():
        try:
            config = ConnectionConfig.from_settings(settings)
            if config is None:
                log.info("RAG Interaction Logger: db_host is empty, logging is disabled")
                return
            result = check_connection(
                config, warn=lambda message: log.warning(f"RAG Interaction Logger: {message}")
            )
            report = log.info if result.ok else log.warning
            report(f"RAG Interaction Logger: {result.message}")
        except Exception as error:
            log.warning(
                f"RAG Interaction Logger: connection check could not run ({type(error).__name__})"
            )

    thread = threading.Thread(target=run, name="ril-connection-check", daemon=True)
    thread.start()
    return thread


@plugin
def save_settings(settings):
    """Save the settings, then check the database connection in the background."""
    updated = write_settings(SETTINGS_PATH, settings)
    if updated:
        check_in_background(updated)
    return updated
