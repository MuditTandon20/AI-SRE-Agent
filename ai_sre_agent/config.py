"""Explicit configuration; imports never open database connections."""

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class ConfigurationError(ValueError):
    """Invalid configuration, with input values excluded from the error."""


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    postgres_db: str = Field(min_length=1)
    postgres_user: str = Field(min_length=1)
    postgres_password: SecretStr = Field(min_length=1)
    postgres_host: str = Field(default="localhost", min_length=1)
    postgres_port: int = Field(default=5433, ge=1, le=65535)
    database_timeout_seconds: int = Field(default=3, ge=1, le=30)
    database_startup_timeout_seconds: int = Field(default=10, ge=1, le=60)
    database_statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    api_token: SecretStr | None = None
    approver_token: SecretStr | None = None
    webhook_token: SecretStr | None = None
    execution_mode: Literal["fixture", "live"] = "fixture"
    llm_provider: Literal["deterministic", "openai"] = "deterministic"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-6.1-sol"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    tempo_url: str | None = None
    github_repository: str | None = None
    github_token: SecretStr | None = None
    connector_timeout_seconds: int = Field(default=10, ge=1, le=30)
    max_response_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_query_minutes: int = Field(default=15, ge=1, le=60)
    investigation_seconds: int = Field(default=120, ge=10, le=600)
    max_tool_calls: int = Field(default=10, ge=1, le=30)
    max_model_calls: int = Field(default=4, ge=1, le=10)
    max_input_chars: int = Field(default=48000, ge=1000, le=100000)
    max_output_tokens: int = Field(default=2000, ge=256, le=8000)
    worker_poll_seconds: float = Field(default=1, ge=0.1, le=60)
    max_job_attempts: int = Field(default=3, ge=1, le=5)
    queue_limit: int = Field(default=100, ge=1, le=10000)
    notification_mode: Literal["disabled", "fixture", "live"] = "disabled"
    delivery_token: SecretStr | None = None
    ses_min_severity: Literal["SEV1", "SEV2", "SEV3"] = "SEV3"
    slack_min_severity: Literal["SEV1", "SEV2", "SEV3"] = "SEV3"
    slack_mcp_url: str | None = None
    slack_mcp_token: SecretStr | None = None
    slack_channel: str | None = None
    slack_tool_name: str = "slack_send_message"
    ses_region: str = "us-east-1"
    ses_sender: str | None = None
    ses_recipient: str | None = None
    runbooks_path: str | None = None
    retention_days: int = Field(default=30, ge=1, le=3650)
    executor_url: str | None = None
    executor_token: SecretStr | None = None
    remediation_enabled: bool = False
    decision_provider: Literal["disabled", "rules", "jev"] = "disabled"
    typesafe_api_key: SecretStr | None = None
    jev_model: str = "jev-latest"
    kubernetes_url: str | None = None
    kubernetes_token: SecretStr | None = None
    kubernetes_namespace: str = Field(default="default", pattern=r"^[a-z0-9-]{1,63}$")
    kubernetes_ca_file: str | None = None
    postgres_sslmode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = "prefer"
    otel_enabled: bool = False

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        values = dotenv_values(env_file) if env_file is not None else {}
        values.update(os.environ)
        fields = {name: values[name.upper()] for name in cls.model_fields if name.upper() in values}
        try:
            return cls.model_validate(fields)
        except ValidationError as exc:
            details = "; ".join(
                f"{error['loc'][0].upper()}: {error['msg']}" for error in exc.errors()
            )
            raise ConfigurationError(f"Invalid configuration: {details}") from None

    def connection_kwargs(self) -> dict:
        return {
            "host": self.postgres_host,
            "port": self.postgres_port,
            "dbname": self.postgres_db,
            "user": self.postgres_user,
            "password": self.postgres_password.get_secret_value(),
            "connect_timeout": self.database_timeout_seconds,
            "sslmode": self.postgres_sslmode,
            "options": f"-c statement_timeout={self.database_statement_timeout_ms}",
        }
