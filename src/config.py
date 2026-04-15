"""Configuration loaded from environment variables."""

import uuid

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Famly credentials (email+password for auto-login)
    famly_email: str = ""
    famly_password: str = ""
    famly_child_id: str = ""

    # Alternative: provide a static access token (e.g. from Famly API dashboard)
    # If set, skips email/password login entirely
    famly_access_token: str = ""

    # Optional: override installation ID (usually stable)
    famly_installation_id: str = str(uuid.uuid4())

    # Backend base URL — override to point at a different Famly-backed portal
    # e.g. https://familyapp.brighthorizons.co.uk for Bright Horizons
    famly_base_url: str = "https://app.famly.co"

    # Paths
    photo_dir: str = "/photos"

    # Schedule (cron expression parts)
    fetch_interval_hours: int = 6

    # What to fetch
    fetch_tagged: bool = True
    fetch_journey: bool = True
    # Feed, notes, and messages are supported but have no UI yet
    fetch_feed: bool = False
    fetch_notes: bool = False
    fetch_messages: bool = False

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8811
    log_level: str = "INFO"
    admin_password: str = ""


settings = Settings()

DB_PATH = "/appdata/data/famly-photos.db"
