from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import TestCase

from inventory_bot.models import Item
from inventory_bot.slack_views import (
    DATE_ACTION,
    DATE_BLOCK,
    ITEM_ACTION,
    ITEM_BLOCK,
    OPEN_RESERVATION_ACTION,
    RESERVATION_MODAL_CALLBACK,
    TIME_ACTION,
    TIME_BLOCK,
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

    def test_launcher_contains_modal_button(self) -> None:
        _, blocks = reservation_launcher_message()

        self.assertEqual(
            OPEN_RESERVATION_ACTION,
            blocks[1]["elements"][0]["action_id"],
        )

    def test_modal_uses_external_item_select_and_native_date_time_pickers(self) -> None:
        modal = build_reservation_modal(
            self.settings,
            destination=ModalDestination("C123", "123.456"),
            now=self.now,
        )

        self.assertEqual(RESERVATION_MODAL_CALLBACK, modal["callback_id"])
        self.assertEqual("external_select", modal["blocks"][0]["element"]["type"])
        self.assertEqual("datepicker", modal["blocks"][1]["element"]["type"])
        self.assertEqual("timepicker", modal["blocks"][2]["element"]["type"])
        self.assertEqual("2026-07-15", modal["blocks"][1]["element"]["initial_date"])

    def test_item_options_include_location_and_stable_value(self) -> None:
        options = item_options([Item("kayak1", "Dock A")])

        self.assertEqual("kayak1 — Dock A", options[0]["text"]["text"])
        self.assertEqual("kayak1", options[0]["value"])

    def test_parses_modal_selection_into_pending_reservation(self) -> None:
        destination = ModalDestination("C123", "123.456")
        view = {
            "private_metadata": destination.to_json(),
            "state": {
                "values": {
                    ITEM_BLOCK: {
                        ITEM_ACTION: {
                            "selected_option": {"value": "kayak1"}
                        }
                    },
                    DATE_BLOCK: {DATE_ACTION: {"selected_date": "2026-07-15"}},
                    TIME_BLOCK: {TIME_ACTION: {"selected_time": "17:00"}},
                }
            },
        }

        pending = parse_modal_submission(
            view,
            settings=self.settings,
            requester_user_id="U123",
            now=self.now,
        )

        self.assertEqual("kayak1", pending.item_name)
        self.assertEqual(datetime(2026, 7, 15, 17, 0, tzinfo=UTC), pending.end_at_utc)

    def test_past_modal_time_returns_time_field_error(self) -> None:
        view = {
            "state": {
                "values": {
                    ITEM_BLOCK: {
                        ITEM_ACTION: {
                            "selected_option": {"value": "kayak1"}
                        }
                    },
                    DATE_BLOCK: {DATE_ACTION: {"selected_date": "2026-07-14"}},
                    TIME_BLOCK: {TIME_ACTION: {"selected_time": "17:00"}},
                }
            }
        }

        with self.assertRaises(ModalInputError) as raised:
            parse_modal_submission(
                view,
                settings=self.settings,
                requester_user_id="U123",
                now=self.now,
            )

        self.assertEqual(TIME_BLOCK, raised.exception.block_id)
