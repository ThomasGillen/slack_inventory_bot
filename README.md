# Slack Inventory Bot

A Slack bot that accepts reservations in a direct message or an
`@InventoryBot` channel mention, checks a Google Sheet, asks for confirmation,
and updates the item's current reservation state.

## What is implemented

- Direct messages through Slack's `message.im` event
- Channel requests through the `app_mention` event
- Block Kit reservation modal with a multi-item search plus combined Start and End datetime pickers
- Global **Reserve an item** Slack shortcut
- One unique inventory item per sheet row
- Immediate and future reservation scheduling
- Overlap rejection, with back-to-back reservations allowed
- Automatic activation and expiry reconciliation every 30 seconds
- Confirm and Cancel buttons before a sheet write
- Grouped reservations for up to 20 items with all-or-nothing conflict checks
- Durable SQLite request buffering with first-in, first-out processing
- Automatic retries, duplicate-submission protection, and restart recovery
- Separate retries for Slack result notifications after a sheet write succeeds
- Conservative four-reservation-per-minute default processing rate
- A 15-second item-picker cache to reduce Google Sheets reads while users type
- Owner-only partial cancellation through commands or Manage Reservations
- Availability checks before confirmation and immediately before writing
- A process-level write lock for simultaneous requests
- `status` and `status <item>` availability commands
- `help` command with a complete command overview
- Readable, timezone-qualified sheet times using `INVENTORY_TIMEZONE`

Example requests:

```text
reserve kayak1 from Friday at 1 PM until Friday at 3 PM
reserve kayak2 until tomorrow at 5 PM
reserve kayak3 until 2026-07-18 17:00
status
status kayak1
cancel kayak1
cancel group kayak1
help
```

The preferred reservation flow is to send `reserve`, click **Open reservation
form**, and select up to 20 items that need the same start and end time. The
combined **Start** picker defaults to now and the combined **End** picker defaults
to one hour later. Change either the date or time directly; no separate start-mode
choice is needed. Leaving the default Start unchanged uses the exact submission
time. The full text form remains available as a single-item fallback.

## Availability model

The `Reservations` tab is the schedule source of truth:

| reservation_id | group_id | item_name | start_time | end_time | reserved_by | slack_user_id |
|---|---|---|---|---|---|---|
| generated UUID | shared group UUID | kayak1 | 2026-07-18 01:00 PM PDT (UTC-07:00) | 2026-07-18 05:00 PM PDT (UTC-07:00) | Taylor Smith | U123... |
| generated UUID | shared group UUID | paddle1 | 2026-07-18 01:00 PM PDT (UTC-07:00) | 2026-07-18 05:00 PM PDT (UTC-07:00) | Taylor Smith | U123... |

The bot validates every selected item before writing any of them, then appends
one row per item with a shared `group_id`. If any selected item overlaps, the
whole attempt fails and no new rows are written. Because the `[start, end)` end
is exclusive, one reservation may start exactly when the previous one ends.

The `Items` tab remains the simple live inventory view:

| item_name | location | reservation_end | reserved_by |
|---|---|---|---|
| kayak1 | A |  |  |
| kayak2 | B |  |  |
| kayak3 | A |  |  |

- Before a future reservation starts, its `Items` row remains available.
- Within 30 seconds after the start, the bot writes its end time and holder name
  into `Items`.
- Within 30 seconds after the end, the bot clears the two live-state cells and
  removes the ended row from `Reservations`.
- The same reconciliation runs immediately at startup, so downtime is repaired.
- `cancel <item>` cancels only that item from the person's active reservation, or
  their sole upcoming reservation for that item.
- `cancel` opens Manage Reservations, where the owner chooses a reservation and
  checks exactly which items to cancel. Confirmed reservation messages also have
  a **Manage reservation** button.
- `cancel group <item>` explicitly cancels every item sharing the selected
  reservation's `group_id`.
- When an item has multiple upcoming reservations, the manager asks the user to
  choose by time instead of guessing which one to cancel.
- Every item implicitly has quantity 1.
- Item names must be unique; the name acts as the lookup key.
- Partial names work only when they match exactly one item.

