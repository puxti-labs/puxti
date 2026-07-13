from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    anthropic_api_key: str = ""
    github_token: str = ""

    # Empty default so "not configured" checks can trigger a helpful error
    # instead of a confusing "manifest not found at ./dbt/..." message.
    dbt_project_dir: str = ""
    dbt_profiles_dir: str = "~/.dbt"

    # Max concurrent LLM calls during `puxti scan` (definitions, edge batches,
    # dry-run token counting). 4 stays well inside entry-tier Anthropic rate
    # limits; raise it only if your tier allows.
    llm_concurrency: int = 4


settings = Settings()
