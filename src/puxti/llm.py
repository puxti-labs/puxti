"""LLM provider layer.

Single source of truth for the model ID, pricing, and the backend abstraction
every engine calls through. Engines never touch a provider SDK directly — they
depend on the `LLMBackend` protocol, so adding a provider is a new backend class
plus a routing entry in `get_backend()`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

import anthropic

from puxti.settings import settings

_logger = logging.getLogger(__name__)

# Model used for all semantic reasoning — never in the read path
LLM_MODEL = "claude-sonnet-4-6"

# Pricing for LLM_MODEL (USD per million tokens)
INPUT_COST_PER_MTOK = 3.00
OUTPUT_COST_PER_MTOK = 15.00


class LLMAuthError(RuntimeError):
    """The provider rejected the API key."""


class LLMBillingError(RuntimeError):
    """The API key is valid but the account cannot pay for the call."""


@dataclass
class LLMResponse:
    """Normalized completion result — provider response shapes stop here."""

    text: str
    truncated: bool  # the response hit the max_tokens output cap


@dataclass
class TokenCount:
    """Input-token count for a prospective call."""

    tokens: int
    exact: bool  # True when counted by the provider, False when estimated


class LLMBackend(Protocol):
    """What an LLM provider must offer. Implementations own all SDK specifics:
    client construction, response parsing, truncation detection, and mapping
    provider exceptions to LLMAuthError / LLMBillingError."""

    provider: str
    model: str
    # None when pricing for the model is unknown — callers must then omit
    # dollar estimates rather than print a wrong number.
    input_cost_per_mtok: float | None
    output_cost_per_mtok: float | None

    async def complete(
        self, system: str, user_message: str, max_tokens: int
    ) -> LLMResponse: ...

    async def count_input_tokens(
        self, user_message: str, system: str | None = None
    ) -> TokenCount: ...

    async def auth_check(self) -> None: ...


class AnthropicBackend:
    """Native Anthropic Messages API backend — the default provider."""

    provider = "anthropic"

    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key
        )
        self.model = LLM_MODEL
        self.input_cost_per_mtok: float | None = INPUT_COST_PER_MTOK
        self.output_cost_per_mtok: float | None = OUTPUT_COST_PER_MTOK

    async def complete(
        self, system: str, user_message: str, max_tokens: int
    ) -> LLMResponse:
        _logger.debug(
            "LLM call | model=%s prompt_chars=%d hash=%s",
            self.model,
            len(user_message),
            hashlib.sha256(user_message.encode()).hexdigest()[:12],
        )
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError(
                "Anthropic API key is invalid or expired. Check ANTHROPIC_API_KEY."
            ) from exc
        except anthropic.BadRequestError as exc:
            if "credit balance" in str(exc).lower():
                raise LLMBillingError(
                    "Anthropic API credit balance is too low. "
                    "Add credits at https://console.anthropic.com/settings/billing"
                ) from exc
            raise
        return LLMResponse(
            text=response.content[0].text,
            truncated=response.stop_reason == "max_tokens",
        )

    async def count_input_tokens(
        self, user_message: str, system: str | None = None
    ) -> TokenCount:
        """Exact input-token count via the count_tokens API — consumes no credits."""
        kwargs: dict = {"system": system} if system is not None else {}
        response = await self._client.messages.count_tokens(
            model=self.model,
            messages=[{"role": "user", "content": user_message}],
            **kwargs,
        )
        return TokenCount(tokens=response.input_tokens, exact=True)

    async def auth_check(self) -> None:
        """Cheapest authenticated call — count_tokens consumes no credits."""
        try:
            await self._client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
            )
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError(
                "Anthropic API key is invalid or expired. Check ANTHROPIC_API_KEY."
            ) from exc
        except anthropic.BadRequestError as exc:
            if "credit balance" in str(exc).lower():
                raise LLMBillingError(
                    "Anthropic API credit balance is too low. "
                    "Add credits at https://console.anthropic.com/settings/billing"
                ) from exc
            raise


def get_backend() -> LLMBackend:
    """Build the configured LLM backend. Anthropic is the only provider today;
    provider routing (settings.llm_provider) lands with the OpenAI-compatible
    backend."""
    return AnthropicBackend()


def strip_markdown_fences(raw: str) -> str:
    """Return raw LLM output with a wrapping markdown code fence removed.

    The LLM occasionally wraps its JSON response in ```json ... ``` fences
    despite instructions not to. Callers parse the returned string with
    json.loads and handle errors according to their own fallback policy.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
