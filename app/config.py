"""Application configuration. Reads from environment / .env file."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Slack
    slack_bot_token: str = "xoxb-dev-placeholder"
    slack_signing_secret: str = "dev-placeholder"
    slack_client_id: str = ""
    slack_client_secret: str = ""

    # App
    database_url: str = "sqlite:///./commitbot.db"
    app_base_url: str = "http://localhost:8000"
    session_secret: str = "dev-secret-change-me"
    # Sign-in-with-Slack OAuth redirect path. Joined with app_base_url to form
    # the full redirect_uri registered in the Slack app config.
    slack_oauth_redirect_path: str = "/auth/slack/callback"

    # Dev flags
    dry_run_pings: bool = True
    log_level: str = "INFO"
    # Cookies marked Secure won't be sent over plain http://. Default off so
    # `localhost` works; flip on when serving over ngrok / production HTTPS.
    secure_cookies: bool = False

    # --- Agentic commitment capture ---
    # See .env.example for the contract. Sensible defaults so unconfigured
    # installs fall back to the deterministic stub classifier and never
    # write to a real LLM API by accident.
    agent_provider: str = "anthropic"
    anthropic_api_key: str = ""
    agent_model: str = "claude-haiku-4-5-20251001"
    agent_dry_run: bool = False
    agent_scan_interval_minutes: int = 30
    agent_confidence_floor: float = 0.75
    agent_undo_window_minutes: int = 60
    agent_buffer_retention_days: int = 7


@lru_cache
def get_settings() -> Settings:
    return Settings()
