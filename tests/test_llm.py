"""Tests for the LLM provider layer — AnthropicBackend and error normalization.

Anthropic-SDK-shaped mocks live here: the engines are tested against the
LLMBackend protocol, so this is the only file that knows what an Anthropic
response object looks like.
"""

from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from puxti.llm import (
    LLM_MODEL,
    AnthropicBackend,
    LLMAuthError,
    LLMBillingError,
    get_backend,
)


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
