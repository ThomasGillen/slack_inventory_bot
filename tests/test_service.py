from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest import TestCase

from inventory_bot.errors import (
    AmbiguousItemError,
    AvailabilityError,
    CancellationError,
    ParseError,
)
from inventory_bot.models import Item, PendingReservation, ScheduledReservation
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
        self.add_batches: list[list[ScheduledReservation]] = []
        self.delete_batches: list[list[str]] = []
        self.update_batches: list[list[ScheduledReservation]] = []
        self.clear_batches: list[list[str]] = []
        self.list_item_calls = 0

    def list_items(self) -> list[Item]:
        self.list_item_calls += 1
        return list(self.items)

    def list_reservations(self) -> list[ScheduledReservation]:
        return list(self.reservations)

    def add_reservations(self, reservations: list[ScheduledReservation]) -> None:
        self.add_batches.append(list(reservations))
        self.reservations.extend(reservations)

    def delete_reservations(self, *, reservation_ids: list[str]) -> None:
        self.delete_batches.append(list(reservation_ids))
        ids = set(reservation_ids)
        self.reservations = [
            reservation
            for reservation in self.reservations
            if reservation.reservation_id not in ids
        ]

    def update_item_reservations(
        self, reservations: list[ScheduledReservation]
    ) -> None:
        self.update_batches.append(list(reservations))
        for reservation in reservations:
            for index, item in enumerate(self.items):
                if item.item_name.casefold() == reservation.item_name.casefold():
                    self.items[index] = replace(
                        item,
                        reservation_end_utc=reservation.end_at_utc,
                        reserved_by=reservation.reserved_by,
                    )
                    self.updates.append(
                        (
                            reservation.item_name,
                            reservation.end_at_utc,
                            reservation.reserved_by,
                        )
                    )
                    break
            else:
                raise AssertionError(f"Missing fake item: {reservation.item_name}")

    def clear_item_reservations(self, *, item_names: list[str]) -> None:
        self.clear_batches.append(list(item_names))
        for item_name in item_names:
            for index, item in enumerate(self.items):
                if item.item_name.casefold() == item_name.casefold():
                    self.items[index] = replace(
                        item,
                        reservation_end_utc=None,
                        reserved_by="",
                    )
                    break
            else:
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

    def pending(
        self,
        *,
        item_names: tuple[str, ...] = ("kayak1",),
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        requester_user_id: str = "U123",
    ) -> PendingReservation:
        return PendingReservation(
            item_names=item_names,
            start_at_utc=start_at,
            end_at_utc=end_at or self.now + timedelta(hours=2),
            requester_user_id=requester_user_id,
        )

    def test_commit_updates_current_item_state(self) -> None:
        pending = self.pending()
        reservation = self.service.commit(
            pending, reserved_by_name="Taylor Smith"
        )

        updated = self.repository.items[0]
        self.assertEqual(("kayak1",), reservation.item_names)
        self.assertEqual("A", reservation.reservations[0].location)
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

    def test_commit_rejects_currently_reserved_item(self) -> None:
        self.repository.items[0] = replace(
            self.repository.items[0],
            reservation_end_utc=self.now + timedelta(hours=1),
        )

        with self.assertRaisesRegex(AvailabilityError, "reserved until"):
            self.service.commit(self.pending())

    def test_second_commit_cannot_overwrite_active_reservation(self) -> None:
        pending = self.pending()
        self.service.commit(pending)

        with self.assertRaises(AvailabilityError):
            self.service.commit(pending)

    def test_partial_item_match_must_be_unambiguous(self) -> None:
        self.repository.items.append(Item("kayak3", "A"))

        with self.assertRaises(AmbiguousItemError):
            self.service.inventory_status("kayak")

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
        self.service.commit(self.pending(), reserved_by_name="Taylor Smith")

        result = self.service.cancel(
            "kayak1", requester_user_id="U123", requester_name="Taylor Smith"
        )

        self.assertEqual(("kayak1",), result.cancelled.item_names)
        self.assertIsNone(self.repository.items[0].reservation_end_utc)
        self.assertEqual("", self.repository.items[0].reserved_by)
        self.assertEqual([], self.repository.reservations)

    def test_different_user_cannot_cancel_reservation(self) -> None:
        self.service.commit(self.pending(), reserved_by_name="Taylor Smith")

        with self.assertRaisesRegex(CancellationError, "Only the person"):
            self.service.cancel(
                "kayak1", requester_user_id="U999", requester_name="Morgan Jones"
            )

    def test_legacy_slack_id_owner_can_still_cancel(self) -> None:
        self.service.commit(self.pending())

        result = self.service.cancel(
            "kayak1", requester_user_id="U123", requester_name="Taylor Smith"
        )

        self.assertEqual(("kayak1",), result.cancelled.item_names)

    def test_cannot_cancel_available_item(self) -> None:
        with self.assertRaisesRegex(CancellationError, "active or upcoming"):
            self.service.cancel("kayak1", requester_user_id="U123")

    def test_future_reservation_does_not_activate_item_early(self) -> None:
        pending = self.pending(
            start_at=self.now + timedelta(hours=22),
            end_at=self.now + timedelta(hours=24),
        )

        reservation = self.service.commit(
            pending, reserved_by_name="Taylor Smith"
        )

        self.assertGreater(reservation.start_at_utc, self.now)
        self.assertIsNone(self.repository.items[0].reservation_end_utc)
        self.assertEqual(1, len(self.repository.reservations))

    def test_overlapping_future_reservation_is_rejected(self) -> None:
        first = self.pending(
            start_at=self.now + timedelta(hours=22),
            end_at=self.now + timedelta(hours=24),
        )
        self.service.commit(first, reserved_by_name="Taylor Smith")

        with self.assertRaisesRegex(AvailabilityError, "already reserved from"):
            self.service.commit(
                self.pending(
                    start_at=self.now + timedelta(hours=23),
                    end_at=self.now + timedelta(hours=25),
                    requester_user_id="U999",
                ),
                reserved_by_name="Morgan Jones",
            )

    def test_commit_rechecks_future_schedule_for_overlap(self) -> None:
        pending = self.pending(
            start_at=self.now + timedelta(hours=22),
            end_at=self.now + timedelta(hours=24),
        )
        self.repository.reservations.append(
            ScheduledReservation(
                "R-other",
                "kayak1",
                pending.start_at_utc + timedelta(minutes=30),
                pending.end_at_utc + timedelta(hours=1),
                "Morgan Jones",
                "U999",
            )
        )

        with self.assertRaises(AvailabilityError):
            self.service.commit(pending, reserved_by_name="Taylor Smith")

    def test_back_to_back_reservations_are_allowed(self) -> None:
        first = self.pending(
            start_at=self.now + timedelta(hours=22),
            end_at=self.now + timedelta(hours=24),
        )
        self.service.commit(first, reserved_by_name="Taylor Smith")
        second = self.pending(
            start_at=self.now + timedelta(hours=24),
            end_at=self.now + timedelta(hours=25),
            requester_user_id="U999",
        )

        self.service.commit(second, reserved_by_name="Morgan Jones")

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

    def test_multiple_upcoming_reservations_require_manager_choice(self) -> None:
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

        with self.assertRaisesRegex(CancellationError, "multiple upcoming"):
            self.service.cancel(
                "kayak1",
                requester_user_id="U123",
                requester_name="Taylor Smith",
            )

        self.assertEqual(
            ["R1", "R2"],
            [value.reservation_id for value in self.repository.reservations],
        )

    def test_multi_item_commit_is_grouped_and_batched(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )

        reservation_group = self.service.commit(
            pending, reserved_by_name="Taylor Smith"
        )

        self.assertEqual(("kayak1", "kayak2"), reservation_group.item_names)
        self.assertEqual(2, len(reservation_group.reservations))
        self.assertTrue(reservation_group.group_id)
        self.assertEqual(
            {reservation_group.group_id},
            {value.group_id for value in self.repository.reservations},
        )
        self.assertEqual(1, len(self.repository.add_batches))
        self.assertEqual(1, len(self.repository.update_batches))
        self.assertEqual(2, len(self.repository.add_batches[0]))
        self.assertEqual(2, len(self.repository.update_batches[0]))
        self.assertTrue(all(item.reservation_end_utc for item in self.repository.items))

    def test_queue_group_id_makes_commit_idempotent(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )

        first = self.service.commit(
            pending,
            reserved_by_name="Taylor Smith",
            group_id="queue-request-1",
        )
        second = self.service.commit(
            pending,
            reserved_by_name="Taylor Smith",
            group_id="queue-request-1",
        )

        self.assertEqual("queue-request-1", first.group_id)
        self.assertEqual(first, second)
        self.assertEqual(2, len(self.repository.reservations))
        self.assertEqual(1, len(self.repository.add_batches))
        self.assertEqual(1, len(self.repository.update_batches))

    def test_queue_group_id_collision_is_rejected(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1",),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )
        self.service.commit(pending, group_id="queue-request-1")
        different = PendingReservation(
            item_names=("kayak2",),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )

        with self.assertRaisesRegex(ParseError, "different reservation"):
            self.service.commit(different, group_id="queue-request-1")

    def test_item_picker_cache_collapses_repeated_sheet_reads(self) -> None:
        service = ReservationService(
            self.repository,
            timezone=timezone(timedelta(hours=-7), name="PDT"),
            clock=lambda: self.now,
            inventory_cache_ttl_seconds=15,
        )

        service.inventory_items("kay")
        service.inventory_items("kayak1")

        self.assertEqual(1, self.repository.list_item_calls)

    def test_multi_item_conflict_rejects_entire_group(self) -> None:
        existing = ScheduledReservation(
            "R-existing",
            "kayak2",
            self.now,
            self.now + timedelta(hours=2),
            "Morgan Jones",
            "U999",
            "G-existing",
        )
        self.repository.reservations.append(existing)
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=1),
            requester_user_id="U123",
        )

        with self.assertRaisesRegex(AvailabilityError, "kayak2"):
            self.service.commit(pending, reserved_by_name="Taylor Smith")

        self.assertEqual([existing], self.repository.reservations)
        self.assertEqual([], self.repository.add_batches)
        self.assertEqual([], self.repository.update_batches)

    def test_multi_item_commit_limits_group_to_twenty_items(self) -> None:
        pending = PendingReservation(
            item_names=tuple(f"item-{index}" for index in range(21)),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=1),
            requester_user_id="U123",
        )

        with self.assertRaisesRegex(ParseError, "no more than 20"):
            self.service.commit(pending)

        self.assertEqual([], self.repository.add_batches)

    def test_multi_item_commit_accepts_twenty_items(self) -> None:
        items = [Item(f"item-{index}", "A") for index in range(20)]
        repository = FakeRepository(items=items)
        service = ReservationService(
            repository,
            timezone=timezone(timedelta(hours=-7), name="PDT"),
            clock=lambda: self.now,
        )
        pending = PendingReservation(
            item_names=tuple(item.item_name for item in items),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=1),
            requester_user_id="U123",
        )

        group = service.commit(pending, reserved_by_name="Taylor Smith")

        self.assertEqual(20, len(group.reservations))
        self.assertEqual(20, len(repository.add_batches[0]))

    def test_cancel_one_item_preserves_rest_of_group(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )
        committed = self.service.commit(pending, reserved_by_name="Taylor Smith")

        result = self.service.cancel(
            "kayak1", requester_user_id="U123", requester_name="Taylor Smith"
        )

        self.assertEqual(committed.group_id, result.cancelled.group_id)
        self.assertEqual(("kayak1",), result.cancelled.item_names)
        self.assertEqual(
            ("kayak2",),
            tuple(value.item_name for value in result.remaining),
        )
        self.assertEqual(
            ["kayak2"],
            [value.item_name for value in self.repository.reservations],
        )
        self.assertEqual(1, len(self.repository.delete_batches))
        self.assertEqual(1, len(self.repository.delete_batches[0]))
        self.assertIsNone(self.repository.items[0].reservation_end_utc)
        self.assertIsNotNone(self.repository.items[1].reservation_end_utc)

    def test_explicit_group_cancel_removes_every_item(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )
        committed = self.service.commit(pending, reserved_by_name="Taylor Smith")

        result = self.service.cancel(
            "kayak1",
            requester_user_id="U123",
            requester_name="Taylor Smith",
            whole_group=True,
        )

        self.assertEqual(committed.group_id, result.cancelled.group_id)
        self.assertEqual(("kayak1", "kayak2"), result.cancelled.item_names)
        self.assertEqual((), result.remaining)
        self.assertEqual([], self.repository.reservations)
        self.assertEqual(2, len(self.repository.delete_batches[0]))

    def test_cancel_selected_removes_only_chosen_group_rows(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )
        committed = self.service.commit(pending, reserved_by_name="Taylor Smith")
        kayak2 = next(
            value
            for value in committed.reservations
            if value.item_name == "kayak2"
        )

        result = self.service.cancel_selected(
            committed.group_id,
            (kayak2.reservation_id,),
            requester_user_id="U123",
            requester_name="Taylor Smith",
        )

        self.assertEqual(("kayak2",), result.cancelled.item_names)
        self.assertEqual(
            ("kayak1",),
            tuple(value.item_name for value in result.remaining),
        )
        self.assertEqual(
            ["kayak1"],
            [value.item_name for value in self.repository.reservations],
        )

    def test_cancel_group_by_id_rechecks_owner_and_removes_all_rows(self) -> None:
        pending = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=self.now,
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )
        committed = self.service.commit(pending, reserved_by_name="Taylor Smith")

        with self.assertRaisesRegex(CancellationError, "Only the person"):
            self.service.cancel_group(
                committed.group_id,
                requester_user_id="U999",
                requester_name="Morgan Jones",
            )

        result = self.service.cancel_group(
            committed.group_id,
            requester_user_id="U123",
            requester_name="Taylor Smith",
        )

        self.assertEqual(("kayak1", "kayak2"), result.cancelled.item_names)
        self.assertEqual([], self.repository.reservations)

    def test_lists_only_requesters_reservation_groups(self) -> None:
        own = PendingReservation(
            item_names=("kayak1", "kayak2"),
            start_at_utc=self.now + timedelta(hours=1),
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )
        committed = self.service.commit(own, reserved_by_name="Taylor Smith")
        self.repository.reservations.append(
            ScheduledReservation(
                "R-other",
                "paddle1",
                self.now + timedelta(hours=1),
                self.now + timedelta(hours=2),
                "Morgan Jones",
                "U999",
                "G-other",
            )
        )

        groups = self.service.reservation_groups_for_user(
            requester_user_id="U123", requester_name="Taylor Smith"
        )

        self.assertEqual([committed.group_id], [group.group_id for group in groups])
        self.assertEqual(("kayak1", "kayak2"), groups[0].item_names)
