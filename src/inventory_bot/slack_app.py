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
    CancellationResult,
    InventoryAvailability,
    ReservationGroup,
)
from .repository import InventoryRepository
from .reservation_queue import (
    EnqueueResult,
    QueueDestination,
    QueuedReservationRequest,
    ReservationQueueWorker,
    ReservationRequestQueue,
)
from .service import ReservationService
from .sheets import GoogleSheetsRepository
from .slack_views import (
    CANCELLATION_GROUP_BLOCK,
    CANCELLATION_GROUP_MODAL_CALLBACK,
    CANCELLATION_ITEMS_MODAL_CALLBACK,
    CANCEL_ENTIRE_GROUP_ACTION,
    ITEM_ACTION,
    MANAGE_RESERVATION_ACTION,
    OPEN_CANCELLATION_ACTION,
    OPEN_RESERVATION_ACTION,
    RESERVATION_MODAL_CALLBACK,
    CancellationModalMetadata,
    ModalDestination,
    ModalInputError,
    build_cancellation_group_modal,
    build_cancellation_items_modal,
    build_reservation_modal,
    cancellation_complete_view,
    cancellation_launcher_message,
    item_options,
    parse_cancellation_group_submission,
    parse_cancellation_items_submission,
    parse_modal_submission,
    reservation_launcher_message,
)

