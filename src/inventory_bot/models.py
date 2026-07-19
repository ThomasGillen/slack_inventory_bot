"""Core inventory and reservation models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MAX_RESERVATION_ITEMS = 20


@dataclass(frozen=True, slots=True)
class Item:
    item_name: str
    location: str
    reservation_end_utc: datetime | None = None
    reserved_by: str = ""


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    item_name: str
    location: str
    start_at_utc: datetime
    end_at_utc: datetime
    group_id: str = ""


@dataclass(frozen=True, slots=True)
class ReservationGroup:
    group_id: str
    reservations: tuple[Reservation, ...]

    @property
    def start_at_utc(self) -> datetime:
        return self.reservations[0].start_at_utc

    @property
    def end_at_utc(self) -> datetime:
        return self.reservations[0].end_at_utc

    @property
    def item_names(self) -> tuple[str, ...]:
        return tuple(reservation.item_name for reservation in self.reservations)


@dataclass(frozen=True, slots=True)
class CancellationResult:
    cancelled: ReservationGroup
    remaining: tuple[Reservation, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduledReservation:
    reservation_id: str
    item_name: str
    start_at_utc: datetime
    end_at_utc: datetime
    reserved_by: str
    slack_user_id: str
    group_id: str = ""


@dataclass(frozen=True, slots=True)
class PendingReservation:
    item_names: tuple[str, ...]
    end_at_utc: datetime
    requester_user_id: str
    start_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class InventoryAvailability:
    item: Item
    available: bool
    next_reservation: ScheduledReservation | None = None
