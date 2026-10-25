# Google authorization and cloud trade archives

The current design uses Vercel, RamNaym server 1606 and verified local SQLite at `/app/data/trading.db`. Neon and the original laptop records are preserved separately for recovery. This document describes implemented behavior and setup; it is not evidence that Google consent or production uploads have already passed.

The older laptop companion and its existing `Personal Options Trader Backups` folder are separate. The cloud application uses its own owner-authorized Google OAuth client. It never copies Codex connector tokens into the backend.

## Two related cloud services

- **Completed individual trades:** `GOOGLE_DRIVE_BACKUP_ENABLED=true` enables a durable job queue for existing per-trade Excel summaries and any available compressed one-second CSV history.
- **Daily sheets and files:** `GOOGLE_SHEETS_SYNC_ENABLED=true` enables approximately five-second Google Sheets batches and verified uploads of finalized daily XLSX/CSV artifacts. See [live Sheets](GOOGLE-SHEETS-LIVE.md) and [Daily Excel Downloads](DAILY-REPORTS.md).

The two switches are independent. Connecting Google is not the same as enabling either worker. Generating daily exports and downloading them from the backend does not require Google authorization.

Both services run in the FastAPI server lifetime, independently of an open browser or laptop, when the cloud backend is healthy. Queue state, retry times, reserved file IDs, source checksums and verification evidence are durable in the application database. Conditional claims/leases recover interrupted work. No app source record is deleted by an upload worker.

## Configure owner authorization

1. Enable Google Drive API and Google Sheets API in the owner's Google Cloud project and configure its consent application.
2. Create a **Web application** OAuth client. The app requests only `https://www.googleapis.com/auth/drive.file`, restricting access to files this app creates or that you select for it. The Sheets API also accepts this scope for updating those app-authorized spreadsheets; the broader `spreadsheets` scope is not requested.
3. Register the exact callback `https://personal-options-trader.vercel.app/api/cloud/drive/callback`. Vercel must already proxy `/api` to the new ready RamNaym backend.
4. Store these in RamNaym's private environment, never in frontend variables, public source or chat:

   ```dotenv
   GOOGLE_DRIVE_CLIENT_ID=YOUR_WEB_CLIENT_ID
   GOOGLE_DRIVE_CLIENT_SECRET=YOUR_PRIVATE_WEB_CLIENT_SECRET
   GOOGLE_DRIVE_REDIRECT_URI=https://personal-options-trader.vercel.app/api/cloud/drive/callback
   GOOGLE_DRIVE_TOKEN_KEY=YOUR_PRIVATE_FERNET_KEY
   DATA_DIR=/app/data
   DRIVE_SPOOL_MAX_MB=256
   DRIVE_MAX_FILE_MB=64
   DRIVE_MIN_FREE_MB=512
   DRIVE_MAX_PENDING_JOBS=1000
   ```

   Generate the encryption key once in a trusted private terminal:

   ```sh
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

   Keep it in the server's private settings and a separate private recovery store. Replacing it without migrating encrypted tokens requires reconnecting Google.
5. Enable the desired worker switches, restart the backend, sign in, and open **Data & settings -> Cloud backups & server health -> Connect Google Drive**.
6. Choose the intended separate 15 GB account on Google's account chooser and review its actual permission screen. Complete consent in the same authenticated app session.
7. Verify both connection state and an actual new upload before calling the integration complete.

Authorization uses one-use state tied to the owner's session, a ten-minute state expiry, PKCE and the client secret. Refresh tokens and PKCE verifiers are encrypted in the database; access tokens stay in server memory. Offline access supports unattended refresh but can expire or be revoked. An External consent app left in **Testing** receives a seven-day refresh token for these file-access permissions. Add the selected account as a test user for trial checks, then review the appropriate publishing configuration before relying on longer unattended operation. Publishing does not guarantee a token will never expire or be revoked. See [Google's refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration). Reconnect when requested. Keep request query strings, codes, socket tickets and tokens out of logs.

## Change the backup Google account

Use **Change or reconnect Google account** in Data & settings and choose the new account on Google's page. The callback verifies the account identity before replacing encrypted authorization. Reauthorizing the same account retains its file IDs and synchronization cursors. Choosing a different account archives the previous account's synchronization metadata locally, resets the current account-bound queues, and starts using new app-created files in the new account. Existing remote files stay in their original account; this does not transfer ownership or delete them. Owner settings, trades and captured history are preserved. Older details already archived out of SQLite remain in their verified exports; they are not invented again to fill a new live sheet.

Google operations and account changes share one operation lock so an in-flight upload cannot write old-account destinations into new-account state. Consent still requires your review on Google's page. If the previous connection has metadata but no verifiable account identity, changing it is blocked for review rather than guessing the account.

## Individual-trade archive verification

Individual completed-trade files use the app-managed hierarchy:

```text
Paper Trading Records/
  2026/
    October/
      Kotak Neo/
        <trade>_summary.xlsx
        <trade>_history.csv.gz
      5paisa/
        <trade>_summary.xlsx
        <trade>_history.csv.gz
