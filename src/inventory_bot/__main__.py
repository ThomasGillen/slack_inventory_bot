"""Run the bot over Slack Socket Mode."""

import logging

from .config import Settings
from .scheduler import ReservationReconciler
from .slack_app import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    app = create_app(settings)
    reservation_service = getattr(app, "_inventory_service")
    reservation_service.reconcile()
    reconciler = ReservationReconciler(reservation_service)
    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise RuntimeError(
            "Slack Bolt is not installed. Run: python -m pip install -e ."
        ) from exc
    reconciler.start()
    try:
        SocketModeHandler(app, settings.slack_app_token).start()
    finally:
        reconciler.stop()


if __name__ == "__main__":
    main()
