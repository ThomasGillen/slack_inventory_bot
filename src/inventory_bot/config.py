"""Environment-backed configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError


def load_dotenv(path: str | Path = ".env") -> None:
    """Load a small, predictable subset of dotenv syntax without a dependency."""
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _csv_set(value: str | None) -> frozenset[str]:
    return frozenset(part.strip() for part in (value or "").split(",") if part.strip())


def _float_setting(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number.") from exc


def _int_setting(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a whole number.") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    spreadsheet_id: str
    timezone_name: str = "America/Los_Angeles"
    slack_bot_token: str = ""
    slack_app_token: str = ""
    service_account_file: str | None = None
    service_account_json: str | None = None
    items_sheet: str = "Items"
    reservations_sheet: str = "Reservations"
    allowed_channel_ids: frozenset[str] = frozenset()
    allowed_user_ids: frozenset[str] = frozenset()
    reservation_queue_database: str = ".inventory_bot/reservation_queue.sqlite3"
    reservation_queue_rate_per_minute: float = 4.0
    reservation_queue_max_attempts: int = 8
    reservation_queue_retention_days: int = 30
    item_picker_cache_seconds: float = 15.0

    @classmethod
    def from_env(cls, *, require_slack: bool = True) -> "Settings":
        load_dotenv()
        settings = cls(
            spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", "").strip(),
            timezone_name=os.getenv("INVENTORY_TIMEZONE", "America/Los_Angeles").strip(),
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN", "").strip(),
            slack_app_token=os.getenv("SLACK_APP_TOKEN", "").strip(),
            service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or None,
            service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
            items_sheet=os.getenv("ITEMS_SHEET", "Items").strip(),
            reservations_sheet=os.getenv(
                "RESERVATIONS_SHEET", "Reservations"
            ).strip(),
            allowed_channel_ids=_csv_set(os.getenv("ALLOWED_CHANNEL_IDS")),
            allowed_user_ids=_csv_set(os.getenv("ALLOWED_USER_IDS")),
            reservation_queue_database=os.getenv(
                "RESERVATION_QUEUE_DATABASE",
                ".inventory_bot/reservation_queue.sqlite3",
            ).strip(),
            reservation_queue_rate_per_minute=_float_setting(
                "RESERVATION_QUEUE_RATE_PER_MINUTE", 4.0
            ),
            reservation_queue_max_attempts=_int_setting(
                "RESERVATION_QUEUE_MAX_ATTEMPTS", 8
            ),
            reservation_queue_retention_days=_int_setting(
                "RESERVATION_QUEUE_RETENTION_DAYS", 30
            ),
            item_picker_cache_seconds=_float_setting(
                "ITEM_PICKER_CACHE_SECONDS", 15.0
            ),
        )
        settings.validate(require_slack=require_slack)
        return settings

    def validate(self, *, require_slack: bool = True) -> None:
        missing: list[str] = []
        if not self.spreadsheet_id:
            missing.append("GOOGLE_SPREADSHEET_ID")
        if require_slack and not self.slack_bot_token:
            missing.append("SLACK_BOT_TOKEN")
        if require_slack and not self.slack_app_token:
            missing.append("SLACK_APP_TOKEN")
        if missing:
            raise ConfigurationError(f"Missing required settings: {', '.join(missing)}")
        if not self.reservation_queue_database:
            raise ConfigurationError("RESERVATION_QUEUE_DATABASE cannot be empty.")
        if (
            not isfinite(self.reservation_queue_rate_per_minute)
            or self.reservation_queue_rate_per_minute <= 0
        ):
            raise ConfigurationError(
                "RESERVATION_QUEUE_RATE_PER_MINUTE must be greater than zero."
            )
        if self.reservation_queue_max_attempts < 1:
            raise ConfigurationError(
                "RESERVATION_QUEUE_MAX_ATTEMPTS must be at least one."
            )
        if self.reservation_queue_retention_days < 1:
            raise ConfigurationError(
                "RESERVATION_QUEUE_RETENTION_DAYS must be at least one."
            )
        if (
            not isfinite(self.item_picker_cache_seconds)
            or self.item_picker_cache_seconds < 0
        ):
            raise ConfigurationError(
                "ITEM_PICKER_CACHE_SECONDS cannot be negative."
            )
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(
                f"Unknown INVENTORY_TIMEZONE: {self.timezone_name}"
            ) from exc

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def user_allowed(self, user_id: str) -> bool:
        return not self.allowed_user_ids or user_id in self.allowed_user_ids

    def channel_allowed(self, channel_id: str) -> bool:
        return channel_id.startswith("D") or not self.allowed_channel_ids or channel_id in self.allowed_channel_ids
