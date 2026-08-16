# Slack Inventory Bot

A Slack reservation service backed by a Google Sheets inventory schedule. Users
can open a reservation form from a direct message, an `@InventoryBot` channel
mention, or a global Slack shortcut; the bot queues the request, checks the latest
schedule, and reports a separate confirmed or failed result.

> Setting up a new machine or workspace? Follow the complete
> [Windows 10/11 setup guide](SETUP.md).

## What the bot provides

- Direct-message handling through Slack's `message.im` event
- Channel handling through the `app_mention` event
- Global **Reserve an item** Slack shortcut
- Block Kit reservation modal with searchable multi-item selection
- Combined Start and End date/time pickers
- Immediate and future reservations
- Grouped reservations for up to 20 items
- All-or-nothing conflict checking for multi-item requests
- Owner-safe partial or whole-group cancellation
- Current and upcoming availability reporting
- Durable first-in, first-out request buffering in SQLite
- Automatic retry, duplicate-submission protection, and restart recovery
- Separate Slack-notification retries after a successful sheet write
- Automatic activation and expiration reconciliation every 30 seconds
- A short item-picker cache to reduce Google Sheets reads while users type
- Optional Slack channel and user allowlists

## User workflow

### Reserve inventory

In a DM, send:

```text
reserve
```

In a channel where the bot has been invited, send:

```text
@InventoryBot reserve
```

The bot posts an **Open reservation form** button. The form lets the requester
choose up to 20 items sharing the same start and end time. The Start picker
defaults to now and the End picker defaults to one hour later. Leaving the
default Start unchanged uses the exact submission time.

Submitting the form does not immediately mean the inventory is reserved. Slack
first reports that the request is queued. The worker then rereads the latest
Google Sheet, processes queued work in order, and sends a separate confirmation
or failure message.

The same modal can be opened from Slack's global **Reserve an item** shortcut.

### Check status

```text
status
status kayak1
```

The full status command shows the inventory list. A specific-item status reports
its current holder and end time, or its next scheduled reservation.

### Cancel reservations

```text
cancel
cancel kayak1
cancel group kayak1
```

- `cancel` opens **Manage Reservations**, listing only the requester's active and
  upcoming reservation groups.
- `cancel <item>` cancels only that item from the requester's current reservation,
  or their sole upcoming reservation for that item.
- `cancel group <item>` explicitly cancels every item sharing the selected
  reservation's group ID.
- A confirmed reservation message also includes a **Manage reservation** button.

If an item has multiple upcoming reservations, the bot asks the requester to
choose the intended time instead of guessing.

### Command summary

```text
reserve
status
status <item>
cancel
cancel <item>
cancel group <item>
help
```

## Architecture

```text
Slack DM, mention, or shortcut
             |
             v
      Slack Bolt handlers
             |
             v
 Durable SQLite request queue
             |
             v
 Reservation and conflict service
             |
             v
       Google Sheets API
        /             \
   Items view    Reservations schedule
             |
             v
    Slack result notification
```

The implementation separates Slack presentation, reservation rules, persistence,
and queue processing:

| Component | Responsibility |
|---|---|
| `slack_app.py` | Slack events, actions, modal submissions, and messages |
| `slack_views.py` | Block Kit launchers, reservation forms, and cancellation forms |
| `service.py` | Item resolution, overlap checks, commits, cancellation, and reconciliation |
| `sheets.py` | Google Sheets schemas, reads, batched writes, and migrations |
| `reservation_queue.py` | Durable FIFO requests, retries, deduplication, and recovery |
| `config.py` | `.env` loading and startup validation |
| `init_sheet.py` | New-sheet initialization and supported schema migrations |

## Inventory and schedule model

### `Items`: live operational view

Each row represents one unique reservable item with implicit quantity 1:

| item_name | location | reservation_end | reserved_by |
|---|---|---|---|
| kayak1 | A |  |  |
| kayak2 | B |  |  |
| camera1 | Equipment Room |  |  |

Inventory maintainers edit `item_name` and `location`. The bot manages
`reservation_end` and `reserved_by` for the currently active reservation.

Item names must be unique, including capitalization-only differences. Exact names
are preferred; partial names work only when exactly one item matches.

### `Reservations`: schedule source of truth

Confirmed schedule rows use this model:

| reservation_id | group_id | item_name | start_time | end_time | reserved_by | slack_user_id |
|---|---|---|---|---|---|---|
| generated UUID | shared group UUID | kayak1 | 2026-07-18 01:00 PM PDT (UTC-07:00) | 2026-07-18 05:00 PM PDT (UTC-07:00) | Taylor Smith | U123... |
| generated UUID | shared group UUID | paddle1 | 2026-07-18 01:00 PM PDT (UTC-07:00) | 2026-07-18 05:00 PM PDT (UTC-07:00) | Taylor Smith | U123... |

One row is written per item. Items submitted together share a `group_id`, allowing
the bot to cancel selected items or the whole reservation as one logical group.

