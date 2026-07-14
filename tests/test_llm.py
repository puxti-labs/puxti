"""Tests for the LLM provider layer — AnthropicBackend and error normalization.

Anthropic-SDK-shaped mocks live here: the engines are tested against the
LLMBackend protocol, so this is the only file that knows what an Anthropic
response object looks like.
"""

from unittest.mock import AsyncMock, MagicMock

import anthropic
import openai
import pytest

from puxti.llm import (
    LLM_MODEL,
    AnthropicBackend,
    LLMAuthError,
    LLMBillingError,
    LLMConfigError,
    OpenAICompatBackend,
    get_backend,
)
from puxti.settings import settings


def _sdk_response(text: str, stop_reason: str = "end_turn") -> MagicMock:
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.stop_reason = stop_reason
    return response


def _make_client(**message_mocks) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    for name, mock in message_mocks.items():
        setattr(client.messages, name, mock)
    return client


# ── complete ──────────────────────────────────────────────────────────────────

async def test_complete_extracts_text_and_is_not_truncated():
    client = _make_client(create=AsyncMock(return_value=_sdk_response('{"a": 1}')))
    backend = AnthropicBackend(client=client)

    response = await backend.complete(system="sys", user_message="msg", max_tokens=256)

    assert response.text == '{"a": 1}'
    assert response.truncated is False
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == LLM_MODEL
    assert kwargs["max_tokens"] == 256
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "msg"}]


async def test_complete_flags_truncation_on_max_tokens_stop():
    client = _make_client(
        create=AsyncMock(return_value=_sdk_response('{"partial', stop_reason="max_tokens"))
    )
    backend = AnthropicBackend(client=client)

    response = await backend.complete(system="sys", user_message="msg", max_tokens=64)

    assert response.truncated is True


async def test_complete_maps_credit_exhaustion_to_billing_error():
    error = anthropic.BadRequestError(
        message="Your credit balance is too low to access the Anthropic API.",
        response=MagicMock(status_code=400),
        body={"type": "invalid_request_error"},
    )
    client = _make_client(create=AsyncMock(side_effect=error))
    backend = AnthropicBackend(client=client)

    with pytest.raises(LLMBillingError, match="credit balance"):
        await backend.complete(system="sys", user_message="msg", max_tokens=256)


async def test_complete_reraises_other_bad_requests_unchanged():
    error = anthropic.BadRequestError(
        message="max_tokens is too large",
        response=MagicMock(status_code=400),
        body={"type": "invalid_request_error"},
    )
    client = _make_client(create=AsyncMock(side_effect=error))
    backend = AnthropicBackend(client=client)

    with pytest.raises(anthropic.BadRequestError):
        await backend.complete(system="sys", user_message="msg", max_tokens=256)


async def test_complete_maps_authentication_error():
    error = anthropic.AuthenticationError(
        message="Invalid API key",
        response=MagicMock(status_code=401),
        body={},
    )
    client = _make_client(create=AsyncMock(side_effect=error))
    backend = AnthropicBackend(client=client)

    with pytest.raises(LLMAuthError, match="ANTHROPIC_API_KEY"):
        await backend.complete(system="sys", user_message="msg", max_tokens=256)


# ── count_input_tokens ────────────────────────────────────────────────────────

async def test_count_input_tokens_is_exact_and_passes_system():
    count_response = MagicMock()
    count_response.input_tokens = 321
    client = _make_client(count_tokens=AsyncMock(return_value=count_response))
    backend = AnthropicBackend(client=client)

    count = await backend.count_input_tokens("msg", system="sys")

    assert count.tokens == 321
    assert count.exact is True
    assert client.messages.count_tokens.call_args.kwargs["system"] == "sys"


async def test_count_input_tokens_omits_system_when_none():
    count_response = MagicMock()
    count_response.input_tokens = 10
    client = _make_client(count_tokens=AsyncMock(return_value=count_response))
    backend = AnthropicBackend(client=client)

    await backend.count_input_tokens("msg")

    assert "system" not in client.messages.count_tokens.call_args.kwargs


# ── auth_check ────────────────────────────────────────────────────────────────

async def test_auth_check_passes_on_success():
    count_response = MagicMock()
    count_response.input_tokens = 1
    client = _make_client(count_tokens=AsyncMock(return_value=count_response))

    await AnthropicBackend(client=client).auth_check()  # no exception


async def test_auth_check_maps_authentication_error():
    error = anthropic.AuthenticationError(
        message="Invalid API key", response=MagicMock(status_code=401), body={}
    )
    client = _make_client(count_tokens=AsyncMock(side_effect=error))

    with pytest.raises(LLMAuthError):
        await AnthropicBackend(client=client).auth_check()


