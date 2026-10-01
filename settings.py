"""Settings exposed by Cheshire Cat AI's plugin administration panel."""

from pydantic import BaseModel, Field, field_validator

from cat.mad_hatter.decorators import plugin


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