The bot treats each interval as `[start, end)`: start is inclusive and end is
exclusive. A reservation may therefore begin exactly when the previous one ends.
If any selected item overlaps, the entire multi-item request fails and no new
schedule rows are written.

Times are stored in a readable, timezone-qualified format such as
`2026-07-18 05:00 PM PDT (UTC-07:00)`. The abbreviation is convenient for people;
the numeric offset preserves the exact instant if daylight-saving rules or the
configured timezone later change. Existing ISO/UTC timestamps remain readable.

## Reconciliation behavior

The `Reservations` tab is authoritative for scheduled work; `Items` is a live
projection of the currently active reservation:

- A future reservation does not mark the item unavailable in `Items` before its
  start time.
- Within 30 seconds after a reservation starts, the reconciler writes the end
  time and holder name into the corresponding `Items` row.
- Within 30 seconds after it ends, the reconciler clears those live-state cells
  and removes the ended schedule row.
- Reconciliation also runs immediately at startup, repairing live state after
  downtime.
- Clearing only the live `Items` cells is temporary while an active schedule row
  still exists; reconciliation restores them.

A sheet manager can manually cancel a scheduled item by clearing its complete row
in `Reservations`. Clearing every row with the same `group_id` cancels the whole
group.

## Queue and reliability model

Google Sheets remains the user-facing inventory and confirmed-schedule source of
truth. The bot also creates `.inventory_bot/reservation_queue.sqlite3` locally.
This database contains only pending requests, retry state, duplicate-submission
keys, notification state, and recent processing outcomes.

The queue provides:

- stable FIFO ordering, including identical submission times;
- a conservative default of four reservation groups per minute;
- increasing delays for temporary Google failures;
- up to eight processing attempts by default;
- recovery of requests interrupted by a restart;
- independent retries for Slack notifications;
- a queue request ID reused as the sheet `group_id`, preventing duplicate rows if
  a write succeeds but its response is lost; and
- automatic removal of successfully completed and notified queue records after
  30 days.

Availability is checked against the latest sheet immediately before each commit.
The earliest compatible queued request wins; expected overlap conflicts fail
normally and are not retried.

Do not delete or move the SQLite database while requests are pending. If the bot
must move computers, let the queue drain first or stop the process and move the
database together with any `-wal` and `-shm` companion files.

## Configuration model

Configuration is read from `.env` or existing process environment variables.
See [SETUP.md](SETUP.md) for where each credential comes from and how to configure
it safely.

| Variable | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | Workspace bot token beginning with `xoxb-` |
| `SLACK_APP_TOKEN` | Socket Mode app token beginning with `xapp-` |
| `GOOGLE_SPREADSHEET_ID` | ID of the shared inventory spreadsheet |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Local path to the service-account JSON key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Alternative inline credential for hosted secret stores |
| `INVENTORY_TIMEZONE` | Timezone used for sheet display and reservation interpretation |
| `ITEMS_SHEET` | Items tab name; defaults to `Items` |
| `RESERVATIONS_SHEET` | Schedule tab name; defaults to `Reservations` |
| `ALLOWED_CHANNEL_IDS` | Optional comma-separated Slack channel allowlist |
| `ALLOWED_USER_IDS` | Optional comma-separated Slack user allowlist |
| `RESERVATION_QUEUE_DATABASE` | Local SQLite queue path |
| `RESERVATION_QUEUE_RATE_PER_MINUTE` | Queue processing rate; defaults to 4 |
| `RESERVATION_QUEUE_MAX_ATTEMPTS` | Maximum temporary-failure attempts; defaults to 8 |
| `RESERVATION_QUEUE_RETENTION_DAYS` | Completed queue retention; defaults to 30 |
| `ITEM_PICKER_CACHE_SECONDS` | Item search cache duration; defaults to 15 |

Set only one of `GOOGLE_SERVICE_ACCOUNT_FILE` and
`GOOGLE_SERVICE_ACCOUNT_JSON`. When neither is set, Google Application Default
Credentials are used.

## Running an already configured installation

On Windows, double-click **Start Inventory Bot.exe** and keep its window open.
The direct equivalent is:

```powershell
.\.venv\Scripts\python.exe -m inventory_bot
```

For first-time initialization, credential setup, Slack workspace configuration,
spreadsheet migration, or troubleshooting, use [SETUP.md](SETUP.md).

## Tests

Run the automated tests and source compilation checks from the project folder:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q src tests
```

Rebuild the Windows launchers after changing their C# source:

```powershell
.\windows-launchers\build-launchers.ps1
```

## Current limitations

- Run one bot instance. Separate instances do not share the process lock or local
  SQLite queue.
- Google Sheets is suitable for a modest internal inventory, not a high-volume
  transactional system.
- Each `Items` row is one unit; pooled quantities are represented as multiple
  uniquely named rows.
- Ended schedule rows are removed, so the Google Sheet is not a permanent
  reservation-history ledger.
- New reservation rows retain the Slack user ID for owner-safe cancellation;
  `Items` displays only the readable holder name.
- Credentials and the local queue require deliberate handling when the bot moves
  to another computer or hosting environment.
