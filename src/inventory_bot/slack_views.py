"""Pure Block Kit builders and modal input parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .errors import ParseError
from .models import (
    MAX_RESERVATION_ITEMS,
    Item,
    PendingReservation,
    Reservation,
    ReservationGroup,
)

RESERVATION_MODAL_CALLBACK = "reservation_modal"
OPEN_RESERVATION_ACTION = "open_reservation_modal"
ITEM_BLOCK = "reservation_item"
ITEM_ACTION = "reservation_item_select"
START_BLOCK = "reservation_start"
START_ACTION = "reservation_start_select"
END_BLOCK = "reservation_end"
END_ACTION = "reservation_end_select"
OPEN_CANCELLATION_ACTION = "open_cancellation_modal"
MANAGE_RESERVATION_ACTION = "manage_reservation"
CANCELLATION_GROUP_MODAL_CALLBACK = "cancellation_group_modal"
CANCELLATION_ITEMS_MODAL_CALLBACK = "cancellation_items_modal"
CANCELLATION_GROUP_BLOCK = "cancellation_group"
CANCELLATION_GROUP_ACTION = "cancellation_group_select"
CANCELLATION_ITEMS_BLOCK = "cancellation_items"
CANCELLATION_ITEMS_ACTION = "cancellation_items_select"
CANCEL_ENTIRE_GROUP_ACTION = "cancel_entire_reservation"


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


@dataclass(frozen=True, slots=True)
class CancellationModalMetadata:
    channel_id: str = ""
    thread_ts: str = ""
    group_id: str = ""

    @property
    def destination(self) -> ModalDestination:
        return ModalDestination(self.channel_id, self.thread_ts)

    def to_json(self) -> str:
        return json.dumps(
            {
                "channel_id": self.channel_id,
                "thread_ts": self.thread_ts,
                "group_id": self.group_id,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "CancellationModalMetadata":
        try:
            payload = json.loads(value or "{}")
        except json.JSONDecodeError:
            return cls()
        return cls(
            channel_id=str(payload.get("channel_id", "")),
            thread_ts=str(payload.get("thread_ts", "")),
            group_id=str(payload.get("group_id", "")),
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
                "text": (
                    "*Reserve items*\n"
                    f"Choose up to {MAX_RESERVATION_ITEMS} items and set the "
                    "start and end time."
                ),
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


def cancellation_launcher_message() -> tuple[str, list[dict[str, Any]]]:
    text = "Open Manage Reservations to cancel selected items or an entire reservation."
    return text, [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Manage reservations*\n"
                    "Choose one of your active or upcoming reservations, then "
                    "select only the items you want to cancel."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Manage reservations",
                    },
                    "style": "primary",
                    "action_id": OPEN_CANCELLATION_ACTION,
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
                    "max_selected_items": MAX_RESERVATION_ITEMS,
                    "placeholder": {
                        "type": "plain_text",
                        "text": f"Choose up to {MAX_RESERVATION_ITEMS} items",
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


def build_cancellation_group_modal(
    groups: list[ReservationGroup],
    settings: Settings,
    *,
    destination: ModalDestination = ModalDestination(),
) -> dict[str, Any]:
    metadata = CancellationModalMetadata(
        channel_id=destination.channel_id,
        thread_ts=destination.thread_ts,
    )
    if not groups:
        return {
            "type": "modal",
            "callback_id": CANCELLATION_GROUP_MODAL_CALLBACK,
            "private_metadata": metadata.to_json(),
            "title": {"type": "plain_text", "text": "Manage reservations"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":information_source: You do not have any active or "
                            "upcoming reservations."
                        ),
                    },
                }
            ],
        }

    options = [_reservation_group_option(group, settings) for group in groups[:100]]
    return {
        "type": "modal",
        "callback_id": CANCELLATION_GROUP_MODAL_CALLBACK,
        "private_metadata": metadata.to_json(),
        "title": {"type": "plain_text", "text": "Manage reservations"},
        "submit": {"type": "plain_text", "text": "Next"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "input",
                "block_id": CANCELLATION_GROUP_BLOCK,
                "label": {
                    "type": "plain_text",
                    "text": "Reservation to manage",
                },
                "element": {
                    "type": "static_select",
                    "action_id": CANCELLATION_GROUP_ACTION,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Choose a reservation",
                    },
                    "options": options,
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Times are shown in *{settings.timezone_name}*.",
                    }
                ],
            },
        ],
    }


def build_cancellation_items_modal(
    group: ReservationGroup,
    settings: Settings,
    *,
    destination: ModalDestination = ModalDestination(),
    initial_reservation_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    metadata = CancellationModalMetadata(
        channel_id=destination.channel_id,
        thread_ts=destination.thread_ts,
        group_id=group.group_id,
    )
    options = [_cancellation_item_option(value) for value in group.reservations]
    initial_ids = set(initial_reservation_ids)
    initial_options = [
        option for option in options if option["value"] in initial_ids
    ]
    selector_element: dict[str, Any] = {
        "type": "multi_static_select",
        "action_id": CANCELLATION_ITEMS_ACTION,
        "options": options,
        "max_selected_items": len(options),
        "placeholder": {
            "type": "plain_text",
            "text": "Choose items to cancel",
        },
    }
    if initial_options:
        selector_element["initial_options"] = initial_options

    start_text = group.start_at_utc.astimezone(settings.timezone).strftime(
        "%a, %b %d at %I:%M %p"
    )
    end_text = group.end_at_utc.astimezone(settings.timezone).strftime(
        "%a, %b %d at %I:%M %p %Z"
    )
    return {
        "type": "modal",
        "callback_id": CANCELLATION_ITEMS_MODAL_CALLBACK,
        "private_metadata": metadata.to_json(),
        "title": {"type": "plain_text", "text": "Cancel items"},
        "submit": {"type": "plain_text", "text": "Cancel selected"},
        "close": {"type": "plain_text", "text": "Keep reservation"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Reservation:* {start_text} until {end_text}\n"
                        "Only selected items will be cancelled."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": CANCELLATION_ITEMS_BLOCK,
                "label": {"type": "plain_text", "text": "Items to cancel"},
                "element": selector_element,
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": CANCEL_ENTIRE_GROUP_ACTION,
                        "value": group.group_id,
                        "style": "danger",
                        "text": {
                            "type": "plain_text",
                            "text": "Cancel entire reservation",
                        },
                        "confirm": {
                            "title": {
                                "type": "plain_text",
                                "text": "Cancel everything?",
                            },
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"This will cancel all {len(group.reservations)} "
                                    "items in this reservation."
                                ),
                            },
                            "confirm": {
                                "type": "plain_text",
                                "text": "Cancel all",
                            },
                            "deny": {
                                "type": "plain_text",
                                "text": "Go back",
                            },
                            "style": "danger",
                        },
                    }
                ],
            },
        ],
    }


def cancellation_complete_view(message: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Reservation updated"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            }
        ],
    }


def parse_cancellation_group_submission(view: dict[str, Any]) -> str:
    values = view.get("state", {}).get("values", {})
    try:
        return str(
            values[CANCELLATION_GROUP_BLOCK][CANCELLATION_GROUP_ACTION][
                "selected_option"
            ]["value"]
        )
    except (KeyError, TypeError):
        raise ModalInputError(
            CANCELLATION_GROUP_BLOCK, "Choose a reservation to manage."
        ) from None


def parse_cancellation_items_submission(view: dict[str, Any]) -> tuple[str, ...]:
    values = view.get("state", {}).get("values", {})
    try:
        selected = values[CANCELLATION_ITEMS_BLOCK][CANCELLATION_ITEMS_ACTION][
            "selected_options"
        ]
        reservation_ids = tuple(str(option["value"]) for option in selected)
    except (KeyError, TypeError):
        raise ModalInputError(
            CANCELLATION_ITEMS_BLOCK, "Choose at least one item to cancel."
        ) from None
    if not reservation_ids:
        raise ModalInputError(
            CANCELLATION_ITEMS_BLOCK, "Choose at least one item to cancel."
        )
    return reservation_ids


def _reservation_group_option(
    group: ReservationGroup, settings: Settings
) -> dict[str, Any]:
    local_start = group.start_at_utc.astimezone(settings.timezone)
    local_end = group.end_at_utc.astimezone(settings.timezone)
    items = ", ".join(group.item_names)
    label = (
        f"{local_start.strftime('%a %b %d, %I:%M %p')}–"
        f"{local_end.strftime('%I:%M %p')} · {items}"
    )
    return {
        "text": {"type": "plain_text", "text": label[:75]},
        "value": group.group_id,
    }


def _cancellation_item_option(reservation: Reservation) -> dict[str, Any]:
    option: dict[str, Any] = {
        "text": {
            "type": "plain_text",
            "text": reservation.item_name[:75],
        },
        "value": reservation.reservation_id,
    }
    if reservation.location:
        option["description"] = {
            "type": "plain_text",
            "text": reservation.location[:75],
        }
    return option


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
    if len(item_names) > MAX_RESERVATION_ITEMS:
        raise ModalInputError(
            ITEM_BLOCK,
            f"Choose no more than {MAX_RESERVATION_ITEMS} inventory items.",
        )
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
