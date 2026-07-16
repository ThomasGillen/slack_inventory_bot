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
    CancellationResult,
    InventoryAvailability,
    Item,
    PendingReservation,
    PreparedReservation,
    Reservation,
    ReservationGroup,
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
            item_names=(item.item_name,),
            start_at_utc=parsed.start_at_utc,
            end_at_utc=parsed.end_at_utc,
            requester_user_id=requester_user_id,
        )
        self._assert_no_overlap(item, pending, now, self.repository.list_reservations())
        return PreparedReservation(items=(item,), pending=pending)

    def commit(
        self,
        pending: PendingReservation,
        *,
        reserved_by_name: str | None = None,
    ) -> ReservationGroup:
        with self._write_lock:
            now = self._now()
            items = self._items_by_name(
                pending.item_names, self.repository.list_items()
            )
            if pending.end_at_utc <= now:
                raise ParseError("The reservation end time has already passed.")
            start_at = (pending.start_at_utc or now).astimezone(UTC)
            if start_at < now:
                start_at = now
            if pending.end_at_utc <= start_at:
                raise ParseError("Reservation end time must be after its start time.")

            reservations = self._remove_expired(now)
            normalized_pending = PendingReservation(
                item_names=tuple(item.item_name for item in items),
                start_at_utc=start_at,
                end_at_utc=pending.end_at_utc.astimezone(UTC),
                requester_user_id=pending.requester_user_id,
            )
            for item in items:
                self._assert_no_overlap(
                    item, normalized_pending, now, reservations
                )

            group_id = str(uuid.uuid4())
            owner_name = " ".join(
                (reserved_by_name or pending.requester_user_id).split()
            )
            scheduled = [
                ScheduledReservation(
                    reservation_id=str(uuid.uuid4()),
                    item_name=item.item_name,
                    start_at_utc=start_at,
                    end_at_utc=pending.end_at_utc.astimezone(UTC),
                    reserved_by=owner_name,
                    slack_user_id=pending.requester_user_id,
                    group_id=group_id,
                )
                for item in items
            ]
            self.repository.add_reservations(scheduled)
            if start_at <= now:
                self.repository.update_item_reservations(scheduled)
            return ReservationGroup(
                group_id=group_id,
                reservations=tuple(
                    Reservation(
                        reservation_id=value.reservation_id,
                        item_name=item.item_name,
                        location=item.location,
                        start_at_utc=value.start_at_utc,
                        end_at_utc=value.end_at_utc,
                        group_id=group_id,
                    )
                    for item, value in zip(items, scheduled, strict=True)
                ),
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
        whole_group: bool = False,
    ) -> CancellationResult:
        """Cancel one item by default, or its complete group when requested."""
        with self._write_lock:
            now = self._now()
            items = self.repository.list_items()
            item = self._resolve_item(item_query, items)
            owners = self._owner_keys(requester_user_id, requester_name)

            all_reservations = self._remove_expired(now)
            reservations = [
                reservation
                for reservation in all_reservations
                if _normalized(reservation.item_name) == _normalized(item.item_name)
                and reservation.end_at_utc > now
            ]
            owned = [
                reservation
                for reservation in reservations
                if self._owned_by(reservation, requester_user_id, owners)
            ]
            if owned:
                active = [
                    reservation
                    for reservation in owned
                    if reservation.start_at_utc <= now
                ]
                candidates = active or owned
                if len(candidates) > 1:
                    raise CancellationError(
                        f"You have multiple upcoming reservations for {item.item_name}. "
                        "Send `cancel` and choose the reservation by time."
                    )
                selected = candidates[0]
                selected_rows = [selected]
                if whole_group:
                    group_id = self._effective_group_id(selected)
                    selected_rows = [
                        reservation
                        for reservation in all_reservations
                        if self._effective_group_id(reservation) == group_id
                    ]
                    self._assert_owned_rows(
                        selected_rows,
                        requester_user_id=requester_user_id,
                        owners=owners,
                    )
                return self._cancel_rows_locked(
                    selected_rows,
                    all_reservations=all_reservations,
                    items=items,
                    now=now,
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
            self.repository.clear_item_reservations(item_names=[item.item_name])
            return CancellationResult(
                cancelled=ReservationGroup(
                    group_id="",
                    reservations=(
                        Reservation(
                            reservation_id="",
                            item_name=item.item_name,
                            location=item.location,
                            start_at_utc=now,
                            end_at_utc=item.reservation_end_utc,
                        ),
                    ),
                ),
            )

    def reservation_groups_for_user(
        self,
        *,
        requester_user_id: str,
        requester_name: str = "",
    ) -> list[ReservationGroup]:
        """Return the requester's active and upcoming reservations by group."""
        with self._write_lock:
            now = self._now()
            items = self.repository.list_items()
            reservations = self._remove_expired(now)
            owners = self._owner_keys(requester_user_id, requester_name)
            owned = [
                reservation
                for reservation in reservations
                if self._owned_by(reservation, requester_user_id, owners)
            ]
            grouped: dict[str, list[ScheduledReservation]] = {}
            for reservation in owned:
                grouped.setdefault(
                    self._effective_group_id(reservation), []
                ).append(reservation)
            groups = [
                self._to_reservation_group(group_id, rows, items)
                for group_id, rows in grouped.items()
            ]
            return sorted(
                groups,
                key=lambda group: (group.start_at_utc, group.item_names),
            )

    def cancel_selected(
        self,
        group_id: str,
        reservation_ids: tuple[str, ...],
        *,
        requester_user_id: str,
        requester_name: str = "",
    ) -> CancellationResult:
        """Cancel selected item rows from one owned reservation group."""
        if not reservation_ids:
            raise CancellationError("Choose at least one item to cancel.")
        with self._write_lock:
            now = self._now()
            items = self.repository.list_items()
            reservations = self._remove_expired(now)
            owners = self._owner_keys(requester_user_id, requester_name)
            group_rows = self._owned_group_rows(
                group_id,
                reservations,
                requester_user_id=requester_user_id,
                owners=owners,
            )
            requested = set(reservation_ids)
            selected_rows = [
                reservation
                for reservation in group_rows
                if reservation.reservation_id in requested
            ]
            if len(selected_rows) != len(requested):
                raise CancellationError(
                    "One or more selected items no longer belong to this reservation."
                )
            return self._cancel_rows_locked(
                selected_rows,
                all_reservations=reservations,
                items=items,
                now=now,
            )

    def cancel_group(
        self,
        group_id: str,
        *,
        requester_user_id: str,
        requester_name: str = "",
    ) -> CancellationResult:
        """Cancel every item row in one owned reservation group."""
        with self._write_lock:
            now = self._now()
            items = self.repository.list_items()
            reservations = self._remove_expired(now)
            owners = self._owner_keys(requester_user_id, requester_name)
            group_rows = self._owned_group_rows(
                group_id,
                reservations,
                requester_user_id=requester_user_id,
                owners=owners,
            )
            return self._cancel_rows_locked(
                group_rows,
                all_reservations=reservations,
                items=items,
                now=now,
            )

    @staticmethod
    def _owner_keys(requester_user_id: str, requester_name: str) -> set[str]:
        owners = {_normalized(requester_user_id)}
        if requester_name:
            owners.add(_normalized(requester_name))
        return owners

    @staticmethod
    def _owned_by(
        reservation: ScheduledReservation,
        requester_user_id: str,
        owners: set[str],
    ) -> bool:
        if reservation.slack_user_id:
            return reservation.slack_user_id == requester_user_id
        return _normalized(reservation.reserved_by) in owners

    @staticmethod
    def _effective_group_id(reservation: ScheduledReservation) -> str:
        return reservation.group_id or reservation.reservation_id

    def _assert_owned_rows(
        self,
        reservations: list[ScheduledReservation],
        *,
        requester_user_id: str,
        owners: set[str],
    ) -> None:
        if not all(
            self._owned_by(reservation, requester_user_id, owners)
            for reservation in reservations
        ):
            raise CancellationError(
                "Only the person who made this reservation can cancel it."
            )

    def _owned_group_rows(
        self,
        group_id: str,
        reservations: list[ScheduledReservation],
        *,
        requester_user_id: str,
        owners: set[str],
    ) -> list[ScheduledReservation]:
        rows = [
            reservation
            for reservation in reservations
            if self._effective_group_id(reservation) == group_id
        ]
        if not rows:
            raise CancellationError(
                "That reservation no longer exists or has already ended."
            )
        self._assert_owned_rows(
            rows,
            requester_user_id=requester_user_id,
            owners=owners,
        )
        return rows

    def _cancel_rows_locked(
        self,
        selected_rows: list[ScheduledReservation],
        *,
        all_reservations: list[ScheduledReservation],
        items: list[Item],
        now: datetime,
    ) -> CancellationResult:
        if not selected_rows:
            raise CancellationError("Choose at least one item to cancel.")
        group_id = self._effective_group_id(selected_rows[0])
        if any(
            self._effective_group_id(reservation) != group_id
            for reservation in selected_rows
        ):
            raise CancellationError(
                "Selected items must belong to the same reservation."
            )
        selected_ids = {
            reservation.reservation_id for reservation in selected_rows
        }
        group_rows = [
            reservation
            for reservation in all_reservations
            if self._effective_group_id(reservation) == group_id
        ]
        remaining_rows = [
            reservation
            for reservation in group_rows
            if reservation.reservation_id not in selected_ids
        ]
        self.repository.delete_reservations(
            reservation_ids=sorted(selected_ids)
        )
        self._reconcile_locked(now)
        cancelled = self._to_reservation_group(group_id, selected_rows, items)
        remaining = self._to_reservation_group(
            group_id, remaining_rows, items
        ).reservations if remaining_rows else ()
        return CancellationResult(cancelled=cancelled, remaining=remaining)

    @staticmethod
    def _to_reservation_group(
        group_id: str,
        rows: list[ScheduledReservation],
        items: list[Item],
    ) -> ReservationGroup:
        locations_by_name = {
            _normalized(item.item_name): item.location for item in items
        }
        return ReservationGroup(
            group_id=group_id,
            reservations=tuple(
                Reservation(
                    reservation_id=reservation.reservation_id,
                    item_name=reservation.item_name,
                    location=locations_by_name.get(
                        _normalized(reservation.item_name), ""
                    ),
                    start_at_utc=reservation.start_at_utc,
                    end_at_utc=reservation.end_at_utc,
                    group_id=group_id,
                )
                for reservation in sorted(
                    rows, key=lambda value: value.item_name.casefold()
                )
            ),
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
        expired_ids = [
            reservation.reservation_id
            for reservation in reservations
            if reservation.end_at_utc <= now
        ]
        if expired_ids:
            self.repository.delete_reservations(reservation_ids=expired_ids)
        return [
            reservation
            for reservation in reservations
            if reservation.end_at_utc > now
        ]

    def _reconcile_locked(self, now: datetime) -> None:
        reservations = self._remove_expired(now)
        items = self.repository.list_items()
        updates: list[ScheduledReservation] = []
        clears: list[str] = []
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
                    updates.append(reservation)
            elif item.reservation_end_utc is not None or item.reserved_by:
                clears.append(item.item_name)
        if updates:
            self.repository.update_item_reservations(updates)
        if clears:
            self.repository.clear_item_reservations(item_names=clears)

    @classmethod
    def _items_by_name(
        cls, item_names: tuple[str, ...], items: list[Item]
    ) -> tuple[Item, ...]:
        if not item_names:
            raise ParseError("Choose at least one inventory item.")
        if len(item_names) > 10:
            raise ParseError("Choose no more than 10 inventory items.")
        normalized_names = [_normalized(name) for name in item_names]
        if len(set(normalized_names)) != len(normalized_names):
            raise ParseError("Choose each inventory item only once.")
        return tuple(cls._item_by_name(name, items) for name in item_names)

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