LOGGER = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    *,
    repository: InventoryRepository | None = None,
    reservation_queue: ReservationRequestQueue | None = None,
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
    service = ReservationService(
        repo,
        timezone=settings.timezone,
        inventory_cache_ttl_seconds=settings.item_picker_cache_seconds,
    )
    request_queue = reservation_queue or ReservationRequestQueue(
        settings.reservation_queue_database
    )
    cancellation_group_cache: dict[tuple[str, str], ReservationGroup] = {}
    app = App(
        token=settings.slack_bot_token,
        token_verification_enabled=token_verification_enabled,
    )

    def notify_queued_outcome(request: QueuedReservationRequest) -> None:
        if request.status == "completed" and request.result is not None:
            text_response, blocks = committed_message(request.result, settings)
        else:
            text_response = (
                ":warning: Reservation not recorded: "
                f"{request.last_error or 'The request could not be completed.'}"
            )
            blocks = None
        _deliver_queue_result(
            app.client,
            request=request,
            text=text_response,
            blocks=blocks,
        )

    queue_worker = ReservationQueueWorker(
        request_queue,
        service,
        notify_queued_outcome,
        requests_per_minute=settings.reservation_queue_rate_per_minute,
        max_attempts=settings.reservation_queue_max_attempts,
        retention_days=settings.reservation_queue_retention_days,
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
        command_key = command_without_mention.casefold()
        thread_ts = None if channel_id.startswith("D") else event.get("thread_ts") or event.get("ts")

        if command_key == "reserve":
            text_response, blocks = reservation_launcher_message()
            say(text=text_response, blocks=blocks, thread_ts=thread_ts)
            return

        if command_key in {"help", ""}:
            say(text=help_text(), thread_ts=thread_ts)
            return

        if command_key == "cancel":
            text_response, blocks = cancellation_launcher_message()
            say(text=text_response, blocks=blocks, thread_ts=thread_ts)
            return

        if command_key == "cancel group":
            say(
                text=(
                    ":warning: Use `cancel group <item>`, or send `cancel` to "
                    "choose a reservation in the manager."
                ),
                thread_ts=thread_ts,
            )
            return

        if command_key.startswith("cancel "):
            cancel_query = command_without_mention.split(maxsplit=1)[1]
            whole_group = cancel_query.casefold().startswith("group ")
            item_query = (
                cancel_query.split(maxsplit=1)[1]
                if whole_group and len(cancel_query.split(maxsplit=1)) == 2
                else cancel_query
            )
            try:
                requester_name = _slack_user_name(client, user_id)
                result = service.cancel(
                    item_query,
                    requester_user_id=user_id,
                    requester_name=requester_name,
                    whole_group=whole_group,
                )
                text_response = cancellation_result_message(result, settings)
            except InventoryBotError as exc:
                text_response = f":warning: {exc}"
            except Exception:
                LOGGER.exception("Unexpected error while cancelling a reservation")
                text_response = (
                    ":warning: I couldn't cancel that reservation right now. Please try again."
                )
            say(text=text_response, thread_ts=thread_ts)
            return

        if command_key == "status" or command_key.startswith("status "):
            parts = command_without_mention.split(maxsplit=1)
            query = parts[1] if len(parts) == 2 else ""
            try:
                statuses = service.inventory_status(query)
                text_response = status_text(statuses, settings)
            except InventoryBotError as exc:
                text_response = f":warning: {exc}"
            say(text=text_response, thread_ts=thread_ts)
            return

        say(
            text="I didn't recognize that command.\n\n" + help_text(),
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

    def open_cancellation_modal(
        body: dict[str, Any],
        client: Any,
        *,
        group_id: str = "",
    ) -> None:
        user_id = str(body.get("user", {}).get("id", ""))
        destination = _modal_destination_from_body(body)
        if not settings.user_allowed(user_id) or (
            destination.channel_id
            and not settings.channel_allowed(destination.channel_id)
        ):
            return
        groups = service.reservation_groups_for_user(
            requester_user_id=user_id,
            requester_name=_slack_user_name(client, user_id),
        )
        for key in [key for key in cancellation_group_cache if key[0] == user_id]:
            cancellation_group_cache.pop(key, None)
        for group in groups:
            cancellation_group_cache[(user_id, group.group_id)] = group
        if group_id:
            selected = next(
                (group for group in groups if group.group_id == group_id), None
            )
            if selected is None:
                _notify_actor(
                    client,
                    channel_id=destination.channel_id or user_id,
                    user_id=user_id,
                    text=(
                        "Only the reservation owner can manage it, or it has "
                        "already ended."
                    ),
                )
                return
            view = build_cancellation_items_modal(
                selected,
                settings,
                destination=destination,
            )
        else:
            view = build_cancellation_group_modal(
                groups,
                settings,
                destination=destination,
            )
        client.views_open(trigger_id=body["trigger_id"], view=view)

    @app.action(OPEN_CANCELLATION_ACTION)
    def open_cancellation_from_button(
        ack: Any, body: dict[str, Any], client: Any
    ) -> None:
        ack()
        try:
            open_cancellation_modal(body, client)
        except Exception:
            LOGGER.exception("Unable to open the cancellation modal")

    @app.action(MANAGE_RESERVATION_ACTION)
    def manage_confirmed_reservation(
        ack: Any, body: dict[str, Any], client: Any
    ) -> None:
        ack()
        try:
            group_id = str(body.get("actions", [{}])[0].get("value", ""))
            open_cancellation_modal(body, client, group_id=group_id)
        except Exception:
            LOGGER.exception("Unable to manage the confirmed reservation")

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
            queued = request_queue.enqueue(
                pending,
                reserved_by_name=_slack_user_name(client, user_id),
                dedupe_key=f"modal:{user_id}:{view.get('id', '')}",
                destination=QueueDestination(
                    channel_id=destination.channel_id or user_id,
                    thread_ts=destination.thread_ts,
                ),
            )
            if queued.created:
                text_response, blocks = queued_message(queued, settings)
                _post_modal_result(
                    client,
                    destination=destination,
                    user_id=user_id,
                    text=text_response,
                    blocks=blocks,
                )
        except Exception:
            LOGGER.exception("Unable to queue a modal reservation")
            _post_modal_result(
                client,
                destination=destination,
                user_id=user_id,
                text=":warning: I couldn't safely queue this request. Please try again.",
            )

    @app.view(CANCELLATION_GROUP_MODAL_CALLBACK)
    def choose_cancellation_group(
        ack: Any, body: dict[str, Any], client: Any
    ) -> None:
        user_id = str(body.get("user", {}).get("id", ""))
        view = body.get("view", {})
        try:
            group_id = parse_cancellation_group_submission(view)
            selected = cancellation_group_cache.get((user_id, group_id))
            if selected is None:
                raise ModalInputError(
                    CANCELLATION_GROUP_BLOCK,
                    "That reservation no longer exists or has already ended.",
                )
        except ModalInputError as exc:
            ack(response_action="errors", errors={exc.block_id: str(exc)})
            return

        metadata = CancellationModalMetadata.from_json(
            str(view.get("private_metadata", ""))
        )
        ack(
            response_action="update",
            view=build_cancellation_items_modal(
                selected,
                settings,
                destination=metadata.destination,
            ),
        )

    @app.view(CANCELLATION_ITEMS_MODAL_CALLBACK)
    def cancel_selected_items(
        ack: Any, body: dict[str, Any], client: Any
    ) -> None:
        user_id = str(body.get("user", {}).get("id", ""))
        view = body.get("view", {})
        try:
            reservation_ids = parse_cancellation_items_submission(view)
        except ModalInputError as exc:
            ack(response_action="errors", errors={exc.block_id: str(exc)})
            return

        ack()
        metadata = CancellationModalMetadata.from_json(
            str(view.get("private_metadata", ""))
        )
        try:
            result = service.cancel_selected(
                metadata.group_id,
                reservation_ids,
                requester_user_id=user_id,
                requester_name=_slack_user_name(client, user_id),
            )
            cancellation_group_cache.pop((user_id, metadata.group_id), None)
            text_response = cancellation_result_message(result, settings)
        except InventoryBotError as exc:
            text_response = f":warning: Cancellation not completed: {exc}"
        except Exception:
            LOGGER.exception("Unexpected error while cancelling selected items")
            text_response = (
                ":warning: I couldn't cancel those items right now. Please try again."
            )
        _post_modal_result(
            client,
            destination=metadata.destination,
            user_id=user_id,
            text=text_response,
        )

    @app.action(CANCEL_ENTIRE_GROUP_ACTION)
    def cancel_entire_group(
        ack: Any, body: dict[str, Any], client: Any
    ) -> None:
        ack()
        user_id = str(body.get("user", {}).get("id", ""))
        view = body.get("view", {})
        metadata = CancellationModalMetadata.from_json(
            str(view.get("private_metadata", ""))
        )
        try:
            result = service.cancel_group(
                metadata.group_id,
                requester_user_id=user_id,
                requester_name=_slack_user_name(client, user_id),
            )
            cancellation_group_cache.pop((user_id, metadata.group_id), None)
            text_response = cancellation_result_message(result, settings)
            modal_text = text_response
        except InventoryBotError as exc:
            text_response = f":warning: Cancellation not completed: {exc}"
            modal_text = text_response
        except Exception:
            LOGGER.exception("Unexpected error while cancelling a reservation group")
            text_response = (
                ":warning: I couldn't cancel that reservation right now. Please try again."
            )
            modal_text = text_response

        try:
            client.views_update(
                view_id=str(view.get("id", "")),
                hash=str(view.get("hash", "")),
                view=cancellation_complete_view(modal_text),
            )
        except Exception:
            LOGGER.exception("Unable to update the cancellation modal result")
        _post_modal_result(
            client,
            destination=metadata.destination,
            user_id=user_id,
            text=text_response,
        )

    setattr(app, "_inventory_service", service)
    setattr(app, "_reservation_request_queue", request_queue)
    setattr(app, "_reservation_queue_worker", queue_worker)
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


def queued_message(
    queued: EnqueueResult, settings: Settings
) -> tuple[str, list[dict[str, Any]]]:
    pending = queued.request.pending
    item_names = ", ".join(pending.item_names)
    request_id = queued.request.request_id[:8]
    if queued.position <= 1:
        position_text = "It is next to be processed."
    else:
        estimated_minutes = (
            (queued.position - 1) / settings.reservation_queue_rate_per_minute
        )
        estimate_text = (
            "under a minute"
            if estimated_minutes < 1
            else f"about {max(1, round(estimated_minutes))} minute(s)"
        )
        position_text = (
            f"It is position {queued.position} in the queue; the estimated "
            f"wait is {estimate_text}."
        )
    text = (
        f"Reservation request queued for {item_names}. {position_text} "
        f"Request ID: {request_id}."
    )
    return text, [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":hourglass_flowing_sand: *Reservation request queued*\n"
                    f"*Items:* {_escape_mrkdwn(item_names)}\n"
                    f"{_escape_mrkdwn(position_text)}\n"
                    "This is not confirmed yet. I'll send the final result after "
                    "checking the latest spreadsheet.\n"
                    f"*Request ID:* `{request_id}`"
                ),
            },
        }
    ]


