"""Reservation rules independent of Slack and Google APIs."""

from __future__ import annotations

import threading
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
            end_at_utc=parsed.end_at_utc,
            requester_user_id=requester_user_id,
        )
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
            if not self._is_available(item, now):
                raise AvailabilityError(self._reserved_message(item))

            self.repository.update_item_reservation(
                item_name=item.item_name,
                reservation_end_utc=pending.end_at_utc.astimezone(UTC),
                reserved_by=" ".join(
                    (reserved_by_name or pending.requester_user_id).split()
                ),
            )
            return Reservation(
                item_name=item.item_name,
                location=item.location,
                end_at_utc=pending.end_at_utc.astimezone(UTC),
            )

    def inventory_status(self, item_query: str = "") -> list[InventoryAvailability]:
        now = self._now()
        items = self.repository.list_items()
        if item_query.strip():
            items = [self._resolve_item(item_query, items)]
        return [
            InventoryAvailability(item=item, available=self._is_available(item, now))
            for item in sorted(items, key=lambda candidate: candidate.item_name.casefold())
        ]

    def available_items(self, query: str = "", *, limit: int = 100) -> list[Item]:
        now = self._now()
        needle = _normalized(query)
        matches = [
            item
            for item in self.repository.list_items()
            if self._is_available(item, now)
            and (not needle or needle in _normalized(item.item_name))
        ]
        return sorted(matches, key=lambda item: item.item_name.casefold())[:limit]

    def cancel(
        self,
        item_query: str,
        *,
        requester_user_id: str,
        requester_name: str = "",
    ) -> Item:
        with self._write_lock:
            now = self._now()
            item = self._resolve_item(item_query, self.repository.list_items())
            if self._is_available(item, now):
                raise CancellationError(
                    f"{item.item_name} does not have an active reservation."
                )
            if not item.reserved_by:
                raise CancellationError(
                    f"{item.item_name} has no Slack owner. Clear its reservation cells in the sheet."
                )
            owners = {_normalized(requester_user_id)}
            if requester_name:
                owners.add(_normalized(requester_name))
            if _normalized(item.reserved_by) not in owners:
                raise CancellationError(
                    f"Only the person who reserved {item.item_name} can cancel it."
                )

            self.repository.clear_item_reservation(item_name=item.item_name)
            return item

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
