from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums import Mode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    mode: Mode = Mode.DRY_RUN
    database_url: str = "sqlite+aiosqlite:///data/manager.db"
    timezone: str = "Europe/Moscow"
    account_daily_limit_kopecks: int = Field(default=100000, gt=0)
    telegram_admin_ids: list[int] = []
    telegram_bot_token: SecretStr | None = None
    telegram_confirmation_minutes: int = Field(default=5, ge=1, le=60)
    avito_client_id: SecretStr | None = None
    avito_client_secret: SecretStr | None = None
    avito_official_spec_paths: list[str] = []
    avito_item_ids: list[int] = Field(default_factory=list, max_length=5)
    avito_expected_account_id: int | None = Field(default=None, gt=0)

    @field_validator("avito_item_ids")
    @classmethod
    def item_ids_valid(cls, values):
        if any(type(v) is not int or v <= 0 for v in values) or len(set(values)) != len(values):
            raise ValueError("Use 1–5 distinct positive item IDs")
        return values

    monitoring_interval_minutes: int = Field(default=15, ge=1)
    increase_confirmations: int = Field(default=2, ge=2)
    decrease_confirmations: int = Field(default=3, ge=3)
    max_changes_per_day: int = Field(default=4, ge=1)
    increase_step_percent: int = Field(default=10, ge=1, le=100)
    decrease_step_percent: int = Field(default=5, ge=1, le=100)
    increase_step_kopecks: int = Field(default=100, ge=1)
    decrease_step_kopecks: int = Field(default=100, ge=1)
    max_position_data_age_minutes: int = Field(default=30, ge=1)
    max_spending_data_age_minutes: int = Field(default=30, ge=1)
    budget_reserve_percent: int = Field(default=20, ge=0, lt=100)
    floor_review_days: int = Field(default=14, ge=1)
    floor_review_observations: int = Field(default=5, ge=3)
    floor_review_window_hours: int = Field(default=24, ge=1)
    statistics_interval_minutes: int = Field(default=360, ge=60)
    statistics_read_enabled: bool = False
    statistics_backfill_days: int = Field(default=2, ge=1, le=28)
    backup_retention: int = Field(default=7, ge=1, le=365)
    optimization_min_contacts: int = Field(default=10, ge=5)

    @field_validator("telegram_bot_token", "avito_client_id", "avito_client_secret", mode="before")
    @classmethod
    def empty_secret(cls, value):
        return None if value == "" else value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("mode")
    @classmethod
    def no_environment_auto(cls, value):
        if value != Mode.DRY_RUN:
            raise ValueError("Startup must be DRY_RUN; live modes need owner confirmation")
        return value
