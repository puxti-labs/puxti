from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    anthropic_api_key: str = ""
    github_token: str = ""

    # LLM provider (BYOK). "anthropic" is the default and needs no extra config.
    # Any preset from puxti.llm._PRESETS (openai, mistral, glm, deepseek, groq,
    # openrouter, gemini, bedrock, ollama) or "custom" (requires LLM_BASE_URL)
    # speaks the OpenAI-compatible wire format.
    llm_provider: str = "anthropic"
    # Required for non-anthropic providers; for anthropic it overrides the
    # built-in default model.
    llm_model: str = ""
    # Falls back to ANTHROPIC_API_KEY when the provider is anthropic.
    llm_api_key: str = ""
    # Overrides the preset base URL (e.g. a non-default Bedrock region).
    llm_base_url: str = ""
    # Pricing overrides (USD per million tokens) for models puxti doesn't know.
    # Without them, dry-run shows token counts but never a fabricated dollar cost.
    llm_input_cost_per_mtok: float | None = None
    llm_output_cost_per_mtok: float | None = None

    # Empty default so "not configured" checks can trigger a helpful error
    # instead of a confusing "manifest not found at ./dbt/..." message.
    dbt_project_dir: str = ""
    dbt_profiles_dir: str = "~/.dbt"

    # Max concurrent LLM calls during `puxti scan` (definitions, edge batches,
    # dry-run token counting). 4 stays well inside entry-tier Anthropic rate
    # limits; raise it only if your tier allows.
    llm_concurrency: int = 4


settings = Settings()
