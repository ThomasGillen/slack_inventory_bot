from datetime import UTC, datetime
from unittest import TestCase

from inventory_bot.config import Settings
from inventory_bot.errors import SheetSchemaError
from inventory_bot.models import ScheduledReservation
from inventory_bot.sheets import (
    ITEM_HEADERS,
    LEGACY_ITEM_HEADERS,
    PRE_GROUP_RESERVATION_HEADERS,
    RESERVATION_HEADERS,
    SCREENSHOT_ITEM_HEADERS,
    GoogleSheetsRepository,
    migrate_item_rows,
    migrate_reservation_rows,
)


class RowBackedSheetsRepository(GoogleSheetsRepository):
    def __init__(self, *, item_rows=None, reservation_rows=None) -> None:
        self.settings = Settings(spreadsheet_id="sheet-id")
        self.rows = {
            "Items": item_rows or [ITEM_HEADERS],
            "Reservations": reservation_rows or [RESERVATION_HEADERS],
        }

    def _get_values(self, sheet_title, **kwargs):
        return self.rows[sheet_title]


class ExecutableRequest:
    def __init__(self, result=None) -> None:
        self.result = result or {}

    def execute(self):
        return self.result


class RecordingValuesApi:
    def __init__(self) -> None:
        self.update_calls = []
        self.append_calls = []
        self.clear_calls = []
        self.batch_update_calls = []
        self.batch_clear_calls = []

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        return ExecutableRequest()

    def clear(self, **kwargs):
        self.clear_calls.append(kwargs)
        return ExecutableRequest()

    def append(self, **kwargs):
        self.append_calls.append(kwargs)
        return ExecutableRequest()

    def batchUpdate(self, **kwargs):
        self.batch_update_calls.append(kwargs)
        return ExecutableRequest()

    def batchClear(self, **kwargs):
        self.batch_clear_calls.append(kwargs)
        return ExecutableRequest()


class RecordingSpreadsheetsApi:
    def __init__(self) -> None:
        self.values_api = RecordingValuesApi()

    def values(self):
        return self.values_api


class RecordingService:
    def __init__(self) -> None:
        self.spreadsheets_api = RecordingSpreadsheetsApi()

    def spreadsheets(self):
        return self.spreadsheets_api


