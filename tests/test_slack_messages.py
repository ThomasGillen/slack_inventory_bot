from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import TestCase

from inventory_bot.models import (
    CancellationResult,
    InventoryAvailability,
    Item,
    PendingReservation,
    Reservation,
    ReservationGroup,
    ScheduledReservation,
)
from inventory_bot.slack_app import (
    _reply_to_event,
    _slack_user_name,
    cancellation_result_message,
    committed_message,
    help_text,
    queued_message,
    queued_submission_message,
    status_text,
)
from inventory_bot.reservation_queue import (
    EnqueueResult,
    QueueDestination,
    QueuedReservationRequest,
)
from inventory_bot.slack_views import MANAGE_RESERVATION_ACTION


class SlackMessageTests(TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            timezone=UTC,
            reservation_queue_rate_per_minute=4.0,
        )
        self.end = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)

    def test_consecutive_replies_use_each_current_event(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)

        client = Client()

        _reply_to_event(
            client,
            event={"channel": "C123", "ts": "100.001"},
            text="first response",
        )
        _reply_to_event(
            client,
            event={"channel": "C123", "ts": "100.002"},
            text="second response",
        )

        self.assertEqual(
            ["100.001", "100.002"],
            [message["thread_ts"] for message in client.messages],
        )
        self.assertEqual(
            ["first response", "second response"],
            [message["text"] for message in client.messages],
        )

    def test_direct_message_reply_does_not_reuse_a_thread(self) -> None:
        class Client:
            def chat_postMessage(self, **kwargs):
                self.message = kwargs

        client = Client()

        _reply_to_event(
            client,
            event={"channel": "D123", "ts": "100.001"},
            text="current response",
        )

        self.assertEqual("D123", client.message["channel"])
        self.assertNotIn("thread_ts", client.message)

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
        self.assertEqual(
            MANAGE_RESERVATION_ACTION,
            blocks[1]["elements"][0]["action_id"],
        )

    def test_queued_message_is_explicitly_not_a_confirmation(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
            end_at_utc=self.end,
            requester_user_id="U123",
        )
        request = QueuedReservationRequest(
            request_id="12345678-abcd",
            dedupe_key="event-1",
            pending=pending,
            reserved_by_name="Taylor Smith",
            destination=QueueDestination("D123"),
            status="pending",
            attempts=0,
            notification_attempts=0,
            submitted_at_utc=datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
            next_attempt_at_utc=datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
        )

        text, blocks = queued_message(
            EnqueueResult(request=request, created=True, position=5),
            self.settings,
        )

        self.assertIn("position 5", text)
        self.assertIn("kayak1, kayak2", text)
        self.assertIn("not confirmed yet", str(blocks))
        self.assertIn("12345678", str(blocks))

    def test_duplicate_pending_submission_still_gets_a_response(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1",),
            start_at_utc=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
            end_at_utc=self.end,
            requester_user_id="U123",
        )
        request = QueuedReservationRequest(
            request_id="12345678-abcd",
            dedupe_key="event-1",
            pending=pending,
            reserved_by_name="Taylor Smith",
            destination=QueueDestination("D123"),
            status="pending",
            attempts=0,
            notification_attempts=0,
            submitted_at_utc=datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
            next_attempt_at_utc=datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
        )

        text, blocks = queued_submission_message(
            EnqueueResult(request=request, created=False, position=1),
            self.settings,
        )

        self.assertIn("queued", text)
        self.assertIsNotNone(blocks)

    def test_status_shows_current_owner_and_end_time(self) -> None:
        item = Item("kayak1", "A", self.end, "U123")

        text = status_text(
            [InventoryAvailability(item=item, available=False)], self.settings
        )

        self.assertIn("reserved until", text)
        self.assertIn("<@U123>", text)

    def test_help_lists_only_supported_commands(self) -> None:
        self.assertIn("`reserve`", help_text())
        self.assertIn("`cancel`", help_text())
        self.assertIn("cancel <item>", help_text())
        self.assertIn("cancel group <item>", help_text())
        self.assertIn("`help`", help_text())
        self.assertNotIn("from <date/time>", help_text())

    def test_cancellation_message_lists_cancelled_and_remaining_items(self) -> None:
        start = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
        cancelled = Reservation("R1", "kayak1", "A", start, self.end, "G1")
        remaining = Reservation("R2", "kayak2", "B", start, self.end, "G1")
        result = CancellationResult(
            cancelled=ReservationGroup("G1", (cancelled,)),
            remaining=(remaining,),
        )

        text = cancellation_result_message(result, self.settings)

        self.assertIn("Cancelled *kayak1*", text)
        self.assertIn("Remaining in this reservation: *kayak2*", text)

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
    cancellation_result_message,
