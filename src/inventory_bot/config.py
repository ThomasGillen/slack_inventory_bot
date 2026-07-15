"""Environment-backed configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class Settings:
    spreadsheet_id: str
    timezone_name: str = "America/Los_Angeles"
    slack_bot_token: str = ""
    slack_app_token: str = ""
    service_account_file: str | None = None
    service_account_json: str | None = None
    items_sheet: str = "Items"
    allowed_channel_ids: frozenset[str] = frozenset()
    allowed_user_ids: frozenset[str] = frozenset()

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
            allowed_channel_ids=_csv_set(os.getenv("ALLOWED_CHANNEL_IDS")),
            allowed_user_ids=_csv_set(os.getenv("ALLOWED_USER_IDS")),
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
