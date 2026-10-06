from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    database_url: str = "sqlite+aiosqlite:///./uni_bot.db"
    encryption_key: str = "change-me"
    eclass_base_url: str = ""
    edupage_timetable_url: str = ""
    edupage_cookie: str | None = None
    default_timezone: str = "Asia/Tashkent"


@lru_cache
def get_settings() -> Settings:
    return Settings()
