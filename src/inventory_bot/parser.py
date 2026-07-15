"""Safe, deterministic parsing for reservation messages."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, tzinfo
from .errors import ParseError
from .models import ParsedReservation

MENTION_RE = re.compile(r"<@[A-Z0-9]+>", re.IGNORECASE)
RESERVATION_RE = re.compile(
    r"^reserve\s+(?P<item>.+?)\s+until\s+(?P<end>.+)$",
    re.IGNORECASE,
)
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def strip_slack_mentions(text: str) -> str:
    return " ".join(MENTION_RE.sub(" ", text).split())


def parse_reservation_message(
    text: str,
    *,
    timezone: tzinfo,
    now: datetime | None = None,
) -> ParsedReservation:
    cleaned = strip_slack_mentions(text)
    match = RESERVATION_RE.fullmatch(cleaned)
    if not match:
        raise ParseError(
            'Use: reserve <item> until <date and time>. '
            'Example: reserve kayak1 until tomorrow at 3 PM'
        )

    item_query = match.group("item").strip().strip('"\'')
    if not item_query or len(item_query) > 200:
        raise ParseError("Item name must be between 1 and 200 characters.")

    current = now or datetime.now(tz=timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    else:
        current = current.astimezone(timezone)

    end_local = parse_end_time(match.group("end"), timezone=timezone, now=current)
    if end_local <= current:
        raise ParseError("Reservation end time must be in the future.")

    return ParsedReservation(
        item_query=item_query,
        end_at_utc=end_local.astimezone(UTC),
    )


def parse_end_time(value: str, *, timezone: tzinfo, now: datetime) -> datetime:
    raw = " ".join(value.strip().replace(",", " ").split())
    lowered = raw.casefold()

    relative = re.fullmatch(r"in\s+(\d+)\s+(minute|minutes|hour|hours|day|days)", lowered)
    if relative:
        count = int(relative.group(1))
        unit = relative.group(2)
        if count < 1:
            raise ParseError("Reservation duration must be positive.")
        if unit.startswith("minute"):
            return now + timedelta(minutes=count)
        if unit.startswith("hour"):
            return now + timedelta(hours=count)
        return now + timedelta(days=count)

    relative_day = re.fullmatch(r"(today|tomorrow)\s+(?:at\s+)?(.+)", lowered)
    if relative_day:
        day_offset = 1 if relative_day.group(1) == "tomorrow" else 0
        parsed_time = _parse_clock_time(relative_day.group(2))
        target_date = (now + timedelta(days=day_offset)).date()
        return datetime.combine(target_date, parsed_time, tzinfo=timezone)

    weekday = re.fullmatch(
        r"(?:next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        r"\s+(?:at\s+)?(.+)",
        lowered,
    )
    if weekday:
        parsed_time = _parse_clock_time(weekday.group(2))
        days_ahead = (WEEKDAYS[weekday.group(1)] - now.weekday()) % 7
        candidate = datetime.combine(
            (now + timedelta(days=days_ahead)).date(), parsed_time, tzinfo=timezone
        )
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    iso_candidate = raw.replace("Z", "+00:00")
    try:
        parsed_iso = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed_iso = None
    if parsed_iso is not None:
        if not re.search(r"\d:\d", raw):
            raise ParseError("Include a specific reservation end time, not only a date.")
        if parsed_iso.tzinfo is None:
            parsed_iso = parsed_iso.replace(tzinfo=timezone)
        return parsed_iso.astimezone(timezone)

    normalized = re.sub(r"\s+at\s+", " ", raw, flags=re.IGNORECASE)
    with_year = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %I %p",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %I %p",
        "%B %d %Y %H:%M",
        "%B %d %Y %I:%M %p",
        "%B %d %Y %I %p",
        "%b %d %Y %H:%M",
        "%b %d %Y %I:%M %p",
        "%b %d %Y %I %p",
    )
    for fmt in with_year:
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone)
        except ValueError:
            continue

    without_year = (
        "%B %d %H:%M",
        "%B %d %I:%M %p",
        "%B %d %I %p",
        "%b %d %H:%M",
        "%b %d %I:%M %p",
        "%b %d %I %p",
    )
    for fmt in without_year:
        try:
            parsed = datetime.strptime(normalized, fmt).replace(
                year=now.year, tzinfo=timezone
            )
        except ValueError:
            continue
        if parsed <= now:
            parsed = parsed.replace(year=now.year + 1)
        return parsed

    raise ParseError(
        "I couldn't understand that end time. Try 'tomorrow at 3 PM' "
        "or '2026-07-18 17:00'."
    )


def _parse_clock_time(value: str) -> time:
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value.strip(), re.I)
    if not match:
        raise ParseError("Include a specific time, such as 3 PM or 15:00.")

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = (match.group(3) or "").casefold()
    if minute > 59:
        raise ParseError("Minutes must be between 00 and 59.")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ParseError("Use an hour from 1 to 12 with AM or PM.")
        if hour == 12:
            hour = 0
        if meridiem == "pm":
            hour += 12
    else:
        if hour > 23:
            raise ParseError("Use a 24-hour time between 00:00 and 23:59.")
        if match.group(2) is None and hour <= 12:
            raise ParseError("Specify AM or PM, or use a 24-hour time such as 15:00.")
    return time(hour=hour, minute=minute)
