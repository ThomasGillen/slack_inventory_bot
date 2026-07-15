"""Slack Bolt listeners and message formatting."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from html import escape
from typing import Any

from .config import Settings
from .errors import InventoryBotError
from .models import (
    InventoryAvailability,
    PendingReservation,
    PreparedReservation,
    Reservation,
)
from .repository import InventoryRepository
from .service import ReservationService
from .sheets import GoogleSheetsRepository
from .slack_views import (
    ITEM_ACTION,
    OPEN_RESERVATION_ACTION,
    RESERVATION_MODAL_CALLBACK,
    ModalDestination,
    ModalInputError,
    build_reservation_modal,
    item_options,
    parse_modal_submission,
    reservation_launcher_message,
)

LOGGER = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    *,
    repository: InventoryRepository | None = None,
    token_verification_enabled: bool = True,
) -> Any:
    try:
        from slack_bolt import App
    except ImportError as exc:
        raise RuntimeError(
            "Slack Bolt is not installed. Run: python -m pip install -e ."
        ) from exc

    repo = repository or GoogleSheetsRepository(settings)
    if isinstance(repo, GoogleSheetsRepository):
        repo.ensure_schema()
    service = ReservationService(repo, timezone=settings.timezone)
    app = App(
        token=settings.slack_bot_token,
        token_verification_enabled=token_verification_enabled,
    )

    def handle_message(event: dict[str, Any], say: Any, client: Any) -> None:
        if event.get("bot_id") or event.get("subtype") or not event.get("user"):
            return

        user_id = str(event["user"])
        channel_id = str(event.get("channel", ""))
        if not settings.user_allowed(user_id) or not settings.channel_allowed(channel_id):
            say(text="You are not allowed to make inventory reservations here.")
            return

        text = str(event.get("text", "")).strip()
        command = " ".join(text.split())
        command_without_mention = _remove_leading_mentions(command)
        thread_ts = None if channel_id.startswith("D") else event.get("thread_ts") or event.get("ts")

        if command_without_mention.casefold() == "reserve":
            text_response, blocks = reservation_launcher_message()
            say(text=text_response, blocks=blocks, thread_ts=thread_ts)
            return

        if command_without_mention.casefold() in {"help", ""}:
            say(text=help_text(), thread_ts=thread_ts)
            return

        if command_without_mention.casefold() == "cancel":
            say(
                text=":warning: Use `cancel <item>`. Example: `cancel kayak1`.",
                thread_ts=thread_ts,
            )
            return

        if command_without_mention.casefold().startswith("cancel "):
            item_query = command_without_mention.split(maxsplit=1)[1]
            try:
                requester_name = _slack_user_name(client, user_id)
                reservation = service.cancel(
                    item_query,
                    requester_user_id=user_id,
                    requester_name=requester_name,
                )
                text_response = (
                    f":white_check_mark: Reservation cancelled for "
                    f"*{_escape_mrkdwn(reservation.item_name)}* from "
                    f"{_escape_mrkdwn(_format_end_time(reservation.start_at_utc, settings))} "
                    f"until {_escape_mrkdwn(_format_end_time(reservation.end_at_utc, settings))}."
                )
            except InventoryBotError as exc:
                text_response = f":warning: {exc}"
            except Exception:
                LOGGER.exception("Unexpected error while cancelling a reservation")
                text_response = (
                    ":warning: I couldn't cancel that reservation right now. Please try again."
                )
            say(text=text_response, thread_ts=thread_ts)
            return

        if command_without_mention.casefold() in {"status", "availability"} or command_without_mention.casefold().startswith(
            ("status ", "availability ")
        ):
            parts = command_without_mention.split(maxsplit=1)
            query = parts[1] if len(parts) == 2 else ""
            try:
                statuses = service.inventory_status(query)
                text_response = status_text(statuses, settings)
            except InventoryBotError as exc:
                text_response = f":warning: {exc}"
            say(text=text_response, thread_ts=thread_ts)
            return

        try:
            prepared = service.prepare(
                command_without_mention,
                requester_user_id=user_id,
            )
            text_response, blocks = confirmation_message(prepared, settings)
            say(text=text_response, blocks=blocks, thread_ts=thread_ts)
        except InventoryBotError as exc:
            say(text=f":warning: {exc}", thread_ts=thread_ts)
        except Exception:
            LOGGER.exception("Unexpected error while preparing a reservation")
            say(
                text=":warning: I couldn't check inventory right now. Please try again.",
                thread_ts=thread_ts,
            )

    @app.event("app_mention")
    def on_app_mention(
        event: dict[str, Any], say: Any, client: Any
    ) -> None:
        handle_message(event, say, client)

    @app.event("message")
    def on_direct_message(
        event: dict[str, Any], say: Any, client: Any
    ) -> None:
        if event.get("channel_type") == "im" or str(event.get("channel", "")).startswith("D"):
            handle_message(event, say, client)

    def open_reservation_modal(body: dict[str, Any], client: Any) -> None:
        user_id = str(body.get("user", {}).get("id", ""))
        destination = _modal_destination_from_body(body)
        if not settings.user_allowed(user_id) or (
            destination.channel_id
            and not settings.channel_allowed(destination.channel_id)
        ):
            return
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_reservation_modal(settings, destination=destination),
        )

    @app.action(OPEN_RESERVATION_ACTION)
    def open_reservation_from_button(
        ack: Any, body: dict[str, Any], client: Any
    ) -> None:
        ack()
        try:
            open_reservation_modal(body, client)
        except Exception:
            LOGGER.exception("Unable to open the reservation modal from a button")

    @app.shortcut(OPEN_RESERVATION_ACTION)
    def open_reservation_from_shortcut(
        ack: Any, body: dict[str, Any], client: Any
    ) -> None:
        ack()
        try:
            open_reservation_modal(body, client)
        except Exception:
            LOGGER.exception("Unable to open the reservation modal from a shortcut")

    @app.options(ITEM_ACTION)
    def load_available_item_options(ack: Any, payload: dict[str, Any]) -> None:
        user_id = str(payload.get("user", {}).get("id", ""))
        if not settings.user_allowed(user_id):
            ack(options=[])
            return
        try:
            query = str(payload.get("value", ""))
            ack(options=item_options(service.inventory_items(query)))
        except Exception:
            LOGGER.exception("Unable to load available inventory options")
            ack(options=[])

    @app.view(RESERVATION_MODAL_CALLBACK)
    def submit_reservation_modal(
        ack: Any, body: dict[str, Any], client: Any
    ) -> None:
        user_id = str(body.get("user", {}).get("id", ""))
        view = body.get("view", {})
        try:
            pending = parse_modal_submission(
                view,
                settings=settings,
                requester_user_id=user_id,
            )
        except ModalInputError as exc:
            ack(response_action="errors", errors={exc.block_id: str(exc)})
            return

        ack()
        destination = ModalDestination.from_json(str(view.get("private_metadata", "")))
        try:
            reservation = service.commit(
                pending,
                reserved_by_name=_slack_user_name(client, user_id),
            )
            text_response, blocks = committed_message(reservation, settings)
            _post_modal_result(
                client,
                destination=destination,
                user_id=user_id,
                text=text_response,
                blocks=blocks,
            )
        except InventoryBotError as exc:
            _post_modal_result(
                client,
                destination=destination,
                user_id=user_id,
                text=f":warning: Reservation not recorded: {exc}",
            )
        except Exception:
            LOGGER.exception("Unexpected error while committing a modal reservation")
            _post_modal_result(
                client,
                destination=destination,
                user_id=user_id,
                text=":warning: I couldn't update inventory. Please try again.",
            )

    @app.action("confirm_reservation")
    def confirm_reservation(ack: Any, body: dict[str, Any], client: Any) -> None:
        ack()
        actor_id = str(body.get("user", {}).get("id", ""))
        channel_id, message_ts = _action_message_location(body)
        try:
            value = str(body["actions"][0]["value"])
            pending = PendingReservation.from_action_value(value)
            if actor_id != pending.requester_user_id:
                _notify_actor(
                    client,
                    channel_id=channel_id,
                    user_id=actor_id,
                    text="Only the person who requested this reservation can confirm it.",
                )
                return
            reservation = service.commit(
                pending,
                reserved_by_name=_slack_user_name(client, actor_id),
            )
            text_response, blocks = committed_message(reservation, settings)
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=text_response,
                blocks=blocks,
            )
        except InventoryBotError as exc:
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Reservation not recorded: {exc}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":warning: *Reservation not recorded*\n{_escape_mrkdwn(str(exc))}",
                        },
                    }
                ],
            )
        except Exception:
            LOGGER.exception("Unexpected error while committing a reservation")
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="Reservation not recorded because inventory could not be updated.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":warning: *Reservation not recorded*\nPlease try the command again.",
                        },
                    }
                ],
            )

    @app.action("cancel_reservation")
    def cancel_reservation(ack: Any, body: dict[str, Any], client: Any) -> None:
        ack()
        actor_id = str(body.get("user", {}).get("id", ""))
        channel_id, message_ts = _action_message_location(body)
        try:
            pending = PendingReservation.from_action_value(str(body["actions"][0]["value"]))
            if actor_id != pending.requester_user_id:
                _notify_actor(
                    client,
                    channel_id=channel_id,
                    user_id=actor_id,
                    text="Only the person who requested this reservation can cancel it.",
                )
                return
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="Reservation request cancelled.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":x: Reservation request cancelled by <@{actor_id}>.",
                        },
                    }
                ],
            )
        except InventoryBotError as exc:
            _notify_actor(
                client,
                channel_id=channel_id,
                user_id=actor_id,
                text=str(exc),
            )

    setattr(app, "_inventory_service", service)
    return app


def _remove_leading_mentions(text: str) -> str:
    parts = text.split()
    while parts and parts[0].startswith("<@") and parts[0].endswith(">"):
        parts.pop(0)
    return " ".join(parts)


def _slack_user_name(client: Any, user_id: str) -> str:
    """Return a sheet-friendly Slack name, falling back to the stable user ID."""
    try:
        response = client.users_info(user=user_id)
        user = response.get("user", {})
        profile = user.get("profile", {})
        candidates = (
            profile.get("display_name"),
            profile.get("real_name"),
            user.get("real_name"),
            user.get("name"),
        )
        for candidate in candidates:
            cleaned = " ".join(str(candidate or "").split())
            if cleaned:
                return cleaned
    except Exception:
        LOGGER.exception("Unable to look up Slack profile for %s", user_id)
    return user_id


def _format_end_time(end_at: datetime, settings: Settings) -> str:
    return end_at.astimezone(settings.timezone).strftime("%a, %b %d, %Y at %I:%M %p %Z")


def _escape_mrkdwn(value: str) -> str:
    return escape(value, quote=False)


def confirmation_message(
    prepared: PreparedReservation, settings: Settings
) -> tuple[str, list[dict[str, Any]]]:
    item_name = _escape_mrkdwn(prepared.item.item_name)
    location = _escape_mrkdwn(prepared.item.location or "Not specified")
    start_text = (
        _format_end_time(prepared.pending.start_at_utc, settings)
        if prepared.pending.start_at_utc is not None
        else "Immediately after confirmation"
    )
    end_text = _format_end_time(prepared.pending.end_at_utc, settings)
    value = prepared.pending.to_action_value()
    text = (
        f"Confirm reservation for {prepared.item.item_name} from {start_text} "
        f"until {end_text}."
    )
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Confirm reservation*\n"
                    f"*Item:* {item_name}\n"
                    f"*Location:* {location}\n"
                    f"*Starts:* {_escape_mrkdwn(start_text)}\n"
                    f"*Ends:* {_escape_mrkdwn(end_text)}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Confirm"},
                    "style": "primary",
                    "action_id": "confirm_reservation",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": "cancel_reservation",
                    "value": value,
                },
            ],
        },
    ]
    return text, blocks


def committed_message(
    reservation: Reservation, settings: Settings
) -> tuple[str, list[dict[str, Any]]]:
    start_text = _format_end_time(reservation.start_at_utc, settings)
    end_text = _format_end_time(reservation.end_at_utc, settings)
    text = (
        f"Reservation confirmed for {reservation.item_name} from {start_text} "
        f"until {end_text}."
    )
    return text, [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: *Reservation confirmed*\n"
                    f"*Item:* {_escape_mrkdwn(reservation.item_name)}\n"
                    f"*Location:* {_escape_mrkdwn(reservation.location or 'Not specified')}\n"
                    f"*Starts:* {_escape_mrkdwn(start_text)}\n"
                    f"*Ends:* {_escape_mrkdwn(end_text)}"
                ),
            },
        }
    ]


def status_text(
    statuses: list[InventoryAvailability], settings: Settings
) -> str:
    if not statuses:
        return "No inventory items are configured yet."
    visible = statuses[:20]
    lines: list[str] = []
    for status in visible:
        item_name = _escape_mrkdwn(status.item.item_name)
        location = (
            f" — {_escape_mrkdwn(status.item.location)}"
            if status.item.location
            else ""
        )
        if status.available:
            state = "available"
            if status.next_reservation is not None:
                next_start = _format_end_time(
                    status.next_reservation.start_at_utc, settings
                )
                next_end = _format_end_time(
                    status.next_reservation.end_at_utc, settings
                )
                owner = (
                    f" by {_escape_mrkdwn(status.next_reservation.reserved_by)}"
                    if status.next_reservation.reserved_by
                    else ""
                )
                state += (
                    f" now; next reserved from {_escape_mrkdwn(next_start)} "
                    f"until {_escape_mrkdwn(next_end)}{owner}"
                )
        else:
            end_text = _format_end_time(status.item.reservation_end_utc, settings)
            reserved_by = status.item.reserved_by
            if re.fullmatch(r"[UW][A-Z0-9]+", reserved_by):
                owner = f" by <@{reserved_by}>"
            elif reserved_by:
                owner = f" by {_escape_mrkdwn(reserved_by)}"
            else:
                owner = ""
            state = f"reserved until {_escape_mrkdwn(end_text)}{owner}"
        lines.append(f"• {item_name}: {state}{location}")
    if len(statuses) > len(visible):
        lines.append(f"…and {len(statuses) - len(visible)} more items.")
    return "*Inventory availability*\n" + "\n".join(lines)


def help_text() -> str:
    return (
        "*Inventory Bot commands*\n"
        "• `reserve` — open the reservation form\n"
        "• `reserve <item> from <date/time> until <date/time>`\n"
        "• `reserve <item> until <date/time>` — start immediately\n"
        "• `cancel <item>`\n"
        "• `status` or `status <item>`\n\n"
        "Examples:\n"
        "`reserve kayak1 from Friday at 1 PM until Friday at 3 PM`\n"
        "`reserve kayak2 until 2026-07-18 17:00`"
    )


def _action_message_location(body: dict[str, Any]) -> tuple[str, str]:
    channel_id = str(
        body.get("channel", {}).get("id")
        or body.get("container", {}).get("channel_id")
        or ""
    )
    message_ts = str(
        body.get("message", {}).get("ts")
        or body.get("container", {}).get("message_ts")
        or ""
    )
    return channel_id, message_ts


def _notify_actor(client: Any, *, channel_id: str, user_id: str, text: str) -> None:
    if channel_id.startswith("D"):
        client.chat_postMessage(channel=channel_id, text=text)
    else:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)


def _modal_destination_from_body(body: dict[str, Any]) -> ModalDestination:
    channel_id = str(
        body.get("channel", {}).get("id")
        or body.get("container", {}).get("channel_id")
        or ""
    )
    if not channel_id or channel_id.startswith("D"):
        return ModalDestination(channel_id=channel_id)
    message = body.get("message", {})
    thread_ts = str(
        message.get("thread_ts")
        or body.get("container", {}).get("thread_ts")
        or message.get("ts")
        or body.get("container", {}).get("message_ts")
        or ""
    )
    return ModalDestination(channel_id=channel_id, thread_ts=thread_ts)


def _post_modal_result(
    client: Any,
    *,
    destination: ModalDestination,
    user_id: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    channel_id = destination.channel_id or user_id
    kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    if destination.thread_ts and not channel_id.startswith("D"):
        kwargs["thread_ts"] = destination.thread_ts
    client.chat_postMessage(**kwargs)
