"""Durable reservation request queue backed by the Python standard library."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .errors import InventoryBotError
from .models import PendingReservation, Reservation, ReservationGroup
from .service import ReservationService

LOGGER = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "retry", "processing")
FINISHED_STATUSES = ("completed", "failed")


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Queue timestamps must be timezone-aware.")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Stored queue timestamp is missing its timezone.")
    return parsed.astimezone(UTC)


def _pending_to_json(pending: PendingReservation) -> str:
    return json.dumps(
        {
            "item_names": list(pending.item_names),
            "start_at_utc": (
                pending.start_at_utc.astimezone(UTC).isoformat()
                if pending.start_at_utc is not None
                else None
            ),
            "end_at_utc": pending.end_at_utc.astimezone(UTC).isoformat(),
            "requester_user_id": pending.requester_user_id,
        },
        separators=(",", ":"),
    )


def _pending_from_json(value: str) -> PendingReservation:
    payload = json.loads(value)
    start_at = _parse_timestamp(payload.get("start_at_utc"))
    end_at = _parse_timestamp(payload["end_at_utc"])
    if end_at is None:
        raise ValueError("Stored reservation is missing its end time.")
    return PendingReservation(
        item_names=tuple(str(name) for name in payload["item_names"]),
        start_at_utc=start_at,
        end_at_utc=end_at,
        requester_user_id=str(payload["requester_user_id"]),
    )


def _group_to_json(group: ReservationGroup) -> str:
    return json.dumps(
        {
            "group_id": group.group_id,
            "reservations": [
                {
                    "reservation_id": reservation.reservation_id,
                    "item_name": reservation.item_name,
                    "location": reservation.location,
                    "start_at_utc": _timestamp(reservation.start_at_utc),
                    "end_at_utc": _timestamp(reservation.end_at_utc),
                    "group_id": reservation.group_id,
                }
                for reservation in group.reservations
            ],
        },
        separators=(",", ":"),
    )


def _group_from_json(value: str | None) -> ReservationGroup | None:
    if not value:
        return None
    payload = json.loads(value)
    reservations: list[Reservation] = []
    for stored in payload["reservations"]:
        start_at = _parse_timestamp(stored["start_at_utc"])
        end_at = _parse_timestamp(stored["end_at_utc"])
        if start_at is None or end_at is None:
            raise ValueError("Stored reservation result is missing a timestamp.")
        reservations.append(
            Reservation(
                reservation_id=str(stored["reservation_id"]),
                item_name=str(stored["item_name"]),
                location=str(stored.get("location", "")),
                start_at_utc=start_at,
                end_at_utc=end_at,
                group_id=str(stored.get("group_id", payload["group_id"])),
            )
        )
    return ReservationGroup(
        group_id=str(payload["group_id"]),
        reservations=tuple(reservations),
    )


@dataclass(frozen=True, slots=True)
class QueueDestination:
    """Where the eventual Slack result should be delivered."""

    mode: str
    channel_id: str
    thread_ts: str = ""
    message_ts: str = ""


@dataclass(frozen=True, slots=True)
class QueuedReservationRequest:
    request_id: str
    dedupe_key: str
    pending: PendingReservation
    reserved_by_name: str
    destination: QueueDestination
    status: str
    attempts: int
    notification_attempts: int
    submitted_at_utc: datetime
    next_attempt_at_utc: datetime
    completed_at_utc: datetime | None = None
    result: ReservationGroup | None = None
    last_error: str = ""


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    request: QueuedReservationRequest
    created: bool
    position: int


class ReservationRequestQueue:
    """SQLite persistence for reservation requests and Slack notifications."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reservation_requests (
                    request_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    pending_json TEXT NOT NULL,
                    reserved_by_name TEXT NOT NULL,
                    destination_mode TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL DEFAULT '',
                    message_ts TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    result_json TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    notification_status TEXT NOT NULL DEFAULT 'none',
                    notification_attempts INTEGER NOT NULL DEFAULT 0,
                    notification_next_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS reservation_requests_ready
                ON reservation_requests(status, next_attempt_at, submitted_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS reservation_notifications_ready
                ON reservation_requests(
                    notification_status,
                    notification_next_at,
                    completed_at
                )
                """
            )

    def enqueue(
        self,
        pending: PendingReservation,
        *,
        dedupe_key: str,
        reserved_by_name: str,
        destination: QueueDestination,
        submitted_at_utc: datetime | None = None,
    ) -> EnqueueResult:
        now = (submitted_at_utc or _utc_now()).astimezone(UTC)
        request_id = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM reservation_requests WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            created = existing is None
            if created:
                connection.execute(
                    """
                    INSERT INTO reservation_requests (
                        request_id, dedupe_key, pending_json, reserved_by_name,
                        destination_mode, channel_id, thread_ts, message_ts,
                        status, next_attempt_at, submitted_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        request_id,
                        dedupe_key,
                        _pending_to_json(pending),
                        reserved_by_name,
                        destination.mode,
                        destination.channel_id,
                        destination.thread_ts,
                        destination.message_ts,
                        _timestamp(now),
                        _timestamp(now),
                        _timestamp(now),
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM reservation_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
            if existing is None:
                raise RuntimeError("Unable to load the queued reservation request.")
            position = self._position(connection, existing)
        return EnqueueResult(
            request=self._from_row(existing),
            created=created,
            position=position,
        )

    @staticmethod
    def _position(connection: sqlite3.Connection, row: sqlite3.Row) -> int:
        if row["status"] not in ACTIVE_STATUSES:
            return 0
        result = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM reservation_requests
            WHERE status IN ('pending', 'retry', 'processing')
              AND (
                  submitted_at < ?
                  OR (
                      submitted_at = ?
                      AND rowid <= (
                          SELECT rowid FROM reservation_requests
                          WHERE request_id = ?
                      )
                  )
              )
            """,
            (row["submitted_at"], row["submitted_at"], row["request_id"]),
        ).fetchone()
        return int(result["count"])

    def get(self, request_id: str) -> QueuedReservationRequest | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM reservation_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def claim_next(
        self, *, now: datetime | None = None
    ) -> QueuedReservationRequest | None:
        ready_at = (now or _utc_now()).astimezone(UTC)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM reservation_requests
                WHERE status IN ('pending', 'retry')
                ORDER BY submitted_at, rowid
                LIMIT 1
                """,
            ).fetchone()
            if row is None or row["next_attempt_at"] > _timestamp(ready_at):
                return None
            connection.execute(
                """
                UPDATE reservation_requests
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE request_id = ?
                """,
                (_timestamp(ready_at), row["request_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM reservation_requests WHERE request_id = ?",
                (row["request_id"],),
            ).fetchone()
        return self._from_row(claimed)

    def mark_retry(
        self,
        request_id: str,
        *,
        next_attempt_at_utc: datetime,
        error: str,
    ) -> None:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE reservation_requests
                SET status = 'retry', next_attempt_at = ?, updated_at = ?,
                    last_error = ?
                WHERE request_id = ?
                """,
                (
                    _timestamp(next_attempt_at_utc),
                    _timestamp(now),
                    error,
                    request_id,
                ),
            )

    def mark_completed(
        self, request_id: str, result: ReservationGroup
    ) -> None:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE reservation_requests
                SET status = 'completed', completed_at = ?, updated_at = ?,
                    result_json = ?, last_error = '',
                    notification_status = 'pending', notification_next_at = ?
                WHERE request_id = ?
                """,
                (
                    _timestamp(now),
                    _timestamp(now),
                    _group_to_json(result),
                    _timestamp(now),
                    request_id,
                ),
            )

    def mark_failed(self, request_id: str, *, error: str) -> None:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE reservation_requests
                SET status = 'failed', completed_at = ?, updated_at = ?,
                    last_error = ?, notification_status = 'pending',
                    notification_next_at = ?
                WHERE request_id = ?
                """,
                (
                    _timestamp(now),
                    _timestamp(now),
                    error,
                    _timestamp(now),
                    request_id,
                ),
            )

    def claim_notification(
        self, *, now: datetime | None = None
    ) -> QueuedReservationRequest | None:
        ready_at = (now or _utc_now()).astimezone(UTC)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM reservation_requests
                WHERE status IN ('completed', 'failed')
                  AND notification_status = 'pending'
                  AND notification_next_at <= ?
                ORDER BY completed_at, rowid
                LIMIT 1
                """,
                (_timestamp(ready_at),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE reservation_requests
                SET notification_status = 'sending',
                    notification_attempts = notification_attempts + 1,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (_timestamp(ready_at), row["request_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM reservation_requests WHERE request_id = ?",
                (row["request_id"],),
            ).fetchone()
        return self._from_row(claimed)

    def mark_notification_sent(self, request_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE reservation_requests
                SET notification_status = 'sent', updated_at = ?
                WHERE request_id = ?
                """,
                (_timestamp(_utc_now()), request_id),
            )

    def mark_notification_retry(
        self, request_id: str, *, next_attempt_at_utc: datetime
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE reservation_requests
                SET notification_status = 'pending', notification_next_at = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    _timestamp(next_attempt_at_utc),
                    _timestamp(_utc_now()),
                    request_id,
                ),
            )

    def mark_notification_abandoned(self, request_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE reservation_requests
                SET notification_status = 'abandoned', updated_at = ?
                WHERE request_id = ?
                """,
                (_timestamp(_utc_now()), request_id),
            )

    def recover_interrupted(self, *, now: datetime | None = None) -> None:
        recovered_at = (now or _utc_now()).astimezone(UTC)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE reservation_requests
                SET status = 'pending', next_attempt_at = ?, updated_at = ?
                WHERE status = 'processing'
                """,
                (
                    _timestamp(recovered_at),
                    _timestamp(recovered_at),
                ),
            )
            connection.execute(
                """
                UPDATE reservation_requests
                SET notification_status = 'pending', notification_next_at = ?,
                    updated_at = ?
                WHERE notification_status = 'sending'
                """,
                (
                    _timestamp(recovered_at),
                    _timestamp(recovered_at),
                ),
            )

    def purge_finished_before(self, cutoff: datetime) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM reservation_requests
                WHERE status IN ('completed', 'failed')
                  AND notification_status = 'sent'
                  AND completed_at < ?
                """,
                (_timestamp(cutoff),),
            )
        return int(cursor.rowcount)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> QueuedReservationRequest:
        submitted_at = _parse_timestamp(row["submitted_at"])
        next_attempt_at = _parse_timestamp(row["next_attempt_at"])
        if submitted_at is None or next_attempt_at is None:
            raise ValueError("Stored queue request is missing a timestamp.")
        return QueuedReservationRequest(
            request_id=str(row["request_id"]),
            dedupe_key=str(row["dedupe_key"]),
            pending=_pending_from_json(str(row["pending_json"])),
            reserved_by_name=str(row["reserved_by_name"]),
            destination=QueueDestination(
                mode=str(row["destination_mode"]),
                channel_id=str(row["channel_id"]),
                thread_ts=str(row["thread_ts"]),
                message_ts=str(row["message_ts"]),
            ),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            notification_attempts=int(row["notification_attempts"]),
            submitted_at_utc=submitted_at,
            next_attempt_at_utc=next_attempt_at,
            completed_at_utc=_parse_timestamp(row["completed_at"]),
            result=_group_from_json(row["result_json"]),
            last_error=str(row["last_error"]),
        )


QueueNotifier = Callable[[QueuedReservationRequest], None]


class ReservationQueueWorker:
    """Rate-limited worker that commits queued requests and reports outcomes."""

    def __init__(
        self,
        request_queue: ReservationRequestQueue,
        service: ReservationService,
        notifier: QueueNotifier,
        *,
        requests_per_minute: float = 4.0,
        max_attempts: int = 8,
        retry_base_seconds: float = 15.0,
        retry_max_seconds: float = 300.0,
        retention_days: int = 30,
        poll_interval_seconds: float = 0.5,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be greater than zero")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self.request_queue = request_queue
        self.service = service
        self.notifier = notifier
        self.requests_per_minute = requests_per_minute
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.retention_days = retention_days
        self.poll_interval_seconds = poll_interval_seconds
        self.clock = clock
        self._minimum_interval = 60.0 / requests_per_minute
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        now = self.clock().astimezone(UTC)
        self.request_queue.recover_interrupted(now=now)
        self.request_queue.purge_finished_before(
            now - timedelta(days=self.retention_days)
        )
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="reservation-queue-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def process_ready_request(self, *, now: datetime | None = None) -> bool:
        attempted_at = (now or self.clock()).astimezone(UTC)
        request = self.request_queue.claim_next(now=attempted_at)
        if request is None:
            return False
        try:
            result = self.service.commit(
                request.pending,
                reserved_by_name=request.reserved_by_name,
                group_id=request.request_id,
            )
        except InventoryBotError as exc:
            self.request_queue.mark_failed(request.request_id, error=str(exc))
        except Exception as exc:
            if request.attempts >= self.max_attempts:
                LOGGER.exception(
                    "Reservation request %s failed after %s attempts",
                    request.request_id,
                    request.attempts,
                )
                self.request_queue.mark_failed(
                    request.request_id,
                    error=(
                        "I couldn't update inventory after several attempts. "
                        f"Please contact the inventory administrator and include "
                        f"request ID `{request.request_id}`."
                    ),
                )
            else:
                delay = self._retry_delay(request.attempts)
                LOGGER.exception(
                    "Reservation request %s will retry in %.0f seconds",
                    request.request_id,
                    delay,
                )
                self.request_queue.mark_retry(
                    request.request_id,
                    next_attempt_at_utc=attempted_at + timedelta(seconds=delay),
                    error=str(exc),
                )
        else:
            self.request_queue.mark_completed(request.request_id, result)
        return True

    def deliver_ready_notification(self, *, now: datetime | None = None) -> bool:
        attempted_at = (now or self.clock()).astimezone(UTC)
        request = self.request_queue.claim_notification(now=attempted_at)
        if request is None:
            return False
        try:
            self.notifier(request)
        except Exception:
            if request.notification_attempts >= self.max_attempts:
                LOGGER.exception(
                    "Giving up Slack notification for request %s after %s attempts",
                    request.request_id,
                    request.notification_attempts,
                )
                self.request_queue.mark_notification_abandoned(request.request_id)
            else:
                delay = self._retry_delay(request.notification_attempts)
                LOGGER.exception(
                    "Slack notification for request %s will retry in %.0f seconds",
                    request.request_id,
                    delay,
                )
                self.request_queue.mark_notification_retry(
                    request.request_id,
                    next_attempt_at_utc=attempted_at + timedelta(seconds=delay),
                )
        else:
            self.request_queue.mark_notification_sent(request.request_id)
        return True

    def _retry_delay(self, attempts: int) -> float:
        return min(
            self.retry_base_seconds * (2 ** max(0, attempts - 1)),
            self.retry_max_seconds,
        )

    def _run(self) -> None:
        last_sheet_attempt = 0.0
        next_cleanup = time.monotonic() + 86400.0
        while not self._stop_event.is_set():
            try:
                if time.monotonic() >= next_cleanup:
                    self.request_queue.purge_finished_before(
                        self.clock().astimezone(UTC)
                        - timedelta(days=self.retention_days)
                    )
                    next_cleanup = time.monotonic() + 86400.0
                if self.deliver_ready_notification():
                    continue
                since_last = time.monotonic() - last_sheet_attempt
                if since_last >= self._minimum_interval:
                    if self.process_ready_request():
                        last_sheet_attempt = time.monotonic()
                        continue
                wait_for_rate = max(0.0, self._minimum_interval - since_last)
                wait_seconds = min(
                    self.poll_interval_seconds,
                    wait_for_rate or self.poll_interval_seconds,
                )
            except Exception:
                LOGGER.exception("Unexpected reservation queue worker error")
                try:
                    self.request_queue.recover_interrupted(now=self.clock())
                except Exception:
                    LOGGER.exception("Unable to recover the reservation queue")
                wait_seconds = self.poll_interval_seconds
            self._stop_event.wait(wait_seconds)