async def test_auth_check_maps_credit_exhaustion():
    error = anthropic.BadRequestError(
        message="Your credit balance is too low to access the Anthropic API.",
        response=MagicMock(status_code=400),
        body={"type": "invalid_request_error"},
    )
    client = _make_client(count_tokens=AsyncMock(side_effect=error))

    with pytest.raises(LLMBillingError):
        await AnthropicBackend(client=client).auth_check()


# ── contract ──────────────────────────────────────────────────────────────────

def test_llm_errors_are_runtime_errors():
    """CLI command wrappers catch RuntimeError for actionable messages — the
    normalized errors must stay inside that contract."""
    assert issubclass(LLMAuthError, RuntimeError)
    assert issubclass(LLMBillingError, RuntimeError)


def test_get_backend_returns_configured_anthropic_backend():
    backend = get_backend()
    assert isinstance(backend, AnthropicBackend)
    assert backend.provider == "anthropic"
    assert backend.model == LLM_MODEL
    assert backend.input_cost_per_mtok is not None
    assert backend.output_cost_per_mtok is not None


# ── OpenAICompatBackend ───────────────────────────────────────────────────────

def _compat_response(text: str, finish_reason: str = "stop") -> MagicMock:
    choice = MagicMock()
    choice.message.content = text
    choice.finish_reason = finish_reason
    response = MagicMock()
    response.choices = [choice]
    return response


def _make_compat_client(**mocks) -> MagicMock:
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    for name, mock in mocks.items():
        if name == "create":
            client.chat.completions.create = mock
        else:
            setattr(client, name, mock)
    return client


def _compat_backend(client: MagicMock, provider: str = "mistral") -> OpenAICompatBackend:
    return OpenAICompatBackend(
        provider=provider, model="some-model",
        base_url="https://example.invalid/v1", client=client,
    )


async def test_compat_complete_extracts_text_and_sends_system_message():
    client = _make_compat_client(create=AsyncMock(return_value=_compat_response('{"a": 1}')))
    backend = _compat_backend(client)

    response = await backend.complete(system="sys", user_message="msg", max_tokens=256)

    assert response.text == '{"a": 1}'
    assert response.truncated is False
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "some-model"
    assert kwargs["max_tokens"] == 256
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg"},
    ]


async def test_compat_complete_flags_truncation_on_length_finish():
    client = _make_compat_client(
        create=AsyncMock(return_value=_compat_response('{"partial', finish_reason="length"))
    )
    backend = _compat_backend(client)

    response = await backend.complete(system="sys", user_message="msg", max_tokens=64)

    assert response.truncated is True


async def test_compat_complete_maps_authentication_error():
    error = openai.AuthenticationError(
        message="Incorrect API key provided",
        response=MagicMock(status_code=401),
        body={},
    )
    client = _make_compat_client(create=AsyncMock(side_effect=error))
    backend = _compat_backend(client)

    with pytest.raises(LLMAuthError, match="LLM_API_KEY"):
        await backend.complete(system="sys", user_message="msg", max_tokens=256)


async def test_compat_complete_maps_quota_exhaustion_to_billing_error():
    error = openai.RateLimitError(
        message="You exceeded your current quota, please check your plan and billing details.",
        response=MagicMock(status_code=429),
        body={"code": "insufficient_quota"},
    )
    client = _make_compat_client(create=AsyncMock(side_effect=error))
    backend = _compat_backend(client)

    with pytest.raises(LLMBillingError, match="credits or quota"):
        await backend.complete(system="sys", user_message="msg", max_tokens=256)


async def test_compat_complete_reraises_plain_rate_limits():
    error = openai.RateLimitError(
        message="Rate limit reached, retry after 2s",
        response=MagicMock(status_code=429),
        body={"code": "rate_limit_exceeded"},
    )
    client = _make_compat_client(create=AsyncMock(side_effect=error))
    backend = _compat_backend(client)

    with pytest.raises(openai.RateLimitError):
        await backend.complete(system="sys", user_message="msg", max_tokens=256)


async def test_compat_count_input_tokens_is_approximate():
    backend = _compat_backend(_make_compat_client())

    count = await backend.count_input_tokens("x" * 400, system="y" * 400)

    assert count.exact is False
    assert count.tokens == 200  # (400 + 400) / 4 chars per token


async def test_compat_auth_check_passes_on_models_list():
    client = _make_compat_client(models=MagicMock(list=AsyncMock()))
    await _compat_backend(client).auth_check()  # no exception


