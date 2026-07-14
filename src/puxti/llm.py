"""LLM provider layer (BYOK).

Single source of truth for provider routing, model IDs, pricing, and the
backend abstraction every engine calls through. Engines never touch a provider
SDK directly — they depend on the `LLMBackend` protocol.

Two backends cover the provider landscape:
- `AnthropicBackend` — native Anthropic API (the default; exact token counts)
- `OpenAICompatBackend` — any provider speaking the OpenAI wire format, routed
  by preset (OpenAI, Mistral, GLM, DeepSeek, Groq, OpenRouter, Gemini, AWS
  Bedrock via Mantle, local Ollama) or a custom base URL (vLLM, proxies, …)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

import anthropic
import openai

from puxti.settings import settings

_logger = logging.getLogger(__name__)

# Default model when the provider is anthropic and LLM_MODEL is not set —
# never in the read path
LLM_MODEL = "claude-sonnet-4-6"

# Pricing for LLM_MODEL (USD per million tokens). Third-party model prices are
# never hardcoded — they drift and a wrong dollar figure is worse than none.
# Users supply LLM_INPUT_COST_PER_MTOK / LLM_OUTPUT_COST_PER_MTOK instead.
INPUT_COST_PER_MTOK = 3.00
OUTPUT_COST_PER_MTOK = 15.00

# OpenAI-compatible provider presets. All of these speak the OpenAI wire
# format at their own base URL; LLM_BASE_URL overrides a preset (e.g. a
# non-default Bedrock region).
_PRESETS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "bedrock": "https://bedrock-mantle.us-east-1.api.aws/v1",
    "ollama": "http://localhost:11434/v1",
}

# Providers that work without an API key (local inference).
_KEY_OPTIONAL = {"ollama", "custom"}

# Rough chars-per-token ratio for the approximate counter. English prose and
# SQL both land near 4; being ~20% off is acceptable for a labeled estimate.
_CHARS_PER_TOKEN = 4


# Shown by --dry-run panels when a model's pricing is unknown.
COST_UNKNOWN_HINT = (
    "unknown for this model — set LLM_INPUT_COST_PER_MTOK "
    "and LLM_OUTPUT_COST_PER_MTOK"
)


class LLMConfigError(RuntimeError):
    """The LLM provider configuration is incomplete or invalid."""


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
    key_configured: bool
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


def _resolve_pricing(model: str) -> tuple[float | None, float | None]:
    """Pricing for a model: explicit env overrides win; the built-in default
    model has known pricing; everything else is unknown (None, None)."""
    if (
        settings.llm_input_cost_per_mtok is not None
        and settings.llm_output_cost_per_mtok is not None
    ):
        return settings.llm_input_cost_per_mtok, settings.llm_output_cost_per_mtok
    if model == LLM_MODEL:
        return INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK
    return None, None


class AnthropicBackend:
    """Native Anthropic Messages API backend — the default provider."""

    provider = "anthropic"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else settings.anthropic_api_key
        self._client = client or anthropic.AsyncAnthropic(api_key=resolved_key)
        self.model = model or settings.llm_model or LLM_MODEL
        self.key_configured = bool(resolved_key) or client is not None
        self.input_cost_per_mtok, self.output_cost_per_mtok = _resolve_pricing(self.model)

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


class OpenAICompatBackend:
    """Any provider speaking the OpenAI Chat Completions wire format.

    Covers OpenAI, Mistral, GLM/Zhipu, DeepSeek, Groq, OpenRouter, Gemini's
    compat endpoint, AWS Bedrock (Mantle), local Ollama/vLLM, and anything
    reachable via LLM_BASE_URL. The `openai` package is only the wire client.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str,
        api_key: str = "",
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        # The SDK requires a non-empty key even for keyless local servers.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key or "not-required", base_url=base_url
        )
        self.key_configured = bool(api_key) or provider in _KEY_OPTIONAL or client is not None
        self.input_cost_per_mtok, self.output_cost_per_mtok = _resolve_pricing(model)

    async def complete(
        self, system: str, user_message: str, max_tokens: int
    ) -> LLMResponse:
        _logger.debug(
            "LLM call | provider=%s model=%s prompt_chars=%d hash=%s",
            self.provider,
            self.model,
            len(user_message),
            hashlib.sha256(user_message.encode()).hexdigest()[:12],
        )
        try:
            # max_tokens (not max_completion_tokens) — it is the parameter the
            # compat ecosystem (Mistral, Groq, Ollama, Mantle, …) implements.
            response = await self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
        except openai.AuthenticationError as exc:
            raise LLMAuthError(
                f"{self.provider} rejected the API key. Check LLM_API_KEY."
            ) from exc
        except openai.RateLimitError as exc:
            # OpenAI-compatible providers signal an out-of-credit account as a
            # 429 with an insufficient_quota code — that's billing, not pacing.
            if "quota" in str(exc).lower() or "billing" in str(exc).lower():
                raise LLMBillingError(
                    f"{self.provider} account has insufficient credits or quota. "
                    "Check your plan and billing details."
                ) from exc
            raise
        choice = response.choices[0]
        return LLMResponse(
            text=choice.message.content or "",
            truncated=choice.finish_reason == "length",
        )

    async def count_input_tokens(
        self, user_message: str, system: str | None = None
    ) -> TokenCount:
        """Approximate count — the OpenAI wire format has no free counting
        endpoint, and per-model tokenizers vary across providers."""
        chars = len(user_message) + (len(system) if system else 0)
        return TokenCount(tokens=max(1, chars // _CHARS_PER_TOKEN), exact=False)

    async def auth_check(self) -> None:
        """GET /models is free on every major compat provider. Providers that
        don't implement it (404) still prove the endpoint is reachable and the
        key wasn't rejected."""
        try:
            await self._client.models.list()
        except openai.AuthenticationError as exc:
            raise LLMAuthError(
                f"{self.provider} rejected the API key. Check LLM_API_KEY."
            ) from exc
        except openai.NotFoundError:
            pass


def get_backend() -> LLMBackend:
    """Build the LLM backend from settings. Raises LLMConfigError with an
    actionable message when the provider configuration is incomplete."""
    provider = (settings.llm_provider or "anthropic").strip().lower()

    if provider == "anthropic":
        return AnthropicBackend(api_key=settings.llm_api_key or settings.anthropic_api_key)

    if provider == "custom":
        base_url = settings.llm_base_url
        if not base_url:
            raise LLMConfigError(
                "LLM_PROVIDER=custom requires LLM_BASE_URL "
                "(an OpenAI-compatible endpoint, e.g. your vLLM server)."
            )
    else:
        preset = _PRESETS.get(provider)
        if preset is None:
            raise LLMConfigError(
                f"Unknown LLM_PROVIDER '{provider}'. "
                f"Valid providers: anthropic, {', '.join(sorted(_PRESETS))}, custom."
            )
        base_url = settings.llm_base_url or preset

    if not settings.llm_model:
        raise LLMConfigError(
            f"LLM_MODEL is required when LLM_PROVIDER='{provider}' "
            "(e.g. LLM_MODEL=mistral-large-latest)."
        )
    if provider not in _KEY_OPTIONAL and not settings.llm_api_key:
        raise LLMConfigError(
            f"LLM_API_KEY is required for provider '{provider}'."
        )

    return OpenAICompatBackend(
        provider=provider,
        model=settings.llm_model,
        base_url=base_url,
        api_key=settings.llm_api_key,
    )


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