class GoogleSheetsRepositoryTests(TestCase):
    def test_parses_blank_and_active_reservation_state(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[
                ITEM_HEADERS,
                ["kayak1", "A", "", ""],
                ["kayak2", "B", "2026-07-15T22:00:00Z", "U123"],
            ]
        )

        items = repository.list_items()

        self.assertIsNone(items[0].reservation_end_utc)
        self.assertEqual(datetime(2026, 7, 15, 22, 0, tzinfo=UTC), items[1].reservation_end_utc)
        self.assertEqual("U123", items[1].reserved_by)

    def test_dash_is_accepted_as_no_reservation(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[ITEM_HEADERS, ["kayak1", "A", "-", ""]]
        )

        self.assertIsNone(repository.list_items()[0].reservation_end_utc)

    def test_parses_readable_local_reservation_time(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[
                ITEM_HEADERS,
                [
                    "kayak1",
                    "A",
                    "2026-07-15 03:00 PM PDT (UTC-07:00)",
                    "Taylor Smith",
                ],
            ]
        )

        item = repository.list_items()[0]

        self.assertEqual(
            datetime(2026, 7, 15, 22, 0, tzinfo=UTC),
            item.reservation_end_utc,
        )

    def test_rejects_duplicate_item_names_case_insensitively(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[
                ITEM_HEADERS,
                ["kayak1", "A", "", ""],
                ["KAYAK1", "B", "", ""],
            ]
        )

        with self.assertRaisesRegex(SheetSchemaError, "must be unique"):
            repository.list_items()

    def test_rejects_reservation_end_without_timezone(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[
                ITEM_HEADERS,
                ["kayak1", "A", "2026-07-15T22:00:00", "U123"],
            ]
        )

        with self.assertRaisesRegex(SheetSchemaError, "include a timezone"):
            repository.list_items()

    def test_rejects_item_name_too_long_for_slack_picker(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[ITEM_HEADERS, ["x" * 76, "A", "", ""]]
        )

        with self.assertRaisesRegex(SheetSchemaError, "75 characters"):
            repository.list_items()

    def test_updates_only_current_reservation_cells(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[ITEM_HEADERS, ["kayak1", "A", "", ""]]
        )
        repository.service = RecordingService()

        repository.update_item_reservations(
            [
                ScheduledReservation(
                    "R1",
                    "KAYAK1",
                    datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
                    datetime(2026, 7, 15, 22, 0, tzinfo=UTC),
                    "U123",
                    "U123",
                    "G1",
                )
            ]
        )

        call = repository.service.spreadsheets_api.values_api.batch_update_calls[0]
        self.assertEqual(
            {
                "range": "'Items'!C2:D2",
                "values": [["2026-07-15 03:00 PM PDT (UTC-07:00)", "U123"]],
            },
            call["body"]["data"][0],
        )

    def test_cancellation_clears_only_current_reservation_cells(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[
                ITEM_HEADERS,
                ["kayak1", "A", "2026-07-15T22:00:00Z", "U123"],
            ]
        )
        repository.service = RecordingService()

        repository.clear_item_reservations(item_names=["kayak1"])

        call = repository.service.spreadsheets_api.values_api.batch_clear_calls[0]
        self.assertEqual(["'Items'!C2:D2"], call["body"]["ranges"])

    def test_parses_scheduled_reservation_rows(self) -> None:
        repository = RowBackedSheetsRepository(
            reservation_rows=[
                RESERVATION_HEADERS,
                [
                    "R1",
                    "G1",
                    "kayak1",
                    "2026-07-15 01:00 PM PDT (UTC-07:00)",
                    "2026-07-15 03:00 PM PDT (UTC-07:00)",
                    "Taylor Smith",
                    "U123",
                ],
            ]
        )

        reservation = repository.list_reservations()[0]

        self.assertEqual("R1", reservation.reservation_id)
        self.assertEqual("G1", reservation.group_id)
        self.assertEqual(
            datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
            reservation.start_at_utc,
        )
        self.assertEqual(
            datetime(2026, 7, 15, 22, 0, tzinfo=UTC),
            reservation.end_at_utc,
        )

    def test_appends_scheduled_reservation(self) -> None:
        repository = RowBackedSheetsRepository()
        repository.service = RecordingService()

        repository.add_reservations(
            [
                ScheduledReservation(
                    "R1",
                    "kayak1",
                    datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
                    datetime(2026, 7, 15, 22, 0, tzinfo=UTC),
                    "Taylor Smith",
                    "U123",
                    "G1",
                )
            ]
        )

        call = repository.service.spreadsheets_api.values_api.append_calls[0]
        self.assertEqual("'Reservations'!A:G", call["range"])
        self.assertEqual(
            [[
                "R1",
                "G1",
                "kayak1",
                "2026-07-15 01:00 PM PDT (UTC-07:00)",
                "2026-07-15 03:00 PM PDT (UTC-07:00)",
                "Taylor Smith",
                "U123",
            ]],
            call["body"]["values"],
        )

    def test_deleting_schedule_clears_only_matching_row(self) -> None:
        repository = RowBackedSheetsRepository(
            reservation_rows=[
                RESERVATION_HEADERS,
                [
                    "R1",
                    "G1",
                    "kayak1",
                    "2026-07-15T20:00:00Z",
                    "2026-07-15T22:00:00Z",
                    "Taylor",
                    "U1",
                ],
                [
                    "R2",
                    "G2",
                    "kayak2",
                    "2026-07-16T20:00:00Z",
                    "2026-07-16T22:00:00Z",
                    "Morgan",
                    "U2",
                ],
            ]
        )
        repository.service = RecordingService()

        repository.delete_reservations(reservation_ids=["R2"])

        call = repository.service.spreadsheets_api.values_api.batch_clear_calls[0]
        self.assertEqual(["'Reservations'!A3:G3"], call["body"]["ranges"])

    def test_seeds_existing_active_item_into_empty_schedule(self) -> None:
        repository = RowBackedSheetsRepository(
            item_rows=[
                ITEM_HEADERS,
                ["kayak1", "A", "2099-07-15T22:00:00Z", "Taylor Smith"],
            ]
        )
        repository.service = RecordingService()

        repository._seed_active_item_reservations()

        call = repository.service.spreadsheets_api.values_api.append_calls[0]
        values = call["body"]["values"][0]
        self.assertTrue(values[0].startswith("migrated-"))
        self.assertEqual(values[0], values[1])
        self.assertEqual("kayak1", values[2])
        self.assertEqual("Taylor Smith", values[5])

    def test_migrates_original_six_column_layout(self) -> None:
        migrated = migrate_item_rows(
            [
                LEGACY_ITEM_HEADERS,
                ["K1", "kayak1", "A", "1", "", "TRUE"],
                ["K2", "kayak2", "B", "1", "", "TRUE"],
            ]
        )

        self.assertEqual(ITEM_HEADERS, migrated[0])
        self.assertEqual(["kayak1", "A", "", ""], migrated[1])
        self.assertEqual(["kayak2", "B", "", ""], migrated[2])

    def test_migrates_screenshot_layout_and_preserves_end_time(self) -> None:
        migrated = migrate_item_rows(
            [
                SCREENSHOT_ITEM_HEADERS,
                ["kayak1", "A", "FALSE", "-"],
                ["kayak2", "B", "TRUE", "2026-07-15T22:00:00Z"],
            ]
        )

        self.assertEqual(["kayak1", "A", "", ""], migrated[1])
        self.assertEqual(
            ["kayak2", "B", "2026-07-15T22:00:00Z", ""], migrated[2]
        )

    def test_migrates_single_item_schedule_and_preserves_rows(self) -> None:
        migrated = migrate_reservation_rows(
            [
                PRE_GROUP_RESERVATION_HEADERS,
                [
                    "R1",
                    "kayak1",
                    "2026-07-15T20:00:00Z",
                    "2026-07-15T22:00:00Z",
                    "Taylor Smith",
                    "U123",
                ],
            ]
        )

        self.assertEqual(RESERVATION_HEADERS, migrated[0])
        self.assertEqual(
            [
                "R1",
                "R1",
                "kayak1",
                "2026-07-15T20:00:00Z",
                "2026-07-15T22:00:00Z",
                "Taylor Smith",
                "U123",
            ],
            migrated[1],
        )
