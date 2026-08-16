# Slack Inventory Bot Setup Guide

This guide walks through a complete first-time setup on Windows 10 or Windows 11: creating the
Slack app, creating Google credentials, sharing and initializing the spreadsheet,
configuring the local project, and testing the finished connection.

For an explanation of the bot's behavior and architecture, see
[README.md](README.md).

## Guide map

- [Before you begin](#before-you-begin)
- [Part 1: Create and configure the Slack app](#part-1-create-and-configure-the-slack-app)
- [Part 2: Create Google credentials and the spreadsheet](#part-2-create-google-credentials-and-the-spreadsheet)
- [Part 3: Configure and initialize Windows](#part-3-configure-and-initialize-the-windows-installation)
- [Part 4: Run and test the bot](#part-4-run-and-test-the-bot)
- [Updating an existing installation](#updating-an-existing-installation)
- [Optional configuration](#optional-configuration)
- [Troubleshooting](#troubleshooting)
- [Security and operation notes](#security-and-operation-notes)

## What you will create

By the end of setup you will have:

- one Slack app installed in your workspace;
- an `xapp-` Slack app token for Socket Mode;
- an `xoxb-` Slack bot token;
- one Google Cloud project with the Google Sheets API enabled;
- one Google service account and, for local use, its JSON key;
- one Google Sheet shared with that service account;
- a local `.env` file containing the required configuration; and
- a running bot connected to Slack and Google Sheets.

The bot uses Socket Mode, so local setup does not require a public web server,
request URL, Slack signing secret, client ID, or client secret.

## Before you begin

You need:

- Windows 10 or Windows 11;
- Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/);
- permission to create or install an app in the target Slack workspace;
- permission to create a Google Cloud project and service account; and
- a current, complete copy of this project.

During Python installation, enable **Add Python to PATH** if the installer offers
it. Confirm the Python launcher can find a supported version:

```powershell
py -3.11 --version
```

Python 3.12 and 3.13 are also supported. Substitute the installed version in the
commands below if necessary.

Run PowerShell commands from the project folder—the folder containing
`pyproject.toml`. For example:

```powershell
cd C:\path\to\slack_inventory_bot
Test-Path .\pyproject.toml
Test-Path .\slack-manifest.yaml
```

Both checks must return `True`.

## Part 1: Create and configure the Slack app

### 1. Create the app from the included manifest

1. Open the [Slack app dashboard](https://api.slack.com/apps) and sign in.
2. Select **Create New App**.
3. Select **From an app manifest**.
4. Select the Slack workspace where the bot will run, then select **Next**.
5. Choose **YAML** if Slack asks for the manifest format.
6. Open [slack-manifest.yaml](slack-manifest.yaml), copy the entire file, and
   paste it into Slack's manifest editor.
7. Select **Next**, review the summary, and select **Create**.

Slack documents the same create-from-manifest flow in its
[app manifest guide](https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests).
If the workspace requires administrator approval, submit the app for approval or
ask a workspace owner to complete the installation.

The manifest configures the following automatically:

- Socket Mode;
- the App Home Messages tab;
- interactive buttons and modal submissions;
- a global **Reserve an item** shortcut;
- `app_mention` and `message.im` event subscriptions; and
- the `app_mentions:read`, `chat:write`, `commands`, `im:history`, and
  `users:read` bot scopes.

`users:read` is used only to store a readable display or real name with the
reservation. The bot does not request email access.

### 2. Generate the Socket Mode `xapp-` token

1. In the new Slack app, open **Settings → Basic Information**.
2. Scroll to **App-Level Tokens**.
3. Select **Generate Token and Scopes**.
4. Name it something recognizable, such as `inventory-bot-windows`.
5. Add the `connections:write` scope.
6. Select **Generate**.
7. Copy the token beginning with `xapp-` and store it temporarily in a secure
   password manager or protected note.

This becomes `SLACK_APP_TOKEN` in `.env`. The `connections:write` scope is what
allows the app to establish its Socket Mode WebSocket connection. See Slack's
[Socket Mode quickstart](https://docs.slack.dev/quickstart/) and
[`connections:write` reference](https://docs.slack.dev/reference/scopes/connections.write/).

### 3. Install the app and copy the `xoxb-` token

1. Open **Features → OAuth & Permissions**.
2. Select **Install to Workspace** near the top of the page.
3. Review the requested permissions and select **Allow**.
4. After Slack returns to **OAuth & Permissions**, find
   **OAuth Tokens for Your Workspace**.
5. Copy the **Bot User OAuth Token** beginning with `xoxb-`.

This becomes `SLACK_BOT_TOKEN` in `.env`. Do not confuse it with the `xapp-`
token. Slack's [Bolt quickstart](https://docs.slack.dev/quickstart/) describes
both token types and recommends treating them like passwords.

If a manifest update later adds or changes scopes, return to
**OAuth & Permissions** and select **Reinstall to Workspace** so Slack grants the
new permissions.

### 4. Verify the Slack settings

The manifest should have configured these settings. It is still useful to verify
them before troubleshooting the bot:

- **Settings → Socket Mode:** enabled.
- **Features → App Home:** Messages tab enabled and users allowed to send
  messages.
- **Features → Event Subscriptions:** enabled, with `app_mention` and
  `message.im` under bot events.
- **Features → Interactivity & Shortcuts:** enabled, with the global
  **Reserve an item** shortcut.
- **Features → OAuth & Permissions:** the five bot scopes listed above.

Slack's [Python Socket Mode guide](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/)
describes the same App Home, event subscription, and Socket Mode settings.

## Part 2: Create Google credentials and the spreadsheet

The bot only needs the Google Sheets API. It does not need the Google Drive API,
an OAuth consent screen, user OAuth credentials, or domain-wide delegation. The
spreadsheet will be created manually and shared directly with the service
account.

### 5. Create a Google Cloud project

1. Open the [Google Cloud console](https://console.cloud.google.com/).
2. Open the project selector at the top of the page.
3. Select **New Project**.
4. Name it something recognizable, such as `slack-inventory-bot`.
5. Select **Create**, then make sure the new project is selected.

### 6. Enable the Google Sheets API

1. Open **APIs & Services → Library**.
2. Search for **Google Sheets API**.
3. Open the result and select **Enable**.

Google's [Workspace API guide](https://developers.google.com/workspace/guides/enable-apis)
lists the Sheets API service as `sheets.googleapis.com`.

### 7. Create the service account

1. Open **IAM & Admin → Service Accounts**.
2. Select **Create Service Account**.
3. Enter a name such as `slack-inventory-bot` and an optional description.
4. Select **Create and Continue**.
5. Leave the optional project-role selection blank; the spreadsheet will grant
   access by direct file sharing.
6. Select **Done**.

The service account is the bot's Google identity. Google recommends service
accounts for applications that access specific shared documents such as Sheets;
see [Create access credentials](https://developers.google.com/workspace/guides/create-credentials).

### 8. Download a JSON key for local use

1. On the **Service Accounts** page, select the service account's email address.
2. Open the **Keys** tab.
3. Select **Add Key → Create New Key**.
4. Choose **JSON**, then select **Create**.
5. Move the downloaded file to a private location outside this project, such as:

   ```text
   C:\Users\YourName\.credentials\slack-inventory-service-account.json
   ```

The downloaded file contains a private key and cannot be downloaded again. Do
not commit it, place it in a shared folder, send it through Slack, or paste its
contents into an issue. Google provides the same console steps and security
warnings in its [service-account key guide](https://docs.cloud.google.com/iam/docs/keys-create-delete).

Some organizations disable service-account key creation. If **Create New Key**
is unavailable, contact the Google Cloud administrator rather than weakening the
organization's policy. A future Google Cloud deployment can use Application
Default Credentials without a downloaded key.

### 9. Create and share the Google Sheet

1. Open [Google Sheets](https://sheets.google.com/) and create a blank spreadsheet.
2. Name it something recognizable, such as `Slack Inventory`.
3. Find the service account email on its Google Cloud details page or in the
   downloaded JSON file's `client_email` field. It resembles:

   ```text
   slack-inventory-bot@project-id.iam.gserviceaccount.com
   ```

4. In the spreadsheet, select **Share**.
5. Add the service account email and grant **Editor** access.
6. Select **Send** or **Share**.

Google's [Sheets sharing guide](https://support.google.com/docs/answer/9331169)
documents sharing a spreadsheet with a specific email and choosing Editor
access. Only this spreadsheet needs to be shared with the bot.

The spreadsheet URL looks like:

```text
https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit
```

Copy only the value between `/d/` and `/edit`. In this example, the spreadsheet
ID is:

```text
1AbCdEfGhIjKlMnOpQrStUvWxYz
```

## Part 3: Configure and initialize the Windows installation

### 10. Recommended setup using the Windows launcher

Double-click **Initialize Inventory Sheet.exe** in the project folder.

On its first run, the launcher:

1. verifies that the project files are present and Python 3.11+ is available;
2. creates `.venv` if necessary;
3. installs the project and its Slack/Google dependencies;
4. copies `.env.example` to `.env` if `.env` does not exist;
5. opens `.env` in Notepad; and
6. checks the configuration before contacting Google.

Internet access is needed the first time Python dependencies are installed. If
Windows refuses to run the downloaded executable, use the manual PowerShell setup
below instead of changing system security policy.

When `.env` opens, replace the placeholders with the values created above:

```dotenv
SLACK_BOT_TOKEN=xoxb-your-real-bot-token
SLACK_APP_TOKEN=xapp-your-real-socket-mode-token
GOOGLE_SPREADSHEET_ID=your-real-spreadsheet-id
GOOGLE_SERVICE_ACCOUNT_FILE=C:\Users\YourName\.credentials\slack-inventory-service-account.json
INVENTORY_TIMEZONE=America/New_York
```

Use the appropriate [IANA timezone name](https://www.iana.org/time-zones)
for the inventory's operating timezone, such as `America/New_York` or
`America/Los_Angeles`. Save `.env` and close Notepad.

Important details:

- `GOOGLE_SPREADSHEET_ID` is the ID, not the complete Google Sheets URL.
- `GOOGLE_SERVICE_ACCOUNT_FILE` is the complete path to the downloaded `.json`
  file, not the folder containing it.
- Do not add spaces around `=`.
- Do not send `.env` to another person or commit it to Git.

The launcher then displays a menu. For a new blank spreadsheet, choose
**1. Initialize or verify the current sheet**. A successful run reports that the
`Items` and `Reservations` tabs were initialized.

### 11. Manual PowerShell setup

Activation is not required. Using the virtual environment's Python executable
directly avoids PowerShell execution-policy and command-path problems:

```powershell
cd C:\path\to\slack_inventory_bot
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python.exe -m inventory_bot.init_sheet
```

If `.env` already exists, do not overwrite it. Edit it directly:

```powershell
notepad .env
```

Verify the service-account path if initialization reports a credential error:

```powershell
Test-Path -LiteralPath "C:\Users\YourName\.credentials\slack-inventory-service-account.json" -PathType Leaf
```

The result must be `True`.

### 12. Populate the inventory

After initialization, open the spreadsheet's `Items` tab. Add one unique item per
row in the first two columns and leave the two bot-managed columns blank:

| item_name | location | reservation_end | reserved_by |
|---|---|---|---|
| kayak1 | A |  |  |
| kayak2 | B |  |  |
| camera1 | Equipment Room |  |  |

Rules:

- Item names must be unique, including differences only in capitalization.
- Each row represents one reservable item with implicit quantity 1.
- Location may be blank.
- Leave `reservation_end` and `reserved_by` for the bot to manage.
- Do not rename or reorder the header columns.
- Leave the initialized `Reservations` tab headers intact.

## Part 4: Run and test the bot

### 13. Start the process

Double-click **Start Inventory Bot.exe**. Keep its window open—the bot stops when
the window closes.

The manual equivalent is:

```powershell
.\.venv\Scripts\python.exe -m inventory_bot
```

The first successful connection logs that the Slack Bolt app is running. The bot
must remain online to receive Socket Mode events and reconcile scheduled
reservations.

### 14. Add the bot to a channel

In every Slack channel where mentions should work, send:

```text
/invite @Inventory Bot
```

The app must be a channel member to receive mentions there. Direct messages do
not require a channel invitation.

### 15. Run smoke tests

Open the bot under Slack's **Apps** section and send:

```text
help
status
reserve
```

For `reserve`, select **Open reservation form**, choose one or more items, select
start and end times, and submit. Slack first reports that the request is queued;
wait for the separate confirmation. Confirm that rows appear in the
`Reservations` tab and that an active reservation is reflected in `Items`.

In an invited channel, test:

```text
@Inventory Bot status
@Inventory Bot reserve
```

You can also open Slack's shortcuts menu and choose **Reserve an item**.

## Updating an existing installation

### Update the Slack manifest

If the Slack app existed before the current modal, shortcut, or scopes:

1. Open the app in the [Slack app dashboard](https://api.slack.com/apps).
2. Open **Features → App Manifest**.
3. Replace the YAML with the current [slack-manifest.yaml](slack-manifest.yaml).
4. Save the changes.
5. Return to **OAuth & Permissions** and reinstall the app if Slack prompts you
   or if any scopes changed.
6. Restart the bot.

### Update the local Python installation

After downloading or pulling new project code, reinstall the editable package so
new dependencies and command metadata are available:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Then restart **Start Inventory Bot.exe**.

### Migrate an older spreadsheet

Stop the bot before migration. Double-click **Initialize Inventory Sheet.exe**
and choose the applicable migration option. The launcher supports migrating the
older `Items` layout, the older `Reservations` layout, or both.

Manual equivalents are:

```powershell
.\.venv\Scripts\python.exe -m inventory_bot.init_sheet --migrate-items
.\.venv\Scripts\python.exe -m inventory_bot.init_sheet --migrate-reservations
```

Each migration copies the existing tab to a timestamped backup before conversion.
The process is safe to rerun when the sheet already uses the current schema.

## Optional configuration

Restrict usage to specific Slack channels or users with comma-separated IDs:

```dotenv
ALLOWED_CHANNEL_IDS=C01234567,C07654321
ALLOWED_USER_IDS=U01234567,U07654321
```

Direct messages bypass the channel allowlist but still honor the user allowlist.

These defaults control the durable local queue and item-picker cache:

```dotenv
RESERVATION_QUEUE_DATABASE=.inventory_bot/reservation_queue.sqlite3
RESERVATION_QUEUE_RATE_PER_MINUTE=4
RESERVATION_QUEUE_MAX_ATTEMPTS=8
RESERVATION_QUEUE_RETENTION_DAYS=30
ITEM_PICKER_CACHE_SECONDS=15
```

Most installations should keep the defaults. Run only one bot instance against
the sheet and keep `.inventory_bot/reservation_queue.sqlite3` on persistent local
storage.

## Troubleshooting

### `Activate.ps1` is not recognized or scripts are disabled

Activation is optional. Use the explicit virtual-environment commands throughout
this guide:

```powershell
.\.venv\Scripts\python.exe -m inventory_bot
```

### `inventory-sheet-init` or `inventory-bot` is not recognized

Install the project from the folder containing `pyproject.toml`:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Then use the module forms, which do not depend on command-path resolution:

```powershell
.\.venv\Scripts\python.exe -m inventory_bot.init_sheet
.\.venv\Scripts\python.exe -m inventory_bot
```

### `No module named inventory_bot`

The project is not installed in that `.venv`, the command is running from the
wrong project copy, or the downloaded copy is outdated. Verify these files exist,
then reinstall:

```powershell
Test-Path .\pyproject.toml
Test-Path .\src\inventory_bot\init_sheet.py
.\.venv\Scripts\python.exe -m pip install -e .
```

### `PermissionError: [Errno 13] Permission denied` for a credential path

`GOOGLE_SERVICE_ACCOUNT_FILE` points to a directory rather than the JSON file.
Change it from a folder such as:

```dotenv
GOOGLE_SERVICE_ACCOUNT_FILE=C:\Users\YourName\Desktop\slack
```

to the full filename:

```dotenv
GOOGLE_SERVICE_ACCOUNT_FILE=C:\Users\YourName\.credentials\slack-inventory-service-account.json
```

### Google returns `403` or permission denied

- Confirm the Google Sheets API is enabled in the project that owns the service
  account.
- Share the spreadsheet with the exact service account `client_email`.
- Grant that email Editor access.
- Confirm the JSON key belongs to the same service account.

If Google Sheets refuses to share with the service-account email, a Workspace
external-sharing policy may be blocking it. Ask the Workspace administrator for
the approved service-account or file-sharing approach; domain-wide delegation is
not otherwise required by this bot.

### Google reports the spreadsheet was not found

Confirm `GOOGLE_SPREADSHEET_ID` contains only the value between `/d/` and `/edit`,
and confirm the spreadsheet is shared with the service account.

### The Slack bot does not respond

- Keep **Start Inventory Bot.exe** running.
- Check that `SLACK_APP_TOKEN` begins with `xapp-` and `SLACK_BOT_TOKEN` begins
  with `xoxb-`.
- Confirm Socket Mode and Event Subscriptions are enabled.
- For DMs, confirm the App Home Messages tab and `message.im` event are enabled.
- For channel mentions, invite the bot to the channel.
- If scopes changed, reinstall the app to the workspace and restart the bot.

### Slack reports `invalid_auth`

The two Slack tokens may be swapped, copied incompletely, revoked, or from a
different Slack app. Copy them again from the app dashboard and update `.env`.

## Security and operation notes

- Never commit `.env`, the service-account JSON key, or Slack tokens.
- If a token or key is exposed, revoke it immediately and create a replacement.
- Share only the inventory spreadsheet with the service account.
- Keep one bot instance running; separate instances do not share the local SQLite
  queue or process lock.
- Do not delete the queue database while requests are pending.
- For production hosting, store credentials in the platform's secret manager and
  prefer Application Default Credentials over a downloaded service-account key
  when running on Google Cloud.