```

The immutable export manifest supplies actual filenames. The compact individual-trade workbook summarizes final P&L, observed maximum profit/loss, legs and costs. Available one-second history is retained in a separate CSV.gz. Old compact trades have no invented historical rows. The new full daily workbook is a separate reporting workflow.

Uploads reserve Drive file IDs before sending and inspect those IDs plus app-specific archive keys on retry. Independent readback checks parent folder, filename, MIME type, size and MD5. If Google omits MD5, the backend downloads the binary through its authorized connection and compares size, MD5 and SHA-256. HTTP success alone is not verification.

Failures retain source data and pending work with bounded retries. The individual-trade worker backs off up to one hour. Temporary files under `/app/data/cloud-backup-spool` are bounded by file size, total staging allowance and minimum free disk space; only verified temporary staging bytes are discarded. Completed individual-trade archival does not automatically remove source trading history or old Drive archives.

The daily cleanup workflow is separate and requires explicit acknowledgement of a verified local backup. It only trashes eligible app-owned daily copies after all safeguards pass. See [daily cleanup](DAILY-REPORTS.md#verify-a-local-backup-and-clean-cloud-copies).

## Automatic local-history archiving at 12 GB

This is a separate, opt-in retention worker; uploading a file alone does not enable deletion. The configured trigger is 12 GiB of application data, with a 10 GiB target, on the 15 GB plan. The dashboard uses GB labels for these binary byte thresholds. Configure only after Google authorization and real upload verification have passed:

```dotenv
AUTO_ARCHIVE_ENABLED=true
AUTO_ARCHIVE_TRIGGER_GB=12
AUTO_ARCHIVE_TARGET_GB=10
AUTO_ARCHIVE_INTERVAL_SECONDS=60
```

At the trigger the backend works through older finalized IST dates. It requires unchanged daily manifests, complete Excel and compressed-CSV coverage, synchronized Google Sheets records and fresh remote checksums/size/parent checks for every required archive part. It refuses cleanup if any required file is missing, trashed, changed or unverified. Google failure, insufficient Google space, missing authorization or unsent records leave the source details intact and produce a visible blocked/retry status.

Only eligible older `PnlSnapshot` detail is removed, in bounded transactions outside active recording. A durable receipt tracks each committed batch so interruption can resume without duplicate deletion. The last snapshot for each trade/date remains as a continuity anchor. Accounts, credentials, settings, active positions, orders, trade summaries, original quote/timeline journals, daily export bytes and verification evidence remain. This never clears the whole database, and does not delete the old Neon database.

SQLite normally reuses freed database pages; its physical file may remain the same size. The status reports reusable space separately and the archiver subtracts reusable SQLite pages when deciding whether more eligible detail needs archiving. It does not run a full-volume `VACUUM` near capacity. A disk-headroom check can stop cleanup safely; retained export bytes, original journals or other files may still require capacity review. The 10 GiB target is not a guarantee that enough eligible history exists to reach it.

The automatic local-history worker does not trash the verified Google copies. **Delete Verified Cloud Copies** is a separate explicitly confirmed action after a verified local backup. Preserve an independent copy before removing cloud archives.

## Monitoring and recovery

Open **Data & settings** to inspect cloud jobs, authorization errors, CPU/RAM/disk samples and resource notifications. Threshold changes create dashboard notifications and server log entries; external email/SMS is not automatically configured. The current production storage quota setting is `PERSISTENT_STORAGE_LIMIT_GB=15`; filesystem free space and provider plan capacity are distinct measurements.

Pending uploads survive a normal restart only while the persistent database and encryption key survive. A single provider volume is not an independent disaster-recovery backup. Keep verified exports and a separately encrypted consistent database recovery backup. Do not copy a live SQLite file alone; use SQLite's backup API and test restoration on a stopped isolated instance.

Owner-authenticated endpoints include `/api/cloud/drive/status`, `/api/cloud/drive/queue`, `/api/cloud/drive/retry/{job_id}`, `/api/sheets/status` and `/api/system/resources`. Lightweight `/health/live` does not query the database. Readiness and actual broker/Google connectivity are separate conditions.

## Validation status

Automated mocked tests cover authorization protection, encrypted tokens, retry/verification behavior, interrupted Google writes and cleanup safeguards. Actual owner consent, authenticated broker feeds, uploads with the website closed, restart recovery, CPU/RAM peaks and API response times must be recorded during live trial checks. See [test evidence](../TEST-REPORT.md). Archiving or deleting stored rows never resets Neon's monthly transfer allowance; the new SQLite runtime avoids that dependency while preserving the old database.

## Official configuration references

- [Google web-server OAuth and offline access](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Google Sheets batch-write authorization scopes](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchUpdate#authorization-scopes)
- [Drive uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [Drive file metadata](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
