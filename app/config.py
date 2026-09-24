"""Central configuration. All secrets come from environment variables (.env)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_allowed_user_id: int | None = None

    # --- LLM ---
    llm_provider: Literal["openai", "mock"] = "openai"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek/deepseek-chat"
    llm_temperature: float = 0.8
    llm_max_retries: int = 2
    llm_timeout_seconds: float = 120.0

    # --- Threads ---
    threads_app_id: str = ""
    threads_app_secret: str = ""
    threads_redirect_uri: str = "http://localhost:8321/threads/callback"
    threads_access_token: str = ""
    threads_user_id: str = ""
    threads_api_version: str = "v21.0"

    # --- TikTok ---
    tiktok_api_enabled: bool = False
    tiktok_api_base_url: str = "https://open-api.tiktok.com"
    tiktok_api_access_token: str = ""

    # --- Trends ---
    search_api_provider: str = ""
    search_api_key: str = ""
    google_trends_enabled: bool = False
    trend_refresh_minutes: int = 20
    opportunity_scan_minutes: int = 10

    # --- Behaviour ---
    auto_publish: bool = False
    dry_run: bool = True
    tz: str = "Asia/Kuala_Lumpur"

    opportunity_threshold: int = 75
    smart_max_wait_hours: int = 24
    max_posts_per_day: int = 4
    min_hours_between_posts: float = 6.0
    quiet_hours: str = "23:00-08:00"
    max_similar_products_per_day: int = 2
    similarity_threshold: float = 0.62

    analytics_checkpoints: str = "1,6,24,72"

    affiliate_disclosure_enabled: bool = True
    affiliate_disclosure_text: str = "affiliate link"
    content_language: Literal["rojak", "malay", "english"] = "rojak"
    content_strategy: Literal["auto", "evergreen"] = "auto"

    notify_level: Literal["minimal", "normal", "verbose"] = "normal"

    # --- Admin API ---
    admin_host: str = "127.0.0.1"
    admin_port: int = 8321
    admin_token: str = ""

    # --- Data ---
    db_path: str = "data/engine.db"
    data_dir: str = "data"
    log_level: str = "INFO"

    @field_validator("analytics_checkpoints")
    @classmethod
    def _parse_checkpoints(cls, v: str) -> str:
        parts = [p.strip() for p in v.split(",") if p.strip()]
        for p in parts:
            float(p)
        return v

    @property
    def checkpoints_hours(self) -> list[float]:
        return sorted(float(p) for p in self.analytics_checkpoints.split(",") if p.strip())

    @property
    def quiet_hours_tuple(self) -> tuple[int, int, int, int]:
        try:
            start_s, end_s = self.quiet_hours.split("-")
            sh, sm = (int(x) for x in start_s.strip().split(":"))
            eh, em = (int(x) for x in end_s.strip().split(":"))
            return sh, sm, eh, em
        except Exception:
            return 23, 0, 8, 0

    def is_configured(self) -> dict[str, bool]:
        return {
            "telegram": bool(self.telegram_bot_token and self.telegram_allowed_user_id),
            "llm": self.llm_provider == "mock" or bool(self.llm_api_key),
            "threads": bool(self.threads_access_token and self.threads_user_id),
            "tiktok_api": self.tiktok_api_enabled and bool(self.tiktok_api_access_token),
            "admin": bool(self.admin_token),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
