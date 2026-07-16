"""Pure Block Kit builders and modal input parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .errors import ParseError
from .models import Item, PendingReservation

RESERVATION_MODAL_CALLBACK = "reservation_modal"
OPEN_RESERVATION_ACTION = "open_reservation_modal"
ITEM_BLOCK = "reservation_item"
ITEM_ACTION = "reservation_item_select"
START_BLOCK = "reservation_start"
START_ACTION = "reservation_start_select"
END_BLOCK = "reservation_end"
END_ACTION = "reservation_end_select"


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
    text = "Open the reservation form to choose items and reservation timing."
    return text, [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Reserve items*\nChoose up to 10 items, whether to start now, and the end time.",
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
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    initial_start = current.astimezone(UTC).replace(second=0, microsecond=0)
    initial_end = initial_start + timedelta(hours=1)
    metadata = json.loads(destination.to_json())
    metadata["default_start"] = int(initial_start.timestamp())
    return {
        "type": "modal",
        "callback_id": RESERVATION_MODAL_CALLBACK,
        "private_metadata": json.dumps(metadata, separators=(",", ":")),
        "title": {"type": "plain_text", "text": "Reserve items"},
        "submit": {"type": "plain_text", "text": "Reserve"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": ITEM_BLOCK,
                "label": {"type": "plain_text", "text": "Items"},
                "element": {
                    "type": "multi_external_select",
                    "action_id": ITEM_ACTION,
                    "min_query_length": 0,
                    "max_selected_items": 10,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Choose up to 10 items",
                    },
                },
            },
            {
                "type": "input",
                "block_id": START_BLOCK,
                "label": {"type": "plain_text", "text": "Start"},
                "element": {
                    "type": "datetimepicker",
                    "action_id": START_ACTION,
                    "initial_date_time": int(initial_start.timestamp()),
                },
            },
            {
                "type": "input",
                "block_id": END_BLOCK,
                "label": {"type": "plain_text", "text": "End"},
                "element": {
                    "type": "datetimepicker",
                    "action_id": END_ACTION,
                    "initial_date_time": int(initial_end.timestamp()),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Slack displays picker values in your local timezone. "
                            f"Confirmations and sheet times use *{settings.timezone_name}*."
                        ),
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
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current_utc = current.astimezone(UTC)
    try:
        selected_options = values[ITEM_BLOCK][ITEM_ACTION]["selected_options"]
        item_names = tuple(str(option["value"]) for option in selected_options)
    except (KeyError, TypeError):
        raise ModalInputError(ITEM_BLOCK, "Choose at least one inventory item.") from None
    if not item_names:
        raise ModalInputError(ITEM_BLOCK, "Choose at least one inventory item.")
    if len(item_names) > 10:
        raise ModalInputError(ITEM_BLOCK, "Choose no more than 10 inventory items.")
    if len({_normalized_item_name(name) for name in item_names}) != len(item_names):
        raise ModalInputError(ITEM_BLOCK, "Choose each inventory item only once.")

    start_utc, start_timestamp = _parse_datetimepicker(
        values,
        block_id=START_BLOCK,
        action_id=START_ACTION,
        label="start",
    )
    end_utc, _ = _parse_datetimepicker(
        values,
        block_id=END_BLOCK,
        action_id=END_ACTION,
        label="end",
    )

    if start_timestamp == _default_start_timestamp(view):
        start_utc = current_utc
    elif start_utc < current_utc:
        raise ModalInputError(START_BLOCK, "Reservation start cannot be in the past.")
    if end_utc <= current_utc:
        raise ModalInputError(END_BLOCK, "Reservation end must be in the future.")
    if end_utc <= start_utc:
        raise ModalInputError(END_BLOCK, "Reservation end must be after its start.")

    return PendingReservation(
        item_names=item_names,
        start_at_utc=start_utc,
        end_at_utc=end_utc,
        requester_user_id=requester_user_id,
    )


def _normalized_item_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _parse_datetimepicker(
    values: dict[str, Any],
    *,
    block_id: str,
    action_id: str,
    label: str,
) -> tuple[datetime, int]:
    try:
        timestamp = int(values[block_id][action_id]["selected_date_time"])
        selected = datetime.fromtimestamp(timestamp, tz=UTC)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        raise ModalInputError(block_id, f"Choose a valid reservation {label}.") from None
    return selected, timestamp


def _default_start_timestamp(view: dict[str, Any]) -> int | None:
    try:
        metadata = json.loads(str(view.get("private_metadata", "")) or "{}")
        return int(metadata["default_start"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
