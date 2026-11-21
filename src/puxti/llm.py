"""LLM provider layer (BYOK).

Single source of truth for provider routing, model IDs, pricing, and the
backend abstraction every engine calls through. Engines never touch a provider
SDK directly — they depend on the `LLMBackend` protocol.

Two backends cover the provider landscape:
- `AnthropicBackend` — native Anthropic Messages API (the default; exact token
  counts). Also serves Claude on AWS Bedrock via Mantle's Anthropic endpoint
  (`bedrock-anthropic`), authenticated with a bearer token
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
LLM_MODEL = "claude-sonnet-5-5"

# Pricing for LLM_MODEL (USD per million tokens). Third-party model prices are
# never hardcoded — they drift and a wrong dollar figure is worse than none.
# Users supply LLM_INPUT_COST_PER_MTOK / LLM_OUTPUT_COST_PER_MTOK instead.
INPUT_COST_PER_MTOK = 2.00
OUTPUT_COST_PER_MTOK = 10.00

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

# Providers that speak the Anthropic Messages API at their own base URL, with a
# bearer token instead of x-api-key. LLM_BASE_URL overrides the preset.
_ANTHROPIC_NATIVE_PRESETS: dict[str, str] = {
    "bedrock-anthropic": "https://bedrock-mantle.us-east-1.api.aws/anthropic",
}

# Providers that work without an API key (local inference).
_KEY_OPTIONAL = {"ollama", "custom"}

# Rough chars-per-token ratio for the approximate counter. English prose and
# SQL both land near 4; being ~20% off is acceptable for a labeled estimate.
_CHARS_PER_TOKEN = 4


# HTTP statuses a third-party Anthropic-compatible endpoint may answer with when
# it does not implement count_tokens.
_COUNT_UNSUPPORTED_STATUSES = {404, 405, 501}


def _approximate_token_count(user_message: str, system: str | None) -> TokenCount:
    chars = len(user_message) + (len(system) if system else 0)
    return TokenCount(tokens=max(1, chars // _CHARS_PER_TOKEN), exact=False)


# Shown by --dry-run panels when a model's pricing is unknown.
COST_UNKNOWN_HINT = (
    "unknown for this model — set LLM_INPUT_COST_PER_MTOK and LLM_OUTPUT_COST_PER_MTOK"
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

    async def complete(self, system: str, user_message: str, max_tokens: int) -> LLMResponse: ...

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
    """Anthropic Messages API backend — the default provider, and any provider
    exposing the same API at another base URL (Bedrock Mantle).

    With `auth_token` the client authenticates with `Authorization: Bearer`
    and does not send ANTHROPIC_API_KEY, so that key cannot reach a
    third-party endpoint. This holds from anthropic 1.0.0; earlier SDKs sent
    both headers, which is why pyproject.toml pins that floor.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        api_key: str | None = None,
        model: str | None = None,
        provider: str = "anthropic",
        base_url: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.provider = provider
        if auth_token is not None:
            resolved_key = auth_token
            credentials: dict[str, str | None] = {"auth_token": auth_token}
        else:
            resolved_key = api_key if api_key is not None else settings.anthropic_api_key
            credentials = {"api_key": resolved_key}
        self._client = client or anthropic.AsyncAnthropic(base_url=base_url, **credentials)
        self.model = model or settings.llm_model or LLM_MODEL
        self.key_configured = bool(resolved_key) or client is not None
        self.input_cost_per_mtok, self.output_cost_per_mtok = _resolve_pricing(self.model)

    def _auth_message(self) -> str:
        if self.provider == "anthropic":
            return "Anthropic API key is invalid or expired. Check ANTHROPIC_API_KEY."
        return f"{self.provider} rejected the API key. Check LLM_API_KEY."

    @property
    def _is_native(self) -> bool:
        return self.provider == "anthropic"

    def _count_unsupported(self, exc: anthropic.APIStatusError) -> bool:
        """True when a non-Anthropic endpoint does not implement count_tokens.
        The default provider always supports it, so there errors propagate."""
        return not self._is_native and exc.status_code in _COUNT_UNSUPPORTED_STATUSES

    def _billing_message(self) -> str:
        if self.provider == "anthropic":
            return (
                "Anthropic API credit balance is too low. "
                "Add credits at https://console.anthropic.com/settings/billing"
            )
        return (
            f"{self.provider} account has insufficient credits or quota. "
            "Check your plan and billing details."
        )

    async def complete(self, system: str, user_message: str, max_tokens: int) -> LLMResponse:
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
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            if self._is_native and isinstance(exc, anthropic.PermissionDeniedError):
                raise
            raise LLMAuthError(self._auth_message()) from exc
        except anthropic.BadRequestError as exc:
            if "credit balance" in str(exc).lower():
                raise LLMBillingError(self._billing_message()) from exc
            raise
        return LLMResponse(
            # Thinking models can lead with a thinking block, which has no text.
            text="".join(b.text for b in response.content if b.type == "text"),
            truncated=response.stop_reason == "max_tokens",
        )

    async def count_input_tokens(self, user_message: str, system: str | None = None) -> TokenCount:
        """Exact input-token count via the count_tokens API — consumes no credits.

        A non-Anthropic endpoint that lacks count_tokens gets an estimate."""
        kwargs: dict = {"system": system} if system is not None else {}
        try:
            response = await self._client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": user_message}],
                **kwargs,
            )
        except anthropic.APIStatusError as exc:
            if not self._count_unsupported(exc):
                raise
            _logger.debug("count_tokens unsupported by %s; estimating", self.provider)
            return _approximate_token_count(user_message, system)
        return TokenCount(tokens=response.input_tokens, exact=True)

    async def auth_check(self) -> None:
        """Cheapest authenticated call — count_tokens consumes no credits.

        A non-Anthropic endpoint without count_tokens is checked with a
        one-token completion instead."""
        try:
            await self._client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
            )
        except anthropic.APIStatusError as exc:
            if self._count_unsupported(exc):
                await self.complete(system="", user_message="ping", max_tokens=1)
                return
            if isinstance(exc, anthropic.AuthenticationError) or (
                not self._is_native and isinstance(exc, anthropic.PermissionDeniedError)
            ):
                raise LLMAuthError(self._auth_message()) from exc
            if isinstance(exc, anthropic.BadRequestError) and (
                "credit balance" in str(exc).lower()
            ):
                raise LLMBillingError(self._billing_message()) from exc
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

    async def complete(self, system: str, user_message: str, max_tokens: int) -> LLMResponse:
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
            raise LLMAuthError(f"{self.provider} rejected the API key. Check LLM_API_KEY.") from exc
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

    async def count_input_tokens(self, user_message: str, system: str | None = None) -> TokenCount:
        """Approximate count — the OpenAI wire format has no free counting
        endpoint, and per-model tokenizers vary across providers."""
        return _approximate_token_count(user_message, system)

    async def auth_check(self) -> None:
        """GET /models is free on every major compat provider. Providers that
        don't implement it (404) still prove the endpoint is reachable and the
        key wasn't rejected."""
        try:
            await self._client.models.list()
        except openai.AuthenticationError as exc:
            raise LLMAuthError(f"{self.provider} rejected the API key. Check LLM_API_KEY.") from exc
        except openai.NotFoundError:
            pass