The bot writes readable timestamps such as
`2026-07-18 05:00 PM PDT (UTC-07:00)`, using `INVENTORY_TIMEZONE`. The numeric
offset preserves the exact instant even if daylight-saving rules or the configured
timezone later change. Existing ISO/UTC timestamps such as
`2026-07-19T00:00:00Z` remain supported.

## Request queue and source of truth

Google Sheets remains the complete user-facing inventory and confirmed schedule
source of truth. The bot also creates
`.inventory_bot/reservation_queue.sqlite3` locally, but inventory maintainers do
not need to open or manage it. That file contains only pending requests, retry
state, duplicate-submission keys, and recent processing outcomes.

When someone submits a reservation, Slack first says that the request is queued
and explicitly not confirmed. The bot then processes requests first-in,
first-out, rereads the latest sheet, and sends a separate confirmed or failed
result. Overlapping requests still fail normally; the earliest queued compatible
request wins. A multi-item request remains all-or-nothing.

The default rate is four reservation groups per minute. Temporary Google errors
are retried with increasing delays, up to eight attempts. If the bot restarts,
requests that were waiting or processing are recovered. The queue request ID is
also used as the confirmed sheet `group_id`, so retrying a write that actually
succeeded cannot create a duplicate reservation. Finished, successfully
notified queue records are retained for 30 days and then removed automatically.

Do not delete the SQLite file while requests are pending. Deleting it does not
remove reservations already confirmed in Google Sheets, but it would discard
requests still waiting to be processed.

## 1. Create the Slack app

1. Go to Slack API **Your Apps** and choose **Create New App**.
2. Choose **From an app manifest** and paste [slack-manifest.yaml](slack-manifest.yaml).
3. Under **Basic Information > App-Level Tokens**, generate an `xapp-` token with
   the `connections:write` scope.
4. Install the app to the workspace and copy its `xoxb-` bot token.
5. Invite the bot to each channel where it should accept mentions.

For an app that was created before the reservation modal was added:

1. Open the app's **App Manifest** page in Slack.
2. Replace the YAML with the current [slack-manifest.yaml](slack-manifest.yaml).
3. Save the changes and reinstall the app if Slack prompts you.

This adds the global **Reserve an item** shortcut. The message button and modal
still work without the global shortcut after the bot code is restarted.

The manifest enables Socket Mode, the App Home Messages tab, interactive buttons,
and these bot scopes/events:

- `chat:write`
- `app_mentions:read` / `app_mention`
- `im:history` / `message.im`
- `users:read` for the reservation holder's display or real name

After adding `users:read` to an existing app, reinstall the app to the workspace
so Slack grants the new permission, then restart `inventory-bot`. Email access is
not requested or needed.

## 2. Create Google credentials and the sheet

1. Create or select a Google Cloud project.
2. Enable the **Google Sheets API**.
3. Create a service account and, for local development, download a JSON key.
4. Create a Google Sheet and share it with the service account's email as an editor.
5. Copy the spreadsheet ID from the URL between `/d/` and `/edit`.

Keep the JSON key outside this repository. The `.gitignore` also excludes common
service-account filenames as a second line of defense.

When deployed on Google Cloud, omit the key file and give the runtime service
account access to the sheet; the bot will use Application Default Credentials.

## 3. Install and configure locally

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Edit `.env`:

```dotenv
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
GOOGLE_SPREADSHEET_ID=...
GOOGLE_SERVICE_ACCOUNT_FILE=C:\secure-path\service-account.json
INVENTORY_TIMEZONE=America/Los_Angeles
```

`GOOGLE_SERVICE_ACCOUNT_JSON` can be used instead of a file when a deployment
platform provides credentials as a secret environment variable.

## 4. Initialize or migrate the spreadsheet

For a new empty spreadsheet:

```powershell
inventory-sheet-init
```

If the `Items` tab still has the original six-column layout, or the earlier
`item_name, location, active, reservation_end` layout, stop the bot and run:

```powershell
inventory-sheet-init --migrate-items
```

The migration duplicates the current tab to a timestamped `Items Backup ...` tab
before converting it.

For this scheduled-reservation upgrade, initialize the new schedule tab with:

