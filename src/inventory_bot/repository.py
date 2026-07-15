"""Storage interface used by the reservation service."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import Item, ScheduledReservation


class InventoryRepository(Protocol):
    def list_items(self) -> list[Item]: ...

    def list_reservations(self) -> list[ScheduledReservation]: ...

    def add_reservation(self, reservation: ScheduledReservation) -> None: ...

    def delete_reservation(self, *, reservation_id: str) -> None: ...

    def update_item_reservation(
        self,
        *,
        item_name: str,
        reservation_end_utc: datetime,
        reserved_by: str,
    ) -> None: ...

    def clear_item_reservation(self, *, item_name: str) -> None: ...