async def test_compat_auth_check_tolerates_missing_models_endpoint():
    error = openai.NotFoundError(
        message="Not found", response=MagicMock(status_code=404), body={},
    )
    client = _make_compat_client(models=MagicMock(list=AsyncMock(side_effect=error)))
    await _compat_backend(client).auth_check()  # 404 is not an auth failure


async def test_compat_auth_check_maps_authentication_error():
    error = openai.AuthenticationError(
        message="Incorrect API key", response=MagicMock(status_code=401), body={},
    )
    client = _make_compat_client(models=MagicMock(list=AsyncMock(side_effect=error)))
    with pytest.raises(LLMAuthError):
        await _compat_backend(client).auth_check()


# ── get_backend routing ───────────────────────────────────────────────────────

def _configure(monkeypatch, **overrides):
    defaults = {
        "llm_provider": "anthropic", "llm_model": "", "llm_api_key": "",
        "llm_base_url": "", "anthropic_api_key": "sk-ant-test",
        "llm_input_cost_per_mtok": None, "llm_output_cost_per_mtok": None,
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(settings, name, value)


def test_get_backend_defaults_to_anthropic(monkeypatch):
    _configure(monkeypatch)
    backend = get_backend()
    assert isinstance(backend, AnthropicBackend)
    assert backend.model == LLM_MODEL
    assert backend.key_configured is True


def test_get_backend_provider_requires_model(monkeypatch):
    _configure(monkeypatch, llm_provider="mistral", llm_api_key="mk-1")
    with pytest.raises(LLMConfigError, match="LLM_MODEL"):
        get_backend()


def test_get_backend_provider_requires_key(monkeypatch):
    _configure(monkeypatch, llm_provider="mistral", llm_model="mistral-large-latest")
    with pytest.raises(LLMConfigError, match="LLM_API_KEY"):
        get_backend()


def test_get_backend_resolves_preset_base_url(monkeypatch):
    _configure(monkeypatch, llm_provider="mistral",
               llm_model="mistral-large-latest", llm_api_key="mk-1")
    backend = get_backend()
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.provider == "mistral"
    assert str(backend._client.base_url).startswith("https://api.mistral.ai/v1")


def test_get_backend_base_url_overrides_preset(monkeypatch):
    _configure(monkeypatch, llm_provider="bedrock", llm_model="qwen.qwen3-32b",
               llm_api_key="bk-1",
               llm_base_url="https://bedrock-mantle.eu-central-1.api.aws/v1")
    backend = get_backend()
    assert "eu-central-1" in str(backend._client.base_url)


def test_get_backend_ollama_needs_no_key(monkeypatch):
    _configure(monkeypatch, llm_provider="ollama", llm_model="llama3.1")
    backend = get_backend()
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.key_configured is True


def test_get_backend_custom_requires_base_url(monkeypatch):
    _configure(monkeypatch, llm_provider="custom", llm_model="my-model")
    with pytest.raises(LLMConfigError, match="LLM_BASE_URL"):
        get_backend()


def test_get_backend_unknown_provider_lists_valid_ones(monkeypatch):
    _configure(monkeypatch, llm_provider="watsonx")
    with pytest.raises(LLMConfigError, match="mistral"):
        get_backend()


def test_get_backend_anthropic_prefers_llm_api_key(monkeypatch):
    _configure(monkeypatch, llm_api_key="sk-ant-newer", anthropic_api_key="")
    backend = get_backend()
    assert backend.key_configured is True


# ── pricing resolution ────────────────────────────────────────────────────────

def test_unknown_model_has_no_pricing(monkeypatch):
    _configure(monkeypatch, llm_provider="mistral",
               llm_model="mistral-large-latest", llm_api_key="mk-1")
    backend = get_backend()
    assert backend.input_cost_per_mtok is None
    assert backend.output_cost_per_mtok is None


def test_pricing_env_overrides_apply(monkeypatch):
    _configure(monkeypatch, llm_provider="mistral",
               llm_model="mistral-large-latest", llm_api_key="mk-1",
               llm_input_cost_per_mtok=2.0, llm_output_cost_per_mtok=6.0)
    backend = get_backend()
    assert backend.input_cost_per_mtok == 2.0
    assert backend.output_cost_per_mtok == 6.0


def test_custom_anthropic_model_has_no_builtin_pricing(monkeypatch):
    _configure(monkeypatch, llm_model="claude-other-model")
    backend = get_backend()
    assert backend.input_cost_per_mtok is None
