# Slack Inventory Bot

A Slack bot that accepts reservations in a direct message or an
`@InventoryBot` channel mention, checks a Google Sheet, asks for confirmation,
and updates the item's current reservation state.

## What is implemented

- Direct messages through Slack's `message.im` event
- Channel requests through the `app_mention` event
- Block Kit reservation modal with a live item search, date picker, and time picker
- Global **Reserve an item** Slack shortcut
- One unique inventory item per sheet row
- Item name and reservation end-time parsing
- Confirm and Cancel buttons before a sheet write
- Owner-only `cancel <item>` for confirmed reservations
- Availability checks before confirmation and immediately before writing
- A process-level write lock for simultaneous requests
- `status` and `status <item>` availability commands
- Readable, timezone-qualified sheet times using `INVENTORY_TIMEZONE`

Example requests:

```text
reserve kayak1 until Friday at 3 PM
reserve kayak2 until tomorrow at 5 PM
reserve kayak3 until 2026-07-18 17:00
status
status kayak1
cancel kayak1
```

The preferred reservation flow is to send `reserve`, click **Open reservation
form**, and use the dropdown and date/time controls. The full text form remains
available as a fallback.

## Availability model

The `Items` tab is the source of truth and has four columns:

| item_name | location | reservation_end | reserved_by |
|---|---|---|---|
| kayak1 | A |  |  |
| kayak2 | B |  |  |
| kayak3 | A |  |  |

- A blank or past `reservation_end` means the item is available.
- A future `reservation_end` means the item is reserved.
- On confirmation, the bot writes the end time and the person's Slack display
  name into that row (or their real name when no display name is set).
- `cancel <item>` clears both reservation cells when requested by that person.
- Every item implicitly has quantity 1.
- Item names must be unique; the name acts as the lookup key.
- Partial names work only when they match exactly one item.

The bot writes readable timestamps such as
`2026-07-18 05:00 PM PDT (UTC-07:00)`, using `INVENTORY_TIMEZONE`. The numeric
offset preserves the exact instant even if daylight-saving rules or the configured
timezone later change. Existing ISO/UTC timestamps such as
`2026-07-19T00:00:00Z` remain supported.

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

After migration, add or edit items in the first two columns only:

| item_name | location | reservation_end | reserved_by |
|---|---|---|---|
| kayak1 | A |  |  |
| kayak2 | B |  |  |

Normally, leave `reservation_end` and `reserved_by` for the bot to manage. To
release your own item early, send `cancel <item>` to the bot. A sheet manager can
also clear both cells manually. A `-` is accepted as an empty reservation end,
though a blank cell is preferred.

## 5. Run the bot

```powershell
inventory-bot
```

In Slack, use either:

```text
reserve
@InventoryBot reserve
```

Then click **Open reservation form**. The item control searches the current
Google Sheet and only offers items whose reservation has expired or is blank.
The modal's date and time controls use `INVENTORY_TIMEZONE`.

The process must stay running to receive Socket Mode events. For production,
deploy one continuously running instance and place all tokens and Google
credentials in the hosting platform's secret store.

Optional allowlists can restrict use:

```dotenv
ALLOWED_CHANNEL_IDS=C01234567,C07654321
ALLOWED_USER_IDS=U01234567,U07654321
```

Direct messages bypass the channel allowlist but still honor the user allowlist.

## Supported end-time formats

- `tomorrow at 3 PM`
- `Friday at 3 PM`
- `in 4 hours`
- `July 18 at 5 PM`
- `2026-07-18 17:00`
- ISO 8601 timestamps, including explicit offsets

Times without an explicit offset are interpreted using `INVENTORY_TIMEZONE`.
Ambiguous, invalid, or past times are rejected rather than guessed.

## Tests

The parser and reservation service use only the Python standard library, so their
tests can run even before installing Slack or Google packages:

```powershell
python -m unittest discover -v
python -m compileall -q src
```

## Current MVP limits

- The process-level lock protects one bot instance. Do not horizontally scale the
  bot while Google Sheets is the source of truth.
- Cancellation matches the requester's current Slack name (and still recognizes
  legacy Slack IDs). Because the four-column sheet does not retain a hidden user
  ID, duplicate or changed Slack names may require a sheet manager to clear
  `reservation_end` and `reserved_by` manually.
- The simplified sheet stores current state, not reservation history.
- Google Sheets is appropriate for a modest internal inventory. A transactional
  database should replace it if reservation volume or business criticality grows.
