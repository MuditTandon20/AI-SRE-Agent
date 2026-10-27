import os

import pytest

from ai_sre_agent.config import ConfigurationError, Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_missing_configuration_has_actionable_names():
    with pytest.raises(ConfigurationError, match="POSTGRES_PASSWORD"):
        Settings.from_env(None)


def test_environment_overrides_dotenv_and_password_is_hidden(tmp_path, monkeypatch):
    file = tmp_path / ".env"
    file.write_text("POSTGRES_DB=demo\nPOSTGRES_USER=sre\nPOSTGRES_PASSWORD=secret-value\n")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    settings = Settings.from_env(file)
    assert settings.postgres_port == 6543
    assert settings.connection_kwargs()["password"] == "secret-value"
    assert "secret-value" not in repr(settings)
    assert "POSTGRES_PASSWORD" not in os.environ


def test_invalid_configuration_does_not_echo_input(tmp_path):
    file = tmp_path / ".env"
    file.write_text(
        "POSTGRES_DB=demo\nPOSTGRES_USER=sre\nPOSTGRES_PASSWORD=secret-value\n"
        "POSTGRES_PORT=sensitive-invalid-value\n"
    )
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env(file)
    assert "POSTGRES_PORT" in str(error.value)
    assert "sensitive-invalid-value" not in str(error.value)
    assert "secret-value" not in str(error.value)
