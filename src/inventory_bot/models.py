"""Core inventory and reservation models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from .errors import ParseError


@dataclass(frozen=True, slots=True)
class Item:
    item_name: str
    location: str
    reservation_end_utc: datetime | None = None
    reserved_by: str = ""


@dataclass(frozen=True, slots=True)
class Reservation:
    item_name: str
    location: str
    end_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ParsedReservation:
    item_query: str
    end_at_utc: datetime


@dataclass(frozen=True, slots=True)
class PendingReservation:
    item_name: str
    end_at_utc: datetime
    requester_user_id: str

    def to_action_value(self) -> str:
        value = json.dumps(
            {
                "v": 3,
                "n": self.item_name,
                "e": self.end_at_utc.isoformat(),
                "u": self.requester_user_id,
            },
            separators=(",", ":"),
        )
        if len(value) > 1900:
            raise ParseError("The reservation confirmation is too large for Slack.")
        return value

    @classmethod
    def from_action_value(cls, value: str) -> "PendingReservation":
        try:
            payload = json.loads(value)
            version = payload.get("v")
            if version not in {1, 2, 3}:
                raise ValueError("unsupported version")
            end_at = datetime.fromisoformat(payload["e"].replace("Z", "+00:00"))
            if end_at.tzinfo is None:
                raise ValueError("end time is missing a timezone")
            item_name = payload["i"] if version == 1 else payload["n"]
            return cls(
                item_name=str(item_name),
                end_at_utc=end_at,
                requester_user_id=str(payload["u"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ParseError("This confirmation is invalid or has expired. Please try again.") from exc


@dataclass(frozen=True, slots=True)
class PreparedReservation:
    item: Item
    pending: PendingReservation


@dataclass(frozen=True, slots=True)
class InventoryAvailability:
    item: Item
    available: bool
