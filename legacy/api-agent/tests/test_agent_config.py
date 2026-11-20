"""Agent configuration: off by default, strictly validated, and never echoing a key."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from universal_ai_tools.agent.config import (
    ENV_AGENT_MODEL,
    ENV_AGENT_PROVIDER,
    ENV_ANTHROPIC_API_KEY,
    ENV_ENABLE_AGENT_TOOLS,
    ENV_MAX_REPEATED_CALLS,
    ENV_MAX_STEPS,
    ENV_MAX_TASK_SECONDS,
    ENV_OPENAI_API_KEY,
    ENV_REQUEST_TIMEOUT,
    AgentConfig,
    AgentConfigError,
    agent_tools_enabled,
    load_agent_config,
)
from universal_ai_tools.config import ConfigError

KEY = "sk-ant-SECRET-value-0123456789"


def enabled(**extra: str) -> dict[str, str]:
    return {
        ENV_ENABLE_AGENT_TOOLS: "true",
        ENV_AGENT_PROVIDER: "anthropic",
        ENV_AGENT_MODEL: "claude-test",
        ENV_ANTHROPIC_API_KEY: KEY,
        **extra,
    }


def test_disabled_by_default() -> None:
    assert load_agent_config({}) is None
    assert agent_tools_enabled({}) is False


@pytest.mark.parametrize("value", ["", "   ", "false", "FALSE", " False "])
def test_disabled_values(value: str) -> None:
    assert load_agent_config({ENV_ENABLE_AGENT_TOOLS: value}) is None


def test_disabled_reads_nothing_else() -> None:
    class Recording(dict[str, str]):
        def __init__(self) -> None:
            super().__init__({ENV_ENABLE_AGENT_TOOLS: "false", ENV_AGENT_PROVIDER: "nonsense"})
            self.read: list[str] = []

        def get(self, key: str, default: object = None) -> str:
            self.read.append(key)
            return super().get(key, default)  # type: ignore[arg-type]

    source = Recording()

    assert load_agent_config(source) is None
    assert source.read == [ENV_ENABLE_AGENT_TOOLS]


@pytest.mark.parametrize("value", ["1", "yes", "ON", "enabled", "truex", "true!"])
def test_flag_accepts_only_true_or_false(value: str) -> None:
    with pytest.raises(AgentConfigError, match=ENV_ENABLE_AGENT_TOOLS) as excinfo:
        load_agent_config({ENV_ENABLE_AGENT_TOOLS: value})
    assert value not in str(excinfo.value).replace(ENV_ENABLE_AGENT_TOOLS, "")


def test_agent_config_error_is_a_config_error() -> None:
    assert issubclass(AgentConfigError, ConfigError)


def test_enabled_configuration() -> None:
    config = load_agent_config(enabled())

    assert config is not None
    assert config.provider == "anthropic"
    assert config.model == "claude-test"
    assert config.api_key.get_secret_value() == KEY
    assert config.max_steps == 20


def test_provider_is_case_insensitive() -> None:
    config = load_agent_config(enabled(**{ENV_AGENT_PROVIDER: " Anthropic "}))
    assert config is not None
    assert config.provider == "anthropic"


def test_openai_reads_only_its_own_key() -> None:
    config = load_agent_config(
        enabled(**{ENV_AGENT_PROVIDER: "openai", ENV_OPENAI_API_KEY: "sk-openai-key"})
    )
    assert config is not None
    assert config.api_key.get_secret_value() == "sk-openai-key"


def test_missing_provider_model_and_key_are_all_reported() -> None:
    with pytest.raises(AgentConfigError) as excinfo:
        load_agent_config({ENV_ENABLE_AGENT_TOOLS: "true"})
    message = str(excinfo.value)
    assert ENV_AGENT_PROVIDER in message
    assert ENV_AGENT_MODEL in message


def test_missing_credentials() -> None:
    with pytest.raises(AgentConfigError, match=ENV_ANTHROPIC_API_KEY):
        load_agent_config(enabled(**{ENV_ANTHROPIC_API_KEY: "  "}))


def test_unsupported_provider() -> None:
    with pytest.raises(AgentConfigError, match="unsupported provider") as excinfo:
        load_agent_config(enabled(**{ENV_AGENT_PROVIDER: "gemini"}))
    assert "gemini" not in str(excinfo.value)


@pytest.mark.parametrize(
    "variable",
    ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "API_KEY", "UNIVERSAL_AI_TOOLS_AGENT_API_KEY"],
)
def test_generic_key_variables_are_never_used(variable: str) -> None:
    environ = enabled()
    del environ[ENV_ANTHROPIC_API_KEY]
    environ[variable] = KEY

    with pytest.raises(AgentConfigError, match=ENV_ANTHROPIC_API_KEY):
        load_agent_config(environ)


def test_the_other_providers_key_is_not_used() -> None:
    environ = enabled(**{ENV_AGENT_PROVIDER: "openai"})
    with pytest.raises(AgentConfigError, match=ENV_OPENAI_API_KEY):
        load_agent_config(environ)


@pytest.mark.parametrize("model", ["has space", "-leading-dash", "x" * 200, "a;b"])
def test_invalid_model_names(model: str) -> None:
    with pytest.raises(AgentConfigError, match=ENV_AGENT_MODEL):
        load_agent_config(enabled(**{ENV_AGENT_MODEL: model}))


def test_optional_limits() -> None:
    config = load_agent_config(
        enabled(
            **{
                ENV_MAX_STEPS: "7",
                ENV_REQUEST_TIMEOUT: "12.5",
                ENV_MAX_TASK_SECONDS: "90",
                ENV_MAX_REPEATED_CALLS: "2",
            }
        )
    )
    assert config is not None
    assert (config.max_steps, config.request_timeout_seconds) == (7, 12.5)
    assert (config.max_task_seconds, config.max_repeated_calls) == (90.0, 2)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (ENV_MAX_STEPS, "0"),
        (ENV_MAX_STEPS, "-1"),
        (ENV_MAX_STEPS, "2.5"),
        (ENV_MAX_STEPS, "ten"),
        (ENV_MAX_STEPS, "100000"),
        (ENV_REQUEST_TIMEOUT, "0"),
        (ENV_REQUEST_TIMEOUT, "nan"),
        (ENV_REQUEST_TIMEOUT, "inf"),
        (ENV_REQUEST_TIMEOUT, "1e999"),
        (ENV_REQUEST_TIMEOUT, "100000"),
        (ENV_MAX_TASK_SECONDS, "-5"),
    ],
)
def test_invalid_limits_are_refused(variable: str, value: str) -> None:
    with pytest.raises(AgentConfigError, match=variable):
        load_agent_config(enabled(**{variable: value}))


def test_the_key_never_appears_in_repr_str_or_dumps() -> None:
    config = load_agent_config(enabled())
    assert config is not None

    for text in (repr(config), str(config), config.model_dump_json(), str(config.model_dump())):
        assert KEY not in text


def test_errors_never_contain_the_key() -> None:
    with pytest.raises(AgentConfigError) as excinfo:
        load_agent_config(enabled(**{ENV_AGENT_MODEL: "bad model", ENV_MAX_STEPS: "x"}))
    assert KEY not in str(excinfo.value)


def test_config_is_frozen_and_closed() -> None:
    config = AgentConfig(provider="openai", model="m", api_key=SecretStr("k"))
    with pytest.raises(ValidationError):
        config.max_steps = 1000  # type: ignore[misc]
    with pytest.raises(ValidationError):
        AgentConfig(provider="openai", model="m", api_key=SecretStr("k"), base_url="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentConfig(provider="local", model="m", api_key=SecretStr("k"))  # type: ignore[arg-type]