def _deliver_queue_result(
    client: Any,
    *,
    request: QueuedReservationRequest,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    destination = request.destination
    kwargs: dict[str, Any] = {
        "channel": destination.channel_id,
        "text": text,
    }
    if blocks:
        kwargs["blocks"] = blocks
    if destination.thread_ts and not destination.channel_id.startswith("D"):
        kwargs["thread_ts"] = destination.thread_ts
    client.chat_postMessage(**kwargs)


def _format_end_time(end_at: datetime, settings: Settings) -> str:
    return end_at.astimezone(settings.timezone).strftime("%a, %b %d, %Y at %I:%M %p %Z")


def _escape_mrkdwn(value: str) -> str:
    return escape(value, quote=False)


def committed_message(
    reservation_group: ReservationGroup, settings: Settings
) -> tuple[str, list[dict[str, Any]]]:
    start_text = _format_end_time(reservation_group.start_at_utc, settings)
    end_text = _format_end_time(reservation_group.end_at_utc, settings)
    item_names = ", ".join(reservation_group.item_names)
    item_lines = "\n".join(
        (
            f"• *{_escape_mrkdwn(reservation.item_name)}* — "
            f"{_escape_mrkdwn(reservation.location or 'Location not specified')}"
        )
        for reservation in reservation_group.reservations
    )
    text = (
        f"Reservation confirmed for {item_names} from {start_text} "
        f"until {end_text}."
    )
    return text, [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: *Reservation confirmed*\n"
                    f"*Items ({len(reservation_group.reservations)}):*\n{item_lines}\n"
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
                    "text": {
                        "type": "plain_text",
                        "text": "Manage reservation",
                    },
                    "action_id": MANAGE_RESERVATION_ACTION,
                    "value": reservation_group.group_id,
                }
            ],
        },
    ]


