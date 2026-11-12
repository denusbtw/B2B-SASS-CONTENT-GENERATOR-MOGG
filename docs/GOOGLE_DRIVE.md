# Google Drive archive

The optional Drive worker uploads completed MP4 packages to an app-managed folder
named **AI Video Publisher** in the Google account you choose. The app requests
only `https://www.googleapis.com/auth/drive.file`. It never changes Drive sharing
permissions. The operator page's **Google Drive uploads** view shows the file ID,
link, and last error. A Drive failure leaves the local video and publishing flow
available.

## Set up Google Cloud

1. Open [Google Cloud Console](https://console.cloud.google.com/) and select or
   create a project for this Drive integration. Keep its OAuth client separate
   from any YouTube client.
2. In **APIs & Services → Library**, find **Google Drive API** and click **Enable**.
3. Open **Google Auth Platform** and set up **Branding** and **Audience**. For
   a personal Gmail account select **External**. If the app is in **Testing**,
   add your Google account as a test user. Under **Data Access**, configure only
   the Drive `drive.file` scope. External apps in Testing can receive refresh
   tokens that expire after seven days. Review Google's production publishing
   requirements before relying on unattended long-term access.
4. In **Clients**, choose **Create client → Desktop app**, then download its
   OAuth JSON. The CLI uses a loopback redirect and PKCE. See
   [Google's desktop OAuth instructions](https://developers.google.com/identity/protocols/oauth2/native-app).

## Authorize once on Windows

Run from the repository root in PowerShell. Keep both files under your private
Windows profile and out of shared folders, backups you do not control, and Git.
The CLI stores the refresh token in a separate file. The access token stays in
worker memory. On POSIX the CLI creates the token with mode 0600. On Windows
the directory and file inherit the current profile's access controls.

```powershell
$env:DRIVE_SECRETS_DIR = Join-Path $env:LOCALAPPDATA 'AIContentFactory\drive'
New-Item -ItemType Directory -Force -Path $env:DRIVE_SECRETS_DIR | Out-Null
$env:DRIVE_OAUTH_CLIENT_FILE = Join-Path $env:DRIVE_SECRETS_DIR 'client.json'
$env:DRIVE_OAUTH_TOKEN_FILE = Join-Path $env:DRIVE_SECRETS_DIR 'token.json'
# Save the downloaded desktop client JSON as client.json in that folder.
.\.venv\Scripts\python.exe -m factory.drive_oauth
```

Open the URL printed by the CLI, choose the intended account, and grant consent.
Leave the terminal open until the loopback callback confirms it. Then start the
optional worker from the same terminal:

```powershell
docker compose --profile drive up --build -d --wait
docker compose logs -f drive
```

Once enabled, the worker picks up existing and future completed packages
(including each part of a multipart video). Identical MP4 bytes are uploaded
once. Drafts, failed QA packages, and jobs blocked on rights are excluded.
The first upload finds a matching app-managed folder or creates one and saves
its ID in PostgreSQL. A manually created folder with the same name is not
adopted. The app filters folder discovery by its own marker and does not search
for user-created folders. If the saved folder is trashed, renamed, or inaccessible,
the worker stops sending files until that destination is restored. Back up the
database along with the media. Reconnecting the same account preserves the folder
and upload ledger. To use a different Google account, use a separate database
and archive configuration.

### Configuration

- `DRIVE_SECRETS_DIR` is the Compose host directory containing `client.json`
  and `token.json`. If omitted, Compose looks under `data/drive-secrets`.
  Only the Drive container mounts this directory, read-only.
- `DRIVE_OAUTH_CLIENT_FILE` and `DRIVE_OAUTH_TOKEN_FILE` are optional host CLI
  file overrides. Compose sets fixed container paths for these two variables.
- `DRIVE_FILENAME_TEMPLATE` defaults to `{job_id}-part-{part}.mp4`. It accepts
  `{job_id}`, `{part}`, and `{sha256}`, and applies to future uploads.
- `DATABASE_URL` is required for standalone `python -m factory.drive_worker`
  and must point at a migrated PostgreSQL database. Compose sets it automatically.
- Leave off `--profile drive` on an installation where the integration should
  stay disabled. To stop a Drive worker you previously enabled, explicitly run
  `docker compose --profile drive stop drive`.

Transient upload errors are retried with backoff. The worker verifies the saved
folder and uses preallocated file IDs to reconcile uncertain uploads. A
failed chunk triggers a resumable-session status request. A later attempt
checks the same file ID first. The worker never publishes the file to the web.

## Reconnect or revoke

For expired or revoked consent, rerun the authorization CLI with `--replace` for
the same Google account, then run `docker compose --profile drive restart drive`.
To revoke access, first stop the worker, then remove the app's access under
[Google Account connections](https://myaccount.google.com/connections). Delete
the local `token.json` after revoking. Existing Drive files remain in the folder.

The automated suite uses mocked Google endpoints. A live upload requires your
Google Cloud setup and browser consent.
