import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import TestCase

from inventory_bot.models import (
    InventoryAvailability,
    Item,
    PendingReservation,
    PreparedReservation,
    Reservation,
    ReservationGroup,
    ScheduledReservation,
)
from inventory_bot.slack_app import (
    _slack_user_name,
    committed_message,
    confirmation_message,
    help_text,
    status_text,
)


class SlackMessageTests(TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(timezone=UTC)
        self.end = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)

    def test_confirmation_uses_unique_item_fields_without_quantity(self) -> None:
        item = Item("kayak1", "A")
        pending = PendingReservation(
            item_names=("kayak1",),
            start_at_utc=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
            end_at_utc=self.end,
            requester_user_id="U123",
        )

        text, blocks = confirmation_message(
            PreparedReservation(items=(item,), pending=pending),
            self.settings,
        )

        self.assertIn("kayak1", text)
        self.assertNotIn("Quantity", str(blocks))
        self.assertEqual("confirm_reservation", blocks[1]["elements"][0]["action_id"])

    def test_pending_reservation_keeps_old_button_payload_compatibility(self) -> None:
        old_value = json.dumps(
            {
                "v": 2,
                "n": "kayak1",
                "e": self.end.isoformat(),
                "u": "U123",
                "s": "Ev1",
                "c": "C1",
                "m": "123.456",
            }
        )

        pending = PendingReservation.from_action_value(old_value)

        self.assertEqual("kayak1", pending.item_name)
        self.assertEqual(self.end, pending.end_at_utc)
        self.assertEqual("U123", pending.requester_user_id)

    def test_new_button_payload_contains_only_required_fields(self) -> None:
        pending = PendingReservation(
            ("kayak1", "kayak2"),
            self.end,
            "U123",
            datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
        )

        payload = json.loads(pending.to_action_value())

        self.assertEqual({"v", "n", "b", "e", "u"}, set(payload))
        self.assertEqual(5, payload["v"])
        self.assertEqual(["kayak1", "kayak2"], payload["n"])

    def test_committed_message_contains_item_and_end_time(self) -> None:
        reservation = Reservation(
            reservation_id="R1",
            item_name="kayak1",
            location="A",
            start_at_utc=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
            end_at_utc=self.end,
        )

        text, blocks = committed_message(
            ReservationGroup("G1", (reservation,)), self.settings
        )

        self.assertIn("kayak1", text)
        self.assertIn("kayak1", str(blocks))
        self.assertIn("Starts", str(blocks))

    def test_multi_item_confirmation_lists_every_item(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
            end_at_utc=self.end,
            requester_user_id="U123",
        )

        text, blocks = confirmation_message(
            PreparedReservation(
                items=(Item("kayak1", "A"), Item("kayak2", "B")),
                pending=pending,
            ),
            self.settings,
        )

        self.assertIn("kayak1, kayak2", text)
        self.assertIn("Items (2)", str(blocks))
        self.assertIn("kayak1", str(blocks))
        self.assertIn("kayak2", str(blocks))

    def test_status_shows_current_owner_and_end_time(self) -> None:
        item = Item("kayak1", "A", self.end, "U123")

        text = status_text(
            [InventoryAvailability(item=item, available=False)], self.settings
        )

        self.assertIn("reserved until", text)
        self.assertIn("<@U123>", text)

    def test_help_includes_cancel_command(self) -> None:
        self.assertIn("cancel <item>", help_text())
        self.assertIn("from <date/time>", help_text())

    def test_status_shows_next_scheduled_reservation(self) -> None:
        next_reservation = ScheduledReservation(
            "R1",
            "kayak1",
            datetime(2026, 7, 16, 20, 0, tzinfo=UTC),
            datetime(2026, 7, 16, 22, 0, tzinfo=UTC),
            "Taylor Smith",
            "U123",
        )

        text = status_text(
            [
                InventoryAvailability(
                    item=Item("kayak1", "A"),
                    available=True,
                    next_reservation=next_reservation,
                )
            ],
            self.settings,
        )

        self.assertIn("available now; next reserved", text)
        self.assertIn("Taylor Smith", text)

    def test_slack_user_name_prefers_display_name(self) -> None:
        class Client:
            def users_info(self, *, user):
                self.requested_user = user
                return {
                    "user": {
                        "name": "taylor",
                        "profile": {
                            "display_name": "Taylor S.",
                            "real_name": "Taylor Smith",
                        },
                    }
                }

        client = Client()

        self.assertEqual("Taylor S.", _slack_user_name(client, "U123"))
        self.assertEqual("U123", client.requested_user)

    def test_slack_user_name_falls_back_to_real_name_then_id(self) -> None:
        class RealNameClient:
            def users_info(self, *, user):
                return {"user": {"profile": {"real_name": "Taylor Smith"}}}

        class FailingClient:
            def users_info(self, *, user):
                raise RuntimeError("lookup failed")

        self.assertEqual(
            "Taylor Smith", _slack_user_name(RealNameClient(), "U123")
        )
        with self.assertLogs("inventory_bot.slack_app", level="ERROR"):
            fallback = _slack_user_name(FailingClient(), "U123")
        self.assertEqual("U123", fallback)
