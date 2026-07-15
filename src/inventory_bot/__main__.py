"""Run the bot over Slack Socket Mode."""

import logging

from .config import Settings
from .slack_app import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    app = create_app(settings)
    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise RuntimeError(
            "Slack Bolt is not installed. Run: python -m pip install -e ."
        ) from exc
    SocketModeHandler(app, settings.slack_app_token).start()


if __name__ == "__main__":
    main()

