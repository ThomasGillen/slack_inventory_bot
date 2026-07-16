"""Storage interface used by the reservation service."""

from __future__ import annotations

from typing import Protocol

from .models import Item, ScheduledReservation


class InventoryRepository(Protocol):
    def list_items(self) -> list[Item]: ...

    def list_reservations(self) -> list[ScheduledReservation]: ...

    def add_reservations(
        self, reservations: list[ScheduledReservation]
    ) -> None: ...

    def delete_reservations(self, *, reservation_ids: list[str]) -> None: ...

    def update_item_reservations(
        self, reservations: list[ScheduledReservation]
    ) -> None: ...

    def clear_item_reservations(self, *, item_names: list[str]) -> None: ...
