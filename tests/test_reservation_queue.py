from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from unittest import TestCase

from inventory_bot.errors import AvailabilityError
from inventory_bot.models import PendingReservation, Reservation, ReservationGroup
from inventory_bot.reservation_queue import (
    QueueDestination,
    ReservationQueueWorker,
    ReservationRequestQueue,
)


class FakeReservationService:
    def __init__(self, *, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.commits: list[tuple[PendingReservation, str, str]] = []

    def commit(
        self,
        pending: PendingReservation,
        *,
        reserved_by_name: str,
        group_id: str,
    ) -> ReservationGroup:
        self.commits.append((pending, reserved_by_name, group_id))
        if self.failures:
            raise self.failures.pop(0)
        start_at = pending.start_at_utc or datetime.now(tz=UTC)
        return ReservationGroup(
            group_id=group_id,
            reservations=(
                Reservation(
                    reservation_id="reservation-1",
                    item_name=pending.item_names[0],
                    location="A",
                    start_at_utc=start_at,
                    end_at_utc=pending.end_at_utc,
                    group_id=group_id,
                ),
            ),
        )


class ReservationRequestQueueTests(TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database_path = f"{temporary_directory.name}/queue.sqlite3"
        self.queue = ReservationRequestQueue(self.database_path)
        self.now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    def pending(self, item_name: str = "kayak1") -> PendingReservation:
        return PendingReservation(
            item_names=(item_name,),
            start_at_utc=self.now + timedelta(hours=1),
            end_at_utc=self.now + timedelta(hours=2),
            requester_user_id="U123",
        )

    def enqueue(
        self,
        *,
        dedupe_key: str = "slack-event-1",
        item_name: str = "kayak1",
        submitted_at: datetime | None = None,
    ):
        return self.queue.enqueue(
            self.pending(item_name),
            dedupe_key=dedupe_key,
            reserved_by_name="Taylor Smith",
            destination=QueueDestination("D123"),
            submitted_at_utc=submitted_at or self.now,
        )

    def test_queue_is_durable_and_deduplicates_slack_retries(self) -> None:
        first = self.enqueue()
        duplicate = self.enqueue(item_name="kayak2")

        reopened = ReservationRequestQueue(self.database_path)
        stored = reopened.get(first.request.request_id)

        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(first.request.request_id, duplicate.request.request_id)
        self.assertEqual(("kayak1",), stored.pending.item_names)
        self.assertEqual(1, first.position)

    def test_fifo_order_is_stable_for_identical_submission_times(self) -> None:
        first = self.enqueue(dedupe_key="first", item_name="kayak1")
        second = self.enqueue(dedupe_key="second", item_name="kayak2")

        first_claim = self.queue.claim_next(now=self.now)
        second_claim = self.queue.claim_next(now=self.now)

        self.assertEqual(first.request.request_id, first_claim.request_id)
        self.assertEqual(second.request.request_id, second_claim.request_id)
        self.assertEqual(2, second.position)

    def test_interrupted_processing_is_recovered_after_restart(self) -> None:
        queued = self.enqueue()
        first_claim = self.queue.claim_next(now=self.now)
        self.assertEqual(queued.request.request_id, first_claim.request_id)

        reopened = ReservationRequestQueue(self.database_path)
        reopened.recover_interrupted(now=self.now + timedelta(seconds=1))
        recovered = reopened.claim_next(now=self.now + timedelta(seconds=1))

        self.assertEqual(queued.request.request_id, recovered.request_id)
        self.assertEqual(2, recovered.attempts)

    def test_retry_delay_does_not_let_a_later_request_skip_the_line(self) -> None:
        first = self.enqueue(dedupe_key="first")
        self.enqueue(dedupe_key="second", item_name="kayak2")
        claimed = self.queue.claim_next(now=self.now)
        self.queue.mark_retry(
            claimed.request_id,
            next_attempt_at_utc=self.now + timedelta(minutes=1),
            error="temporary outage",
        )

        before_retry = self.queue.claim_next(
            now=self.now + timedelta(seconds=30)
        )
        after_retry = self.queue.claim_next(
            now=self.now + timedelta(minutes=1)
        )

        self.assertIsNone(before_retry)
        self.assertEqual(first.request.request_id, after_retry.request_id)

    def test_worker_commits_with_request_id_and_persists_notification(self) -> None:
        queued = self.enqueue()
        service = FakeReservationService()
        notified = []
        worker = ReservationQueueWorker(
            self.queue,
            service,
            notified.append,
            requests_per_minute=60,
        )

        self.assertTrue(worker.process_ready_request(now=self.now))
        stored = self.queue.get(queued.request.request_id)
        self.assertEqual("completed", stored.status)
        self.assertEqual(queued.request.request_id, stored.result.group_id)
        self.assertEqual(queued.request.request_id, service.commits[0][2])

        self.assertTrue(
            worker.deliver_ready_notification(
                now=datetime.now(tz=UTC) + timedelta(seconds=1)
            )
        )
        self.assertEqual(queued.request.request_id, notified[0].request_id)

    def test_transient_failure_retries_and_expected_conflict_does_not(self) -> None:
        retry_request = self.enqueue(dedupe_key="retry")
        retry_service = FakeReservationService(
            failures=[RuntimeError("temporary Sheets outage")]
        )
        retry_worker = ReservationQueueWorker(
            self.queue,
            retry_service,
            lambda request: None,
            requests_per_minute=60,
            retry_base_seconds=0,
        )

        with self.assertLogs(
            "inventory_bot.reservation_queue", level="ERROR"
        ):
            retry_worker.process_ready_request(now=self.now)
        self.assertEqual(
            "retry", self.queue.get(retry_request.request.request_id).status
        )
        retry_worker.process_ready_request(now=self.now)
        self.assertEqual(
            "completed", self.queue.get(retry_request.request.request_id).status
        )

        conflict_request = self.enqueue(dedupe_key="conflict", item_name="kayak2")
        conflict_service = FakeReservationService(
            failures=[AvailabilityError("kayak2 overlaps another reservation")]
        )
        conflict_worker = ReservationQueueWorker(
            self.queue,
            conflict_service,
            lambda request: None,
            requests_per_minute=60,
        )
        conflict_worker.process_ready_request(now=self.now)

        conflict = self.queue.get(conflict_request.request.request_id)
        self.assertEqual("failed", conflict.status)
        self.assertIn("overlaps", conflict.last_error)
        self.assertEqual(1, conflict.attempts)
