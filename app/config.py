"""Application settings — a mutable singleton, per test contract (tests assign
attributes directly, e.g. settings.api_key = "test-key")."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_key: str = "dev-key"
    database_url: str = "sqlite+aiosqlite:///:memory:"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    morning_cycle_hour: int = 6
    evening_cycle_hour: int = 20
    stripe_webhook_secret: str = ""
    stripe_secret_key: str = ""
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = ""
    twitter_api_key: str = ""
    twitter_api_secret: str = ""
    twitter_access_token: str = ""
    twitter_access_token_secret: str = ""
    tavily_api_key: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    github_token: str = ""
    github_repo: str = ""
    sandbox_mode: bool = True
    llm_provider: str = "claude"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