def get_backend() -> LLMBackend:
    """Build the LLM backend from settings. Raises LLMConfigError with an
    actionable message when the provider configuration is incomplete."""
    provider = (settings.llm_provider or "anthropic").strip().lower()

    if provider == "anthropic":
        return AnthropicBackend(api_key=settings.llm_api_key or settings.anthropic_api_key)

    if provider in _ANTHROPIC_NATIVE_PRESETS:
        if not settings.llm_model:
            raise LLMConfigError(
                f"LLM_MODEL is required when LLM_PROVIDER='{provider}' "
                "(the Bedrock model ID of a Claude model)."
            )
        if not settings.llm_api_key:
            raise LLMConfigError(f"LLM_API_KEY is required for provider '{provider}'.")
        return AnthropicBackend(
            provider=provider,
            model=settings.llm_model,
            base_url=settings.llm_base_url or _ANTHROPIC_NATIVE_PRESETS[provider],
            auth_token=settings.llm_api_key,
        )

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
                "Valid providers: anthropic, "
                f"{', '.join(sorted({*_PRESETS, *_ANTHROPIC_NATIVE_PRESETS}))}, custom."
            )
        base_url = settings.llm_base_url or preset

    if not settings.llm_model:
        raise LLMConfigError(
            f"LLM_MODEL is required when LLM_PROVIDER='{provider}' "
            "(e.g. LLM_MODEL=mistral-large-latest)."
        )
    if provider not in _KEY_OPTIONAL and not settings.llm_api_key:
        raise LLMConfigError(f"LLM_API_KEY is required for provider '{provider}'.")

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
