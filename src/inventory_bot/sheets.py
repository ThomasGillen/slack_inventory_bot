"""Google Sheets implementation of the inventory repository."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ConfigurationError, ItemNotFoundError, SheetSchemaError
from .models import Item

ITEM_HEADERS = ["item_name", "location", "reservation_end", "reserved_by"]
LEGACY_ITEM_HEADERS = [
    "item_id",
    "item_name",
    "location",
    "total_quantity",
    "aliases",
    "active",
]
SCREENSHOT_ITEM_HEADERS = ["item_name", "location", "active", "reservation_end"]
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
READABLE_DATETIME_RE = re.compile(
    r"^(?P<local>\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} [AP]M)"
    r"(?: (?P<zone>[^()]+?))?"
    r"(?: \(UTC(?P<offset>[+-]\d{2}:\d{2})\))?$",
    re.IGNORECASE,
)


def _a1_sheet_name(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _parse_optional_datetime(
    value: str,
    *,
    row_number: int,
    display_timezone: tzinfo,
) -> datetime | None:
    if not value or value == "-":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        match = READABLE_DATETIME_RE.fullmatch(value)
        if not match:
            raise SheetSchemaError(
                f"Items row {row_number} has an invalid reservation_end: {value!r}. "
                "Use the bot's local-time format, an ISO timestamp, or leave it blank."
            ) from exc
        local_text = match.group("local").upper()
        offset = match.group("offset")
        try:
            if offset:
                parsed = datetime.strptime(
                    f"{local_text}{offset}", "%Y-%m-%d %I:%M %p%z"
                )
            else:
                parsed = datetime.strptime(
                    local_text, "%Y-%m-%d %I:%M %p"
                ).replace(tzinfo=display_timezone)
        except ValueError as readable_exc:
            raise SheetSchemaError(
                f"Items row {row_number} has an invalid reservation_end: {value!r}."
            ) from readable_exc
    if parsed.tzinfo is None:
        raise SheetSchemaError(
            f"Items row {row_number} reservation_end must include a timezone."
        )
    return parsed.astimezone(UTC)


def migrate_item_rows(rows: list[list[Any]]) -> list[list[str]]:
    """Convert a supported older Items layout to the four-column current layout."""
    if not rows:
        return [ITEM_HEADERS]
    headers = [str(value).strip() for value in rows[0]]
    if headers == ITEM_HEADERS:
        return [[str(value) for value in row] for row in rows]
    if headers != LEGACY_ITEM_HEADERS and headers != SCREENSHOT_ITEM_HEADERS:
        raise SheetSchemaError(
            "Items cannot be migrated automatically. Expected either the original "
            "six-column schema or: item_name, location, active, reservation_end."
        )

    migrated: list[list[str]] = [ITEM_HEADERS]
    for row in rows[1:]:
        padded = [str(value).strip() for value in row] + [""] * (len(headers) - len(row))
        record = dict(zip(headers, padded[: len(headers)], strict=True))
        item_name = record.get("item_name", "")
        location = record.get("location", "")
        if not item_name and not location:
            continue
        reservation_end = (
            record.get("reservation_end", "")
            if headers == SCREENSHOT_ITEM_HEADERS
            else ""
        )
        if reservation_end == "-":
            reservation_end = ""
        migrated.append([item_name, location, reservation_end, ""])
    return migrated


class GoogleSheetsRepository:
    def __init__(self, settings: Settings, *, service: Any | None = None) -> None:
        self.settings = settings
        self.service = service or self._build_service()

    def _build_service(self) -> Any:
        try:
            import google.auth
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise ConfigurationError(
                "Google API packages are not installed. Run: python -m pip install -e ."
            ) from exc

        if self.settings.service_account_json:
            try:
                info = json.loads(self.settings.service_account_json)
            except json.JSONDecodeError as exc:
                raise ConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[SHEETS_SCOPE]
            )
        elif self.settings.service_account_file:
            key_path = Path(self.settings.service_account_file).expanduser()
            if not key_path.exists():
                raise ConfigurationError(
                    f"GOOGLE_SERVICE_ACCOUNT_FILE does not exist: {key_path}"
                )
            credentials = service_account.Credentials.from_service_account_file(
                str(key_path), scopes=[SHEETS_SCOPE]
            )
        else:
            credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])

        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def ensure_schema(self) -> None:
        spreadsheet = self._spreadsheet_metadata()
        existing = {
            sheet["properties"]["title"] for sheet in spreadsheet.get("sheets", [])
        }
        if self.settings.items_sheet not in existing:
            (
                self.service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.settings.spreadsheet_id,
                    body={
                        "requests": [
                            {
                                "addSheet": {
                                    "properties": {"title": self.settings.items_sheet}
                                }
                            }
                        ]
                    },
                )
                .execute()
            )
        self._ensure_headers(self.settings.items_sheet, ITEM_HEADERS)

    def migrate_items_schema(self) -> str | None:
        """Back up and convert a supported old Items tab. Return backup tab name."""
        spreadsheet = self._spreadsheet_metadata()
        properties = [
            sheet["properties"]
            for sheet in spreadsheet.get("sheets", [])
            if sheet["properties"]["title"] == self.settings.items_sheet
        ]
        if not properties:
            self.ensure_schema()
            return None

        rows = self._get_values(self.settings.items_sheet)
        current_headers = [str(value).strip() for value in rows[0]] if rows else []
        if current_headers == ITEM_HEADERS:
            return None

        migrated_rows = migrate_item_rows(rows)
        existing_titles = {
            sheet["properties"]["title"] for sheet in spreadsheet.get("sheets", [])
        }
        base_name = f"{self.settings.items_sheet} Backup {datetime.now().strftime('%Y%m%d-%H%M%S')}"
        backup_name = base_name
        counter = 2
        while backup_name in existing_titles:
            backup_name = f"{base_name} {counter}"
            counter += 1

        (
            self.service.spreadsheets()
            .batchUpdate(
                spreadsheetId=self.settings.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "duplicateSheet": {
                                "sourceSheetId": properties[0]["sheetId"],
                                "newSheetName": backup_name,
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        (
            self.service.spreadsheets()
            .values()
            .clear(
                spreadsheetId=self.settings.spreadsheet_id,
                range=f"{_a1_sheet_name(self.settings.items_sheet)}!A:Z",
                body={},
            )
            .execute()
        )
        (
            self.service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self.settings.spreadsheet_id,
                range=f"{_a1_sheet_name(self.settings.items_sheet)}!A1",
                valueInputOption="RAW",
                body={"values": migrated_rows},
            )
            .execute()
        )
        return backup_name

    def _spreadsheet_metadata(self) -> dict[str, Any]:
        return (
            self.service.spreadsheets()
            .get(
                spreadsheetId=self.settings.spreadsheet_id,
                fields="sheets.properties(sheetId,title)",
            )
            .execute()
        )

    def _ensure_headers(self, sheet_title: str, expected: list[str]) -> None:
        rows = self._get_values(sheet_title, end_column="Z", end_row=1)
        if not rows or not any(str(value).strip() for value in rows[0]):
            (
                self.service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self.settings.spreadsheet_id,
                    range=f"{_a1_sheet_name(sheet_title)}!A1",
                    valueInputOption="RAW",
                    body={"values": [expected]},
                )
                .execute()
            )
            return
        actual = [str(value).strip() for value in rows[0]]
        if actual != expected:
            raise SheetSchemaError(
                f"{sheet_title} headers must be exactly: {', '.join(expected)}. "
                "Run: inventory-sheet-init --migrate-items"
            )

    def list_items(self) -> list[Item]:
        rows = self._get_values(self.settings.items_sheet)
        records = self._records(rows, ITEM_HEADERS, self.settings.items_sheet)
        items: list[Item] = []
        for row_number, record in records:
            if not record["item_name"]:
                raise SheetSchemaError(f"Items row {row_number} requires item_name.")
            if len(record["item_name"]) > 75:
                raise SheetSchemaError(
                    f"Items row {row_number} item_name must be 75 characters or fewer "
                    "for Slack's item picker."
                )
            items.append(
                Item(
                    item_name=record["item_name"],
                    location=record["location"],
                    reservation_end_utc=_parse_optional_datetime(
                        record["reservation_end"],
                        row_number=row_number,
                        display_timezone=self.settings.timezone,
                    ),
                    reserved_by=record["reserved_by"],
                )
            )

        normalized_names = [item.item_name.casefold() for item in items]
        duplicates = sorted(
            {
                items[index].item_name
                for index, name in enumerate(normalized_names)
                if normalized_names.count(name) > 1
            }
        )
        if duplicates:
            raise SheetSchemaError(
                f"Items item_name values must be unique. Duplicates: {', '.join(duplicates)}."
            )
        return items

    def update_item_reservation(
        self,
        *,
        item_name: str,
        reservation_end_utc: datetime,
        reserved_by: str,
    ) -> None:
        local_end = reservation_end_utc.astimezone(self.settings.timezone)
        numeric_offset = local_end.strftime("%z") or "+0000"
        readable_offset = f"{numeric_offset[:3]}:{numeric_offset[3:]}"
        timezone_label = local_end.tzname() or self.settings.timezone_name
        timestamp = (
            f"{local_end.strftime('%Y-%m-%d %I:%M %p')} "
            f"{timezone_label} (UTC{readable_offset})"
        )
        row_number = self._item_row(item_name)
        (
            self.service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self.settings.spreadsheet_id,
                range=f"{_a1_sheet_name(self.settings.items_sheet)}!C{row_number}:D{row_number}",
                valueInputOption="RAW",
                body={"values": [[timestamp, reserved_by]]},
            )
            .execute()
        )

    def clear_item_reservation(self, *, item_name: str) -> None:
        row_number = self._item_row(item_name)
        (
            self.service.spreadsheets()
            .values()
            .clear(
                spreadsheetId=self.settings.spreadsheet_id,
                range=f"{_a1_sheet_name(self.settings.items_sheet)}!C{row_number}:D{row_number}",
                body={},
            )
            .execute()
        )

    def _item_row(self, item_name: str) -> int:
        rows = self._get_values(self.settings.items_sheet)
        records = self._records(rows, ITEM_HEADERS, self.settings.items_sheet)
        needle = " ".join(item_name.casefold().split())
        matches = [
            row_number
            for row_number, record in records
            if " ".join(record["item_name"].casefold().split()) == needle
        ]
        if len(matches) != 1:
            raise ItemNotFoundError(
                f"Expected one Items row for {item_name!r}; found {len(matches)}."
            )
        return matches[0]

    def _get_values(
        self,
        sheet_title: str,
        *,
        end_column: str = "Z",
        end_row: int | None = None,
    ) -> list[list[Any]]:
        cell_range = f"A1:{end_column}{end_row}" if end_row else f"A:{end_column}"
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.settings.spreadsheet_id,
                range=f"{_a1_sheet_name(sheet_title)}!{cell_range}",
            )
            .execute()
        )
        return response.get("values", [])

    @staticmethod
    def _records(
        rows: list[list[Any]],
        expected_headers: list[str],
        sheet_title: str,
    ) -> list[tuple[int, dict[str, str]]]:
        if not rows:
            raise SheetSchemaError(
                f"{sheet_title} has no header row. Run: inventory-sheet-init"
            )
        actual_headers = [str(value).strip() for value in rows[0]]
        if actual_headers != expected_headers:
            raise SheetSchemaError(
                f"{sheet_title} headers do not match the expected four-column schema. "
                "Run: inventory-sheet-init --migrate-items"
            )

        records: list[tuple[int, dict[str, str]]] = []
        for row_number, row in enumerate(rows[1:], start=2):
            padded = list(row) + [""] * (len(expected_headers) - len(row))
            values = [str(value).strip() for value in padded[: len(expected_headers)]]
            if not any(values):
                continue
            records.append((row_number, dict(zip(expected_headers, values, strict=True))))
        return records
