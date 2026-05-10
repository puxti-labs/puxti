from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    anthropic_api_key: str = ""
    github_token: str = ""

    dbt_project_dir: str = "./dbt"
    dbt_profiles_dir: str = "~/.dbt"


settings = Settings()
