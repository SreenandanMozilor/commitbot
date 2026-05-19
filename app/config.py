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


@lru_cache
def get_settings() -> Settings:
    return Settings()
