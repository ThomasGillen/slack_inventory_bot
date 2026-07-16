"""Google Sheets implementation of the inventory repository."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ConfigurationError, ItemNotFoundError, SheetSchemaError
from .models import Item, ScheduledReservation

ITEM_HEADERS = ["item_name", "location", "reservation_end", "reserved_by"]
PRE_GROUP_RESERVATION_HEADERS = [
    "reservation_id",
    "item_name",
    "start_time",
    "end_time",
    "reserved_by",
    "slack_user_id",
]
RESERVATION_HEADERS = [
    "reservation_id",
    "group_id",
    "item_name",
    "start_time",
    "end_time",
    "reserved_by",
    "slack_user_id",
]
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


def _normalized_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _parse_optional_datetime(
    value: str,
    *,
    row_number: int,
    display_timezone: tzinfo,
    sheet_title: str = "Items",
    field_name: str = "reservation_end",
) -> datetime | None:
    if not value or value == "-":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        match = READABLE_DATETIME_RE.fullmatch(value)
        if not match:
            raise SheetSchemaError(
                f"{sheet_title} row {row_number} has an invalid {field_name}: {value!r}. "
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
                f"{sheet_title} row {row_number} has an invalid {field_name}: {value!r}."
            ) from readable_exc
    if parsed.tzinfo is None:
        raise SheetSchemaError(
            f"{sheet_title} row {row_number} {field_name} must include a timezone."
        )
    return parsed.astimezone(UTC)


def _format_sheet_datetime(value: datetime, *, display_timezone: tzinfo) -> str:
    local_value = value.astimezone(display_timezone)
    numeric_offset = local_value.strftime("%z") or "+0000"
    readable_offset = f"{numeric_offset[:3]}:{numeric_offset[3:]}"
    timezone_label = local_value.tzname() or "UTC"
    return (
        f"{local_value.strftime('%Y-%m-%d %I:%M %p')} "
        f"{timezone_label} (UTC{readable_offset})"
    )


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


def migrate_reservation_rows(rows: list[list[Any]]) -> list[list[str]]:
    """Add group IDs while preserving rows from the single-item schedule."""
    if not rows:
        return [RESERVATION_HEADERS]
    headers = [str(value).strip() for value in rows[0]]
    if headers == RESERVATION_HEADERS:
        return [[str(value) for value in row] for row in rows]
    if headers != PRE_GROUP_RESERVATION_HEADERS:
        return [RESERVATION_HEADERS]

    migrated: list[list[str]] = [RESERVATION_HEADERS]
    for row in rows[1:]:
        padded = [str(value).strip() for value in row] + [""] * (
            len(PRE_GROUP_RESERVATION_HEADERS) - len(row)
        )
        record = dict(
            zip(
                PRE_GROUP_RESERVATION_HEADERS,
                padded[: len(PRE_GROUP_RESERVATION_HEADERS)],
                strict=True,
            )
        )
        if not any(record.values()):
            continue
        migrated.append(
            [
                record["reservation_id"],
                record["reservation_id"],
                record["item_name"],
                record["start_time"],
                record["end_time"],
                record["reserved_by"],
                record["slack_user_id"],
            ]
        )
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
        for sheet_title in (
            self.settings.items_sheet,
            self.settings.reservations_sheet,
        ):
            if sheet_title not in existing:
                (
                    self.service.spreadsheets()
                    .batchUpdate(
                        spreadsheetId=self.settings.spreadsheet_id,
                        body={
                            "requests": [
                                {
                                    "addSheet": {
                                        "properties": {"title": sheet_title}
                                    }
                                }
                            ]
                        },
                    )
                    .execute()
                )
        self._ensure_headers(
            self.settings.items_sheet,
            ITEM_HEADERS,
            migration_command="inventory-sheet-init --migrate-items",
        )
        self._ensure_headers(
            self.settings.reservations_sheet,
            RESERVATION_HEADERS,
            migration_command="inventory-sheet-init --migrate-reservations",
        )
        self._seed_active_item_reservations()

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

    def migrate_reservations_schema(self) -> str | None:
        """Back up an older Reservations tab and initialize the schedule schema."""
        spreadsheet = self._spreadsheet_metadata()
        properties = [
            sheet["properties"]
            for sheet in spreadsheet.get("sheets", [])
            if sheet["properties"]["title"] == self.settings.reservations_sheet
        ]
        if not properties:
            self.ensure_schema()
            return None

        rows = self._get_values(self.settings.reservations_sheet)
        current_headers = [str(value).strip() for value in rows[0]] if rows else []
        if current_headers == RESERVATION_HEADERS:
            return None
        migrated_rows = migrate_reservation_rows(rows)

        existing_titles = {
            sheet["properties"]["title"] for sheet in spreadsheet.get("sheets", [])
        }
        base_name = (
            f"{self.settings.reservations_sheet} Backup "
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
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
                range=f"{_a1_sheet_name(self.settings.reservations_sheet)}!A:Z",
                body={},
            )
            .execute()
        )
        (
            self.service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self.settings.spreadsheet_id,
                range=f"{_a1_sheet_name(self.settings.reservations_sheet)}!A1",
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

    def _ensure_headers(
        self,
        sheet_title: str,
        expected: list[str],
        *,
        migration_command: str,
    ) -> None:
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
                f"Run: {migration_command}"
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

    def list_reservations(self) -> list[ScheduledReservation]:
        rows = self._get_values(self.settings.reservations_sheet)
        records = self._records(
            rows, RESERVATION_HEADERS, self.settings.reservations_sheet
        )
        reservations: list[ScheduledReservation] = []
        for row_number, record in records:
            missing = [
                field
                for field in (
                    "reservation_id",
                    "group_id",
                    "item_name",
                    "start_time",
                    "end_time",
                )
                if not record[field]
            ]
            if missing:
                raise SheetSchemaError(
                    f"{self.settings.reservations_sheet} row {row_number} requires: "
                    f"{', '.join(missing)}."
                )
            start_at = _parse_optional_datetime(
                record["start_time"],
                row_number=row_number,
                display_timezone=self.settings.timezone,
                sheet_title=self.settings.reservations_sheet,
                field_name="start_time",
            )
            end_at = _parse_optional_datetime(
                record["end_time"],
                row_number=row_number,
                display_timezone=self.settings.timezone,
                sheet_title=self.settings.reservations_sheet,
                field_name="end_time",
            )
            if start_at is None or end_at is None or end_at <= start_at:
                raise SheetSchemaError(
                    f"{self.settings.reservations_sheet} row {row_number} must end "
                    "after it starts."
                )
            reservations.append(
                ScheduledReservation(
                    reservation_id=record["reservation_id"],
                    item_name=record["item_name"],
                    start_at_utc=start_at,
                    end_at_utc=end_at,
                    reserved_by=record["reserved_by"],
                    slack_user_id=record["slack_user_id"],
                    group_id=record["group_id"],
                )
            )

        ids = [reservation.reservation_id for reservation in reservations]
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        if duplicates:
            raise SheetSchemaError(
                f"{self.settings.reservations_sheet} reservation_id values must be "
                f"unique. Duplicates: {', '.join(duplicates)}."
            )
        return reservations

    def add_reservations(
        self, reservations: list[ScheduledReservation]
    ) -> None:
        if not reservations:
            return
        values = [
            [
                reservation.reservation_id,
                reservation.group_id or reservation.reservation_id,
                reservation.item_name,
                _format_sheet_datetime(
                    reservation.start_at_utc,
                    display_timezone=self.settings.timezone,
                ),
                _format_sheet_datetime(
                    reservation.end_at_utc,
                    display_timezone=self.settings.timezone,
                ),
                reservation.reserved_by,
                reservation.slack_user_id,
            ]
            for reservation in reservations
        ]
        (
            self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.settings.spreadsheet_id,
                range=f"{_a1_sheet_name(self.settings.reservations_sheet)}!A:G",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )

    def delete_reservations(self, *, reservation_ids: list[str]) -> None:
        if not reservation_ids:
            return
        row_numbers = self._reservation_rows(reservation_ids)
        ranges = [
            (
                f"{_a1_sheet_name(self.settings.reservations_sheet)}!"
                f"A{row_numbers[reservation_id]}:G{row_numbers[reservation_id]}"
            )
            for reservation_id in reservation_ids
        ]
        (
            self.service.spreadsheets()
            .values()
            .batchClear(
                spreadsheetId=self.settings.spreadsheet_id,
                body={"ranges": ranges},
            )
            .execute()
        )

    def update_item_reservations(
        self, reservations: list[ScheduledReservation]
    ) -> None:
        if not reservations:
            return
        row_numbers = self._item_rows(
            [reservation.item_name for reservation in reservations]
        )
        data = [
            {
                "range": (
                    f"{_a1_sheet_name(self.settings.items_sheet)}!"
                    f"C{row_numbers[_normalized_name(reservation.item_name)]}:"
                    f"D{row_numbers[_normalized_name(reservation.item_name)]}"
                ),
                "values": [[
                    _format_sheet_datetime(
                        reservation.end_at_utc,
                        display_timezone=self.settings.timezone,
                    ),
                    reservation.reserved_by,
                ]],
            }
            for reservation in reservations
        ]
        (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.settings.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            )
            .execute()
        )

    def clear_item_reservations(self, *, item_names: list[str]) -> None:
        if not item_names:
            return
        row_numbers = self._item_rows(item_names)
        ranges = [
            (
                f"{_a1_sheet_name(self.settings.items_sheet)}!"
                f"C{row_numbers[_normalized_name(item_name)]}:"
                f"D{row_numbers[_normalized_name(item_name)]}"
            )
            for item_name in item_names
        ]
        (
            self.service.spreadsheets()
            .values()
            .batchClear(
                spreadsheetId=self.settings.spreadsheet_id,
                body={"ranges": ranges},
            )
            .execute()
        )

    def _item_rows(self, item_names: list[str]) -> dict[str, int]:
        rows = self._get_values(self.settings.items_sheet)
        records = self._records(rows, ITEM_HEADERS, self.settings.items_sheet)
        rows_by_name = {
            _normalized_name(record["item_name"]): row_number
            for row_number, record in records
        }
        requested = [_normalized_name(item_name) for item_name in item_names]
        missing = [
            item_name
            for item_name, normalized in zip(item_names, requested, strict=True)
            if normalized not in rows_by_name
        ]
        if missing:
            raise ItemNotFoundError(
                f"Missing Items rows for: {', '.join(missing)}."
            )
        return {normalized: rows_by_name[normalized] for normalized in requested}

    def _reservation_rows(self, reservation_ids: list[str]) -> dict[str, int]:
        rows = self._get_values(self.settings.reservations_sheet)
        records = self._records(
            rows, RESERVATION_HEADERS, self.settings.reservations_sheet
        )
        rows_by_id = {
            record["reservation_id"]: row_number
            for row_number, record in records
        }
        missing = [
            reservation_id
            for reservation_id in reservation_ids
            if reservation_id not in rows_by_id
        ]
        if missing:
            raise ItemNotFoundError(
                f"Missing Reservations rows for: {', '.join(missing)}."
            )
        return {
            reservation_id: rows_by_id[reservation_id]
            for reservation_id in reservation_ids
        }

    def _seed_active_item_reservations(self) -> None:
        if self.list_reservations():
            return
        now = datetime.now(tz=UTC)
        reservations: list[ScheduledReservation] = []
        for item in self.list_items():
            if item.reservation_end_utc is None or item.reservation_end_utc <= now:
                continue
            slack_user_id = (
                item.reserved_by
                if re.fullmatch(r"[UW][A-Z0-9]+", item.reserved_by)
                else ""
            )
            reservation_id = f"migrated-{uuid.uuid4()}"
            reservations.append(
                ScheduledReservation(
                    reservation_id=reservation_id,
                    item_name=item.item_name,
                    start_at_utc=now,
                    end_at_utc=item.reservation_end_utc,
                    reserved_by=item.reserved_by,
                    slack_user_id=slack_user_id,
                    group_id=reservation_id,
                )
            )
        self.add_reservations(reservations)

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
            migration_flag = (
                "--migrate-reservations"
                if "reservation_id" in expected_headers
                else "--migrate-items"
            )
            raise SheetSchemaError(
                f"{sheet_title} headers do not match the expected schema. "
                f"Run: inventory-sheet-init {migration_flag}"
            )

        records: list[tuple[int, dict[str, str]]] = []
        for row_number, row in enumerate(rows[1:], start=2):
            padded = list(row) + [""] * (len(expected_headers) - len(row))
            values = [str(value).strip() for value in padded[: len(expected_headers)]]
            if not any(values):
                continue
            records.append((row_number, dict(zip(expected_headers, values, strict=True))))
        return records