def cancellation_result_message(
    result: CancellationResult, settings: Settings
) -> str:
    cancelled = result.cancelled
    cancelled_names = ", ".join(cancelled.item_names)
    start_text = _format_end_time(cancelled.start_at_utc, settings)
    end_text = _format_end_time(cancelled.end_at_utc, settings)
    message = (
        f":white_check_mark: Cancelled *{_escape_mrkdwn(cancelled_names)}* "
        f"from {_escape_mrkdwn(start_text)} until {_escape_mrkdwn(end_text)}."
    )
    if result.remaining:
        remaining_names = ", ".join(
            reservation.item_name for reservation in result.remaining
        )
        message += (
            f" Remaining in this reservation: "
            f"*{_escape_mrkdwn(remaining_names)}*."
        )
    return message


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
        "*Inventory Bot help*\n"
        "• `reserve` — open the multi-item reservation form\n"
        "• `cancel` — open Manage Reservations and choose items\n"
        "• `cancel <item>` — cancel only that item\n"
        "• `cancel group <item>` — cancel its entire reservation group\n"
        "• `status` — show all inventory availability\n"
        "• `status <item>` — show one item's availability\n"
        "• `help` — show this command overview\n\n"
        "*Reservation process*\n"
        "Send `reserve`, open the form, choose the items and times, then submit. "
        "The request is queued, then checked against the latest sheet. "
        "Wait for the separate confirmed or failed result."
    )


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
