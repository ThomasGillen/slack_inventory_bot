"""Pure Block Kit builders and modal input parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .config import Settings
from .errors import ParseError
from .models import Item, PendingReservation

RESERVATION_MODAL_CALLBACK = "reservation_modal"
OPEN_RESERVATION_ACTION = "open_reservation_modal"
ITEM_BLOCK = "reservation_item"
ITEM_ACTION = "reservation_item_select"
DATE_BLOCK = "reservation_date"
DATE_ACTION = "reservation_date_select"
TIME_BLOCK = "reservation_time"
TIME_ACTION = "reservation_time_select"


@dataclass(frozen=True, slots=True)
class ModalDestination:
    channel_id: str = ""
    thread_ts: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {"channel_id": self.channel_id, "thread_ts": self.thread_ts},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "ModalDestination":
        try:
            payload = json.loads(value or "{}")
        except json.JSONDecodeError:
            return cls()
        return cls(
            channel_id=str(payload.get("channel_id", "")),
            thread_ts=str(payload.get("thread_ts", "")),
        )


class ModalInputError(ParseError):
    def __init__(self, block_id: str, message: str) -> None:
        super().__init__(message)
        self.block_id = block_id


def reservation_launcher_message() -> tuple[str, list[dict[str, Any]]]:
    text = "Open the reservation form to choose an item and end time."
    return text, [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Reserve an item*\nChoose an available item and its reservation end time.",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open reservation form"},
                    "style": "primary",
                    "action_id": OPEN_RESERVATION_ACTION,
                    "value": "open",
                }
            ],
        },
    ]


def build_reservation_modal(
    settings: Settings,
    *,
    destination: ModalDestination = ModalDestination(),
    now: datetime | None = None,
) -> dict[str, Any]:
    local_now = (now or datetime.now(tz=UTC)).astimezone(settings.timezone)
    initial_date = (local_now.date() + timedelta(days=1)).isoformat()
    return {
        "type": "modal",
        "callback_id": RESERVATION_MODAL_CALLBACK,
        "private_metadata": destination.to_json(),
        "title": {"type": "plain_text", "text": "Reserve an item"},
        "submit": {"type": "plain_text", "text": "Reserve"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": ITEM_BLOCK,
                "label": {"type": "plain_text", "text": "Item"},
                "element": {
                    "type": "external_select",
                    "action_id": ITEM_ACTION,
                    "min_query_length": 0,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Choose or search available items",
                    },
                },
            },
            {
                "type": "input",
                "block_id": DATE_BLOCK,
                "label": {"type": "plain_text", "text": "Reservation end date"},
                "element": {
                    "type": "datepicker",
                    "action_id": DATE_ACTION,
                    "initial_date": initial_date,
                },
            },
            {
                "type": "input",
                "block_id": TIME_BLOCK,
                "label": {"type": "plain_text", "text": "Reservation end time"},
                "element": {
                    "type": "timepicker",
                    "action_id": TIME_ACTION,
                    "initial_time": "17:00",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Date and time are interpreted in *{settings.timezone_name}*.",
                    }
                ],
            },
        ],
    }


def item_options(items: list[Item]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for item in items[:100]:
        label = item.item_name
        if item.location:
            label = f"{label} — {item.location}"
        options.append(
            {
                "text": {"type": "plain_text", "text": label[:75]},
                "value": item.item_name,
            }
        )
    return options


def parse_modal_submission(
    view: dict[str, Any],
    *,
    settings: Settings,
    requester_user_id: str,
    now: datetime | None = None,
) -> PendingReservation:
    values = view.get("state", {}).get("values", {})
    try:
        item_name = values[ITEM_BLOCK][ITEM_ACTION]["selected_option"]["value"]
    except (KeyError, TypeError):
        raise ModalInputError(ITEM_BLOCK, "Choose an inventory item.") from None
    try:
        selected_date = values[DATE_BLOCK][DATE_ACTION]["selected_date"]
        end_date = date.fromisoformat(selected_date)
    except (KeyError, TypeError, ValueError):
        raise ModalInputError(DATE_BLOCK, "Choose a valid end date.") from None
    try:
        selected_time = values[TIME_BLOCK][TIME_ACTION]["selected_time"]
        end_time = time.fromisoformat(selected_time)
    except (KeyError, TypeError, ValueError):
        raise ModalInputError(TIME_BLOCK, "Choose a valid end time.") from None

    end_local = datetime.combine(end_date, end_time, tzinfo=settings.timezone)
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if end_local.astimezone(UTC) <= current.astimezone(UTC):
        raise ModalInputError(TIME_BLOCK, "Reservation end must be in the future.")

    return PendingReservation(
        item_name=str(item_name),
        end_at_utc=end_local.astimezone(UTC),
        requester_user_id=requester_user_id,
    )
