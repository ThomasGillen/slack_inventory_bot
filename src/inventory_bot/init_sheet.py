"""Create or migrate the expected Google Sheet header row."""

import argparse

from .config import Settings
from .sheets import GoogleSheetsRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize the inventory Items and Reservations sheets."
    )
    parser.add_argument(
        "--migrate-items",
        action="store_true",
        help="Back up and convert a supported older Items layout.",
    )
    parser.add_argument(
        "--migrate-reservations",
        action="store_true",
        help="Back up an older Reservations tab and initialize the schedule layout.",
    )
    args = parser.parse_args()
    settings = Settings.from_env(require_slack=False)
    repository = GoogleSheetsRepository(settings)
    messages: list[str] = []
    if args.migrate_items:
        backup_name = repository.migrate_items_schema()
        if backup_name:
            messages.append(
                f"Migrated {settings.items_sheet}. The original data was copied to "
                f"{backup_name}."
            )
        else:
            messages.append(f"{settings.items_sheet} already uses the current schema.")
    if args.migrate_reservations:
        backup_name = repository.migrate_reservations_schema()
        if backup_name:
            messages.append(
                f"Migrated {settings.reservations_sheet}. The original data was "
                f"copied to {backup_name}."
            )
        else:
            messages.append(
                f"{settings.reservations_sheet} already uses the current schema."
            )

    repository.ensure_schema()
    if not messages:
        messages.append(
            f"Initialized {settings.items_sheet} and {settings.reservations_sheet} "
            f"in spreadsheet {settings.spreadsheet_id}."
        )
    for message in messages:
        print(message)


if __name__ == "__main__":
    main()
