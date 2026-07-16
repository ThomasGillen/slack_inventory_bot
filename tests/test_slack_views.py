import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase

from inventory_bot.models import Item
from inventory_bot.slack_views import (
    END_ACTION,
    END_BLOCK,
    ITEM_ACTION,
    ITEM_BLOCK,
    OPEN_RESERVATION_ACTION,
    RESERVATION_MODAL_CALLBACK,
    START_ACTION,
    START_BLOCK,
    ModalDestination,
    ModalInputError,
    build_reservation_modal,
    item_options,
    parse_modal_submission,
    reservation_launcher_message,
)


class SlackViewTests(TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(timezone=UTC, timezone_name="UTC")
        self.now = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)

    def modal_view(
        self,
        *,
        start: datetime,
        end: datetime,
        default_start: datetime | None = None,
    ) -> dict:
        metadata = {}
        if default_start is not None:
            metadata["default_start"] = int(default_start.timestamp())
        return {
            "private_metadata": json.dumps(metadata),
            "state": {
                "values": {
                    ITEM_BLOCK: {
                        ITEM_ACTION: {
                            "selected_options": [
                                {"value": "kayak1"},
                                {"value": "kayak2"},
                            ]
                        }
                    },
                    START_BLOCK: {
                        START_ACTION: {
                            "selected_date_time": str(int(start.timestamp()))
                        }
                    },
                    END_BLOCK: {
                        END_ACTION: {
                            "selected_date_time": str(int(end.timestamp()))
                        }
                    },
                }
            },
        }

    def test_launcher_contains_modal_button(self) -> None:
        _, blocks = reservation_launcher_message()

        self.assertEqual(
            OPEN_RESERVATION_ACTION,
            blocks[1]["elements"][0]["action_id"],
        )

    def test_modal_uses_multi_item_select_and_combined_datetime_pickers(self) -> None:
        modal = build_reservation_modal(
            self.settings,
            destination=ModalDestination("C123", "123.456"),
            now=self.now,
        )

        self.assertEqual(RESERVATION_MODAL_CALLBACK, modal["callback_id"])
        self.assertEqual("multi_external_select", modal["blocks"][0]["element"]["type"])
        self.assertEqual(10, modal["blocks"][0]["element"]["max_selected_items"])
        self.assertEqual("datetimepicker", modal["blocks"][1]["element"]["type"])
        self.assertEqual("datetimepicker", modal["blocks"][2]["element"]["type"])
        self.assertEqual(
            int(self.now.timestamp()),
            modal["blocks"][1]["element"]["initial_date_time"],
        )
        self.assertEqual(
            int((self.now + timedelta(hours=1)).timestamp()),
            modal["blocks"][2]["element"]["initial_date_time"],
        )
        metadata = json.loads(modal["private_metadata"])
        self.assertEqual("C123", metadata["channel_id"])
        self.assertEqual(int(self.now.timestamp()), metadata["default_start"])

    def test_item_options_include_location_and_stable_value(self) -> None:
        options = item_options([Item("kayak1", "Dock A")])

        self.assertEqual("kayak1 — Dock A", options[0]["text"]["text"])
        self.assertEqual("kayak1", options[0]["value"])

    def test_parses_combined_datetime_selection(self) -> None:
        start = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
        end = datetime(2026, 7, 15, 17, 0, tzinfo=UTC)

        pending = parse_modal_submission(
            self.modal_view(start=start, end=end),
            settings=self.settings,
            requester_user_id="U123",
            now=self.now,
        )

        self.assertEqual(("kayak1", "kayak2"), pending.item_names)
        self.assertEqual(start, pending.start_at_utc)
        self.assertEqual(end, pending.end_at_utc)

    def test_unchanged_default_start_uses_exact_submission_time(self) -> None:
        submitted_at = self.now + timedelta(minutes=3, seconds=12)
        view = self.modal_view(
            start=self.now,
            end=self.now + timedelta(hours=1),
            default_start=self.now,
        )

        pending = parse_modal_submission(
            view,
            settings=self.settings,
            requester_user_id="U123",
            now=submitted_at,
        )

        self.assertEqual(submitted_at, pending.start_at_utc)

    def test_past_start_returns_start_field_error(self) -> None:
        view = self.modal_view(
            start=self.now - timedelta(hours=1),
            end=self.now + timedelta(hours=1),
        )

        with self.assertRaises(ModalInputError) as raised:
            parse_modal_submission(
                view,
                settings=self.settings,
                requester_user_id="U123",
                now=self.now,
            )

        self.assertEqual(START_BLOCK, raised.exception.block_id)

    def test_end_before_start_returns_end_field_error(self) -> None:
        view = self.modal_view(
            start=self.now + timedelta(hours=2),
            end=self.now + timedelta(hours=1),
        )

        with self.assertRaises(ModalInputError) as raised:
            parse_modal_submission(
                view,
                settings=self.settings,
                requester_user_id="U123",
                now=self.now,
            )

        self.assertEqual(END_BLOCK, raised.exception.block_id)
