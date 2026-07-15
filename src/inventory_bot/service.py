"""Reservation rules independent of Slack and Google APIs."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo

from .errors import (
    AmbiguousItemError,
    AvailabilityError,
    CancellationError,
    ItemNotFoundError,
    ParseError,
)
from .models import (
    InventoryAvailability,
    Item,
    PendingReservation,
    PreparedReservation,
    Reservation,
    ScheduledReservation,
)
from .parser import parse_reservation_message
from .repository import InventoryRepository


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


class ReservationService:
    def __init__(
        self,
        repository: InventoryRepository,
        *,
        timezone: tzinfo,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.repository = repository
        self.timezone = timezone
        self.clock = clock
        self._write_lock = threading.Lock()

    def prepare(
        self,
        text: str,
        *,
        requester_user_id: str,
    ) -> PreparedReservation:
        now = self._now()
        parsed = parse_reservation_message(
            text,
            timezone=self.timezone,
            now=now.astimezone(self.timezone),
        )
        item = self._resolve_item(parsed.item_query, self.repository.list_items())
        if not self._is_available(item, now):
            raise AvailabilityError(self._reserved_message(item))

        pending = PendingReservation(
            item_name=item.item_name,
            start_at_utc=parsed.start_at_utc,
            end_at_utc=parsed.end_at_utc,
            requester_user_id=requester_user_id,
        )
        self._assert_no_overlap(item, pending, now, self.repository.list_reservations())
        return PreparedReservation(item=item, pending=pending)

    def commit(
        self,
        pending: PendingReservation,
        *,
        reserved_by_name: str | None = None,
    ) -> Reservation:
        with self._write_lock:
            now = self._now()
            item = self._item_by_name(pending.item_name, self.repository.list_items())
            if pending.end_at_utc <= now:
                raise ParseError("The reservation end time has already passed.")
            start_at = (pending.start_at_utc or now).astimezone(UTC)
            if start_at < now:
                start_at = now
            if pending.end_at_utc <= start_at:
                raise ParseError("Reservation end time must be after its start time.")

            reservations = self._remove_expired(now)
            normalized_pending = PendingReservation(
                item_name=item.item_name,
                start_at_utc=start_at,
                end_at_utc=pending.end_at_utc.astimezone(UTC),
                requester_user_id=pending.requester_user_id,
            )
            self._assert_no_overlap(item, normalized_pending, now, reservations)

            scheduled = ScheduledReservation(
                reservation_id=str(uuid.uuid4()),
                item_name=item.item_name,
                start_at_utc=start_at,
                end_at_utc=pending.end_at_utc.astimezone(UTC),
                reserved_by=" ".join(
                    (reserved_by_name or pending.requester_user_id).split()
                ),
                slack_user_id=pending.requester_user_id,
            )
            self.repository.add_reservation(scheduled)
            if start_at <= now:
                self.repository.update_item_reservation(
                    item_name=item.item_name,
                    reservation_end_utc=scheduled.end_at_utc,
                    reserved_by=scheduled.reserved_by,
                )
            return Reservation(
                reservation_id=scheduled.reservation_id,
                item_name=item.item_name,
                location=item.location,
                start_at_utc=scheduled.start_at_utc,
                end_at_utc=scheduled.end_at_utc,
            )

    def reconcile(self) -> None:
        """Make Items reflect the active schedule and remove ended bookings."""
        with self._write_lock:
            self._reconcile_locked(self._now())

    def inventory_status(self, item_query: str = "") -> list[InventoryAvailability]:
        self.reconcile()
        now = self._now()
        items = self.repository.list_items()
        reservations = self.repository.list_reservations()
        if item_query.strip():
            items = [self._resolve_item(item_query, items)]
        statuses: list[InventoryAvailability] = []
        for item in sorted(items, key=lambda candidate: candidate.item_name.casefold()):
            upcoming = sorted(
                (
                    reservation
                    for reservation in reservations
                    if _normalized(reservation.item_name) == _normalized(item.item_name)
                    and reservation.start_at_utc > now
                ),
                key=lambda value: value.start_at_utc,
            )
            statuses.append(
                InventoryAvailability(
                    item=item,
                    available=self._is_available(item, now),
                    next_reservation=upcoming[0] if upcoming else None,
                )
            )
        return statuses

    def inventory_items(self, query: str = "", *, limit: int = 100) -> list[Item]:
        needle = _normalized(query)
        matches = [
            item
            for item in self.repository.list_items()
            if not needle or needle in _normalized(item.item_name)
        ]
        return sorted(matches, key=lambda item: item.item_name.casefold())[:limit]

    def cancel(
        self,
        item_query: str,
        *,
        requester_user_id: str,
        requester_name: str = "",
    ) -> Reservation:
        with self._write_lock:
            now = self._now()
            item = self._resolve_item(item_query, self.repository.list_items())
            owners = {_normalized(requester_user_id)}
            if requester_name:
                owners.add(_normalized(requester_name))

            reservations = [
                reservation
                for reservation in self._remove_expired(now)
                if _normalized(reservation.item_name) == _normalized(item.item_name)
                and reservation.end_at_utc > now
            ]
            owned = [
                reservation
                for reservation in reservations
                if reservation.slack_user_id == requester_user_id
                or _normalized(reservation.reserved_by) in owners
            ]
            if owned:
                active = [reservation for reservation in owned if reservation.start_at_utc <= now]
                selected = min(active or owned, key=lambda value: value.start_at_utc)
                self.repository.delete_reservation(
                    reservation_id=selected.reservation_id
                )
                self._reconcile_locked(now)
                return Reservation(
                    reservation_id=selected.reservation_id,
                    item_name=item.item_name,
                    location=item.location,
                    start_at_utc=selected.start_at_utc,
                    end_at_utc=selected.end_at_utc,
                )

            if reservations:
                raise CancellationError(
                    f"Only the person who reserved {item.item_name} can cancel it."
                )

            if self._is_available(item, now):
                raise CancellationError(
                    f"{item.item_name} does not have an active or upcoming reservation."
                )
            if not item.reserved_by:
                raise CancellationError(
                    f"{item.item_name} has no Slack owner. Clear its reservation cells in the sheet."
                )
            if _normalized(item.reserved_by) not in owners:
                raise CancellationError(
                    f"Only the person who reserved {item.item_name} can cancel it."
                )
            self.repository.clear_item_reservation(item_name=item.item_name)
            return Reservation(
                reservation_id="",
                item_name=item.item_name,
                location=item.location,
                start_at_utc=now,
                end_at_utc=item.reservation_end_utc,
            )

    def _now(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None:
            raise RuntimeError("ReservationService clock must return a timezone-aware datetime.")
        return current.astimezone(UTC)

    @staticmethod
    def _is_available(item: Item, now: datetime) -> bool:
        return item.reservation_end_utc is None or item.reservation_end_utc <= now

    def _reserved_message(self, item: Item) -> str:
        if item.reservation_end_utc is None:
            return f"{item.item_name} is currently reserved."
        local_end = item.reservation_end_utc.astimezone(self.timezone)
        end_text = local_end.strftime("%a, %b %d, %Y at %I:%M %p %Z")
        return f"{item.item_name} is reserved until {end_text}."

    def _assert_no_overlap(
        self,
        item: Item,
        pending: PendingReservation,
        now: datetime,
        reservations: list[ScheduledReservation],
    ) -> None:
        start_at = (pending.start_at_utc or now).astimezone(UTC)
        end_at = pending.end_at_utc.astimezone(UTC)
        for existing in reservations:
            if _normalized(existing.item_name) != _normalized(item.item_name):
                continue
            if start_at < existing.end_at_utc and end_at > existing.start_at_utc:
                raise AvailabilityError(self._overlap_message(item, existing))

        if (
            item.reservation_end_utc is not None
            and item.reservation_end_utc > now
            and start_at < item.reservation_end_utc
            and not any(
                _normalized(existing.item_name) == _normalized(item.item_name)
                and existing.start_at_utc <= now < existing.end_at_utc
                for existing in reservations
            )
        ):
            raise AvailabilityError(self._reserved_message(item))

    def _overlap_message(
        self, item: Item, existing: ScheduledReservation
    ) -> str:
        start_text = existing.start_at_utc.astimezone(self.timezone).strftime(
            "%a, %b %d at %I:%M %p %Z"
        )
        end_text = existing.end_at_utc.astimezone(self.timezone).strftime(
            "%a, %b %d at %I:%M %p %Z"
        )
        return f"{item.item_name} is already reserved from {start_text} until {end_text}."

    def _remove_expired(self, now: datetime) -> list[ScheduledReservation]:
        reservations = self.repository.list_reservations()
        active_or_future: list[ScheduledReservation] = []
        for reservation in reservations:
            if reservation.end_at_utc <= now:
                self.repository.delete_reservation(
                    reservation_id=reservation.reservation_id
                )
            else:
                active_or_future.append(reservation)
        return active_or_future

    def _reconcile_locked(self, now: datetime) -> None:
        reservations = self._remove_expired(now)
        items = self.repository.list_items()
        for item in items:
            active = [
                reservation
                for reservation in reservations
                if _normalized(reservation.item_name) == _normalized(item.item_name)
                and reservation.start_at_utc <= now < reservation.end_at_utc
            ]
            if len(active) > 1:
                raise AvailabilityError(
                    f"{item.item_name} has overlapping active rows in the Reservations sheet."
                )
            if active:
                reservation = active[0]
                if (
                    item.reservation_end_utc != reservation.end_at_utc
                    or item.reserved_by != reservation.reserved_by
                ):
                    self.repository.update_item_reservation(
                        item_name=item.item_name,
                        reservation_end_utc=reservation.end_at_utc,
                        reserved_by=reservation.reserved_by,
                    )
            elif item.reservation_end_utc is not None or item.reserved_by:
                self.repository.clear_item_reservation(item_name=item.item_name)

    @staticmethod
    def _item_by_name(item_name: str, items: list[Item]) -> Item:
        needle = _normalized(item_name)
        matches = [item for item in items if _normalized(item.item_name) == needle]
        if not matches:
            raise ItemNotFoundError(f"Item {item_name!r} no longer exists.")
        if len(matches) > 1:
            raise AmbiguousItemError(f"More than one item is named {item_name!r}.")
        return matches[0]

    @staticmethod
    def _resolve_item(query: str, items: list[Item]) -> Item:
        needle = _normalized(query)
        exact = [item for item in items if _normalized(item.item_name) == needle]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise AmbiguousItemError(f"More than one item is named {query!r}.")

        partial = [item for item in items if needle in _normalized(item.item_name)]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = ", ".join(item.item_name for item in partial[:5])
            raise AmbiguousItemError(
                f"That item name is ambiguous. Try the full name: {names}."
            )
        raise ItemNotFoundError(f"I couldn't find an item matching {query!r}.")
