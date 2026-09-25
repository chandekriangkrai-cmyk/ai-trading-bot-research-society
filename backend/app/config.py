from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_version() -> str:
    """Read the application version from the repository's single VERSION file."""
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        value = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        value = "V42.4"
    return value or "V42.4"


class Settings(BaseSettings):
    app_name: str = "AI Trading Bot Research Society API"
    app_version: str = _read_version()
    environment: str = "development"

    database_url: str = "sqlite:///./research_society.db"

    cors_origins: str = "*"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    loaded = Settings()
    # V42.4: VERSION is authoritative. An APP_VERSION environment variable
    # cannot silently make the API report a different release than the source.
    loaded.app_version = _read_version()
    return loaded


settings = get_settings()