```powershell
inventory-sheet-init --migrate-reservations
```

If the earlier six-column `Reservations` tab exists, the command copies it to a
timestamped `Reservations Backup ...` tab and preserves every schedule row while
adding a `group_id`. Each older single-item row becomes its own group. Any active
state found only in `Items` is seeded into the schedule so it is not lost.

After migration, add or edit items in the first two columns only:

| item_name | location | reservation_end | reserved_by |
|---|---|---|---|
| kayak1 | A |  |  |
| kayak2 | B |  |  |

Normally, leave `reservation_end` and `reserved_by` for the bot to manage. To
release selected items early, send `cancel` and use Manage Reservations, or send
`cancel <item>` for one item. A sheet manager can cancel an item by clearing its
complete row in `Reservations`; clear every row with the same `group_id` to cancel
the entire group. The bot will then reconcile `Items`. Clearing only the live
`Items` cells is temporary because an active schedule row will restore them. A
`-` is accepted as an empty reservation end, though a blank cell is preferred.

## 5. Run the bot

```powershell
inventory-bot
```

In Slack, use either:

```text
reserve
@InventoryBot reserve
```

Then click **Open reservation form**. The multi-select searches the current
Google Sheet and lets the user choose up to 20 configured items. Slack displays
the combined datetime pickers in the user's local Slack timezone; confirmations
and sheet timestamps use `INVENTORY_TIMEZONE`. Every selected item is checked
again when the reservation is committed.

Send `cancel` to open the cancellation launcher. The manager lists only the
requester's active and upcoming reservations, then provides an item multi-select plus
an explicitly confirmed **Cancel entire reservation** action. Send `help` at any
time for the full command overview.

The process must stay running to receive Socket Mode events. For production,
deploy one continuously running instance and place all tokens and Google
credentials in the hosting platform's secret store.

Optional allowlists can restrict use:

```dotenv
ALLOWED_CHANNEL_IDS=C01234567,C07654321
ALLOWED_USER_IDS=U01234567,U07654321
```

Direct messages bypass the channel allowlist but still honor the user allowlist.

The request-buffer defaults normally do not need to be changed. They can be
overridden in `.env` when needed:

```dotenv
RESERVATION_QUEUE_DATABASE=.inventory_bot/reservation_queue.sqlite3
RESERVATION_QUEUE_RATE_PER_MINUTE=4
RESERVATION_QUEUE_MAX_ATTEMPTS=8
RESERVATION_QUEUE_RETENTION_DAYS=30
ITEM_PICKER_CACHE_SECONDS=15
```

Keep one running bot instance and store the queue on persistent local storage.
If the bot is moved to another computer while requests are pending, stop it and
copy the SQLite file along with its `-wal` and `-shm` companion files, or wait
for the queue to drain before moving it.

## Supported start/end formats

- `tomorrow at 3 PM`
- `Friday at 3 PM`
- `in 4 hours`
- `July 18 at 5 PM`
- `2026-07-18 17:00`
- ISO 8601 timestamps, including explicit offsets

Times without an explicit offset are interpreted using `INVENTORY_TIMEZONE`.
Ambiguous, invalid, or past times are rejected rather than guessed.

The text form accepts either an explicit start or an immediate reservation:

```text
reserve kayak1 from tomorrow at 1 PM until tomorrow at 3 PM
reserve kayak1 until tomorrow at 3 PM
```

## Tests

The parser and reservation service use only the Python standard library, so their
tests can run even before installing Slack or Google packages:

```powershell
python -m unittest discover -v
python -m compileall -q src
```

## Current MVP limits

- The SQLite queue and process-level lock protect one bot instance. Do not run
  multiple instances against the same sheet with separate local queue files.
- Multi-item schedule appends, live-state updates, and cancellations use batched
  Google Sheets requests, so selecting several items does not multiply the main
  API request count one-for-one.
- New scheduled rows retain the Slack user ID internally for owner-safe
  cancellation; `Items` still shows only the readable holder name.
- Ended schedule rows are removed, so the sheet does not provide reservation
  history.
- Google Sheets is appropriate for a modest internal inventory. A transactional
  database should replace it if reservation volume or business criticality grows.
