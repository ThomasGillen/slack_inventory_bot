from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest import TestCase

from inventory_bot.errors import AmbiguousItemError, AvailabilityError, CancellationError
from inventory_bot.models import Item, ScheduledReservation
from inventory_bot.service import ReservationService


class FakeRepository:
    def __init__(
        self,
        *,
        items: list[Item],
        reservations: list[ScheduledReservation] | None = None,
    ) -> None:
        self.items = items
        self.reservations = reservations or []
        self.updates: list[tuple[str, datetime, str]] = []

    def list_items(self) -> list[Item]:
        return list(self.items)

    def list_reservations(self) -> list[ScheduledReservation]:
        return list(self.reservations)

    def add_reservation(self, reservation: ScheduledReservation) -> None:
        self.reservations.append(reservation)

    def delete_reservation(self, *, reservation_id: str) -> None:
        self.reservations = [
            reservation
            for reservation in self.reservations
            if reservation.reservation_id != reservation_id
        ]

    def update_item_reservation(
        self,
        *,
        item_name: str,
        reservation_end_utc: datetime,
        reserved_by: str,
    ) -> None:
        for index, item in enumerate(self.items):
            if item.item_name.casefold() == item_name.casefold():
                self.items[index] = replace(
                    item,
                    reservation_end_utc=reservation_end_utc,
                    reserved_by=reserved_by,
                )
                self.updates.append((item_name, reservation_end_utc, reserved_by))
                return
        raise AssertionError(f"Missing fake item: {item_name}")

    def clear_item_reservation(self, *, item_name: str) -> None:
        for index, item in enumerate(self.items):
            if item.item_name.casefold() == item_name.casefold():
                self.items[index] = replace(
                    item,
                    reservation_end_utc=None,
                    reserved_by="",
                )
                return
        raise AssertionError(f"Missing fake item: {item_name}")


