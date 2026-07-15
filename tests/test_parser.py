from datetime import UTC, datetime, timedelta, timezone
from unittest import TestCase

from inventory_bot.errors import ParseError
from inventory_bot.parser import parse_reservation_message


class ReservationParserTests(TestCase):
    def setUp(self) -> None:
        self.timezone = timezone(timedelta(hours=-7), name="PDT")
        self.now = datetime(2026, 7, 14, 16, 0, tzinfo=self.timezone)

    def test_parses_mention_item_and_relative_day(self) -> None:
        parsed = parse_reservation_message(
            "<@U123ABC> reserve kayak1 until tomorrow at 3 PM",
            timezone=self.timezone,
            now=self.now,
        )

        self.assertEqual("kayak1", parsed.item_query)
        self.assertEqual(self.now.astimezone(UTC), parsed.start_at_utc)
        self.assertEqual(datetime(2026, 7, 15, 22, 0, tzinfo=UTC), parsed.end_at_utc)

    def test_parses_future_start_and_end(self) -> None:
        parsed = parse_reservation_message(
            "reserve kayak1 from tomorrow at 1 PM until tomorrow at 3 PM",
            timezone=self.timezone,
            now=self.now,
        )

        self.assertEqual(datetime(2026, 7, 15, 20, 0, tzinfo=UTC), parsed.start_at_utc)
        self.assertEqual(datetime(2026, 7, 15, 22, 0, tzinfo=UTC), parsed.end_at_utc)

    def test_rejects_end_before_start(self) -> None:
        with self.assertRaisesRegex(ParseError, "after its start"):
            parse_reservation_message(
                "reserve kayak1 from tomorrow at 3 PM until tomorrow at 1 PM",
                timezone=self.timezone,
                now=self.now,
            )

    def test_parses_upcoming_weekday(self) -> None:
        parsed = parse_reservation_message(
            "reserve Camera-07 until Friday at 3 PM",
            timezone=self.timezone,
            now=self.now,
        )

        self.assertEqual(datetime(2026, 7, 17, 22, 0, tzinfo=UTC), parsed.end_at_utc)

    def test_parses_iso_local_time(self) -> None:
        parsed = parse_reservation_message(
            "reserve Camera-07 until 2026-07-18 17:00",
            timezone=self.timezone,
            now=self.now,
        )

        self.assertEqual(datetime(2026, 7, 19, 0, 0, tzinfo=UTC), parsed.end_at_utc)

    def test_does_not_treat_numbered_item_name_as_quantity(self) -> None:
        parsed = parse_reservation_message(
            "reserve 2-way radio until in 2 hours",
            timezone=self.timezone,
            now=self.now,
        )

        self.assertEqual("2-way radio", parsed.item_query)

    def test_rejects_past_end_time(self) -> None:
        with self.assertRaisesRegex(ParseError, "future"):
            parse_reservation_message(
                "reserve Camera-07 until today at 3 PM",
                timezone=self.timezone,
                now=self.now,
            )

    def test_rejects_date_without_time(self) -> None:
        with self.assertRaisesRegex(ParseError, "specific reservation end time"):
            parse_reservation_message(
                "reserve Camera-07 until 2026-07-18",
                timezone=self.timezone,
                now=self.now,
            )

    def test_rejects_ambiguous_clock_hour(self) -> None:
        with self.assertRaisesRegex(ParseError, "AM or PM"):
            parse_reservation_message(
                "reserve Camera-07 until tomorrow at 3",
                timezone=self.timezone,
                now=self.now,
            )

    def test_rejects_unstructured_input_with_usage(self) -> None:
        with self.assertRaisesRegex(ParseError, "Use: reserve"):
            parse_reservation_message(
                "I want the camera tomorrow",
                timezone=self.timezone,
                now=self.now,
            )
