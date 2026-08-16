import tempfile
from pathlib import Path
from unittest import TestCase

from inventory_bot.config import Settings
from inventory_bot.errors import ConfigurationError


class SettingsValidationTests(TestCase):
    def test_rejects_spreadsheet_placeholder(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "Replace GOOGLE_SPREADSHEET_ID"):
            Settings(spreadsheet_id="your-spreadsheet-id").validate(
                require_slack=False
            )

    def test_rejects_service_account_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigurationError, "points to a directory"):
                Settings(
                    spreadsheet_id="sheet-id",
                    service_account_file=directory,
                ).validate(require_slack=False)

    def test_rejects_missing_service_account_file(self) -> None:
        missing = Path(tempfile.gettempdir()) / "missing-inventory-service-account.json"
        with self.assertRaisesRegex(ConfigurationError, "does not exist"):
            Settings(
                spreadsheet_id="sheet-id",
                service_account_file=str(missing),
            ).validate(require_slack=False)

    def test_rejects_non_json_service_account_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "service-account.txt"
            key_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "service-account \\.json"):
                Settings(
                    spreadsheet_id="sheet-id",
                    service_account_file=str(key_path),
                ).validate(require_slack=False)

    def test_rejects_placeholder_slack_tokens(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "SLACK_BOT_TOKEN"):
            Settings(
                spreadsheet_id="sheet-id",
                slack_bot_token="xoxb-your-bot-token",
                slack_app_token="xapp-your-socket-mode-token",
            ).validate()

    def test_rejects_ellipsis_slack_tokens(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "SLACK_BOT_TOKEN"):
            Settings(
                spreadsheet_id="sheet-id",
                slack_bot_token="xoxb-...",
                slack_app_token="xapp-...",
            ).validate()