class ReservationServiceTests(TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
        self.items = [
            Item("kayak1", "A"),
            Item("kayak2", "B"),
        ]
        self.repository = FakeRepository(items=self.items)
        self.service = ReservationService(
            self.repository,
            timezone=timezone(timedelta(hours=-7), name="PDT"),
            clock=lambda: self.now,
        )

    def prepare(self, *, text: str = "reserve kayak1 until tomorrow at 3 PM"):
        return self.service.prepare(
            text,
            requester_user_id="U123",
        )

    def test_prepare_and_commit_updates_current_item_state(self) -> None:
        prepared = self.prepare()
        reservation = self.service.commit(
            prepared.pending, reserved_by_name="Taylor Smith"
        )

        updated = self.repository.items[0]
        self.assertEqual("kayak1", reservation.item_name)
        self.assertEqual("A", reservation.location)
        self.assertEqual(reservation.end_at_utc, updated.reservation_end_utc)
        self.assertEqual("Taylor Smith", updated.reserved_by)
        self.assertEqual(1, len(self.repository.updates))
        self.assertEqual(self.now, reservation.start_at_utc)
        self.assertEqual(1, len(self.repository.reservations))

    def test_future_end_time_makes_item_unavailable(self) -> None:
        self.repository.reservations.append(
            ScheduledReservation(
                "R1",
                "kayak1",
                self.now - timedelta(hours=1),
                self.now + timedelta(hours=1),
                "Morgan Jones",
                "U999",
            )
        )

        statuses = self.service.inventory_status("kayak1")

        self.assertFalse(statuses[0].available)

    def test_past_end_time_makes_item_available(self) -> None:
        self.repository.items[0] = replace(
            self.repository.items[0],
            reservation_end_utc=self.now - timedelta(seconds=1),
            reserved_by="U999",
        )

        statuses = self.service.inventory_status("kayak1")

        self.assertTrue(statuses[0].available)
        self.assertIsNone(self.repository.items[0].reservation_end_utc)

    def test_prepare_rejects_currently_reserved_item(self) -> None:
        self.repository.items[0] = replace(
            self.repository.items[0],
            reservation_end_utc=self.now + timedelta(hours=1),
        )

        with self.assertRaisesRegex(AvailabilityError, "reserved until"):
            self.prepare()

    def test_commit_rechecks_current_state(self) -> None:
        prepared = self.prepare()
        self.repository.items[0] = replace(
            self.repository.items[0],
            reservation_end_utc=self.now + timedelta(hours=1),
            reserved_by="U999",
        )

        with self.assertRaises(AvailabilityError):
            self.service.commit(prepared.pending)

    def test_second_confirmation_cannot_overwrite_active_reservation(self) -> None:
        prepared = self.prepare()
        self.service.commit(prepared.pending)

        with self.assertRaises(AvailabilityError):
            self.service.commit(prepared.pending)

    def test_partial_item_match_must_be_unambiguous(self) -> None:
        self.repository.items.append(Item("kayak3", "A"))

        with self.assertRaises(AmbiguousItemError):
            self.prepare(text="reserve kayak until tomorrow at 3 PM")

    def test_inventory_picker_includes_items_regardless_of_current_state(self) -> None:
        self.repository.items[1] = replace(
            self.repository.items[1],
            reservation_end_utc=self.now + timedelta(hours=1),
            reserved_by="U999",
        )
        self.repository.items.append(Item("paddle1", "A"))

        items = self.service.inventory_items("kay")

        self.assertEqual(["kayak1", "kayak2"], [item.item_name for item in items])

    def test_reserver_can_cancel_active_reservation(self) -> None:
        prepared = self.prepare()
        self.service.commit(prepared.pending, reserved_by_name="Taylor Smith")

        reservation = self.service.cancel(
            "kayak1", requester_user_id="U123", requester_name="Taylor Smith"
        )

        self.assertEqual("kayak1", reservation.item_name)
        self.assertIsNone(self.repository.items[0].reservation_end_utc)
        self.assertEqual("", self.repository.items[0].reserved_by)
        self.assertEqual([], self.repository.reservations)

    def test_different_user_cannot_cancel_reservation(self) -> None:
        prepared = self.prepare()
        self.service.commit(prepared.pending, reserved_by_name="Taylor Smith")

        with self.assertRaisesRegex(CancellationError, "Only the person"):
            self.service.cancel(
                "kayak1", requester_user_id="U999", requester_name="Morgan Jones"
            )

    def test_legacy_slack_id_owner_can_still_cancel(self) -> None:
        prepared = self.prepare()
        self.service.commit(prepared.pending)

        reservation = self.service.cancel(
            "kayak1", requester_user_id="U123", requester_name="Taylor Smith"
        )

        self.assertEqual("kayak1", reservation.item_name)

    def test_cannot_cancel_available_item(self) -> None:
        with self.assertRaisesRegex(CancellationError, "active or upcoming"):
            self.service.cancel("kayak1", requester_user_id="U123")

    def test_future_reservation_does_not_activate_item_early(self) -> None:
        prepared = self.prepare(
            text=(
                "reserve kayak1 from tomorrow at 1 PM "
                "until tomorrow at 3 PM"
            )
        )

        reservation = self.service.commit(
            prepared.pending, reserved_by_name="Taylor Smith"
        )

        self.assertGreater(reservation.start_at_utc, self.now)
        self.assertIsNone(self.repository.items[0].reservation_end_utc)
        self.assertEqual(1, len(self.repository.reservations))

    def test_overlapping_future_reservation_is_rejected(self) -> None:
        first = self.prepare(
            text=(
                "reserve kayak1 from tomorrow at 1 PM "
                "until tomorrow at 3 PM"
            )
        )
        self.service.commit(first.pending, reserved_by_name="Taylor Smith")

        with self.assertRaisesRegex(AvailabilityError, "already reserved from"):
            self.service.prepare(
                "reserve kayak1 from tomorrow at 2 PM until tomorrow at 4 PM",
                requester_user_id="U999",
            )

    def test_commit_rechecks_future_schedule_for_overlap(self) -> None:
        prepared = self.prepare(
            text=(
                "reserve kayak1 from tomorrow at 1 PM "
                "until tomorrow at 3 PM"
            )
        )
        self.repository.reservations.append(
            ScheduledReservation(
                "R-other",
                "kayak1",
                prepared.pending.start_at_utc + timedelta(minutes=30),
                prepared.pending.end_at_utc + timedelta(hours=1),
                "Morgan Jones",
                "U999",
            )
        )

        with self.assertRaises(AvailabilityError):
            self.service.commit(prepared.pending, reserved_by_name="Taylor Smith")

    def test_back_to_back_reservations_are_allowed(self) -> None:
        first = self.prepare(
            text=(
                "reserve kayak1 from tomorrow at 1 PM "
                "until tomorrow at 3 PM"
            )
        )
        self.service.commit(first.pending, reserved_by_name="Taylor Smith")
        second = self.service.prepare(
            "reserve kayak1 from tomorrow at 3 PM until tomorrow at 4 PM",
            requester_user_id="U999",
        )

        self.service.commit(second.pending, reserved_by_name="Morgan Jones")

        self.assertEqual(2, len(self.repository.reservations))

    def test_reconcile_activates_then_expires_scheduled_reservation(self) -> None:
        self.repository.reservations.append(
            ScheduledReservation(
                "R1",
                "kayak1",
                self.now + timedelta(minutes=30),
                self.now + timedelta(hours=2),
                "Taylor Smith",
                "U123",
            )
        )

        self.now += timedelta(hours=1)
        self.service.reconcile()

        self.assertEqual(
            self.repository.reservations[0].end_at_utc,
            self.repository.items[0].reservation_end_utc,
        )
        self.assertEqual("Taylor Smith", self.repository.items[0].reserved_by)

        self.now += timedelta(hours=2)
        self.service.reconcile()

        self.assertEqual([], self.repository.reservations)
        self.assertIsNone(self.repository.items[0].reservation_end_utc)

    def test_cancels_earliest_owned_upcoming_reservation(self) -> None:
        for index, hours in enumerate((3, 6), start=1):
            self.repository.reservations.append(
                ScheduledReservation(
                    f"R{index}",
                    "kayak1",
                    self.now + timedelta(hours=hours),
                    self.now + timedelta(hours=hours + 1),
                    "Taylor Smith",
                    "U123",
                )
            )

        cancelled = self.service.cancel(
            "kayak1", requester_user_id="U123", requester_name="Taylor Smith"
        )

        self.assertEqual("R1", cancelled.reservation_id)
        self.assertEqual(["R2"], [value.reservation_id for value in self.repository.reservations])
