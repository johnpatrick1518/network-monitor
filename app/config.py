"""Environment-backed application configuration."""

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load settings from environment variables and the local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    demo_mode: bool = False
    demo_database_url: str = "sqlite:///./data/demo.db"
    subnet: str = ""
    scan_interval_seconds: int = Field(default=300, ge=30)
    ping_interval_seconds: int = Field(default=60, ge=5)
    ping_timeout_seconds: int = Field(default=2, ge=1, le=10)
    ping_workers: int = Field(default=32, ge=1, le=128)
    down_threshold: int = Field(default=3, ge=1, le=20)
    alert_cooldown_seconds: int = Field(default=3600, ge=0)
    history_retention_days: int = Field(default=30, ge=30)
    database_url: str = "sqlite:///./data/network-monitor.db"
    dashboard_username: str = "admin"
    dashboard_password: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_starttls: bool = True
    smtp_use_ssl: bool = False
    log_level: str = "INFO"

    @model_validator(mode="after")
    def isolate_demo_database(self) -> "Settings":
        """Never reuse the configured live inventory when enabling demo mode."""
        if self.demo_mode:
            if self.database_url == self.demo_database_url and ":memory:" not in self.database_url:
                raise ValueError("DEMO_DATABASE_URL must differ from the live DATABASE_URL.")
            self.database_url = self.demo_database_url
        return self

    @model_validator(mode="after")
    def validate_smtp(self) -> "Settings":
        """Disallow contradictory SMTP transport options."""
        if self.smtp_use_ssl and self.smtp_starttls:
            raise ValueError("Use either SMTP_USE_SSL or SMTP_STARTTLS, not both.")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide configuration."""
    return Settings()
