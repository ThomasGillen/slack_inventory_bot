"""Create or migrate the expected Google Sheet header row."""

import argparse

from .config import Settings
from .sheets import GoogleSheetsRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the inventory Items sheet.")
    parser.add_argument(
        "--migrate-items",
        action="store_true",
        help="Back up and convert a supported older Items layout.",
    )
    args = parser.parse_args()
    settings = Settings.from_env(require_slack=False)
    repository = GoogleSheetsRepository(settings)
    if args.migrate_items:
        backup_name = repository.migrate_items_schema()
        repository.ensure_schema()
        if backup_name:
            print(
                f"Migrated {settings.items_sheet}. The original data was copied to "
                f"{backup_name}."
            )
        else:
            print(f"{settings.items_sheet} already uses the current schema.")
    else:
        repository.ensure_schema()
        print(
            f"Initialized {settings.items_sheet} in spreadsheet "
            f"{settings.spreadsheet_id}."
        )


if __name__ == "__main__":
    main()
