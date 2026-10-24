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
2. Create a **Web application** OAuth client. The app requests:
   - `https://www.googleapis.com/auth/drive.file`
   - `https://www.googleapis.com/auth/spreadsheets`
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

Authorization uses one-use state tied to the owner's session, a ten-minute state expiry, PKCE and the client secret. Refresh tokens and PKCE verifiers are encrypted in the database; access tokens stay in server memory. Offline access supports unattended refresh but can expire or be revoked. Review Google's app publishing and token rules and reconnect when requested. Keep request query strings, codes, socket tickets and tokens out of logs.

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

## Monitoring and recovery

Open **Data & settings** to inspect cloud jobs, authorization errors, CPU/RAM/disk samples and resource notifications. Threshold changes create dashboard notifications and server log entries; external email/SMS is not automatically configured. The current production storage quota setting is `PERSISTENT_STORAGE_LIMIT_GB=15`; filesystem free space and provider plan capacity are distinct measurements.

Pending uploads survive a normal restart only while the persistent database and encryption key survive. A single provider volume is not an independent disaster-recovery backup. Keep verified exports and a separately encrypted consistent database recovery backup. Do not copy a live SQLite file alone; use SQLite's backup API and test restoration on a stopped isolated instance.

Owner-authenticated endpoints include `/api/cloud/drive/status`, `/api/cloud/drive/queue`, `/api/cloud/drive/retry/{job_id}`, `/api/sheets/status` and `/api/system/resources`. Lightweight `/health/live` does not query the database. Readiness and actual broker/Google connectivity are separate conditions.

## Validation status

Automated mocked tests cover authorization protection, encrypted tokens, retry/verification behavior, interrupted Google writes and cleanup safeguards. Actual owner consent, authenticated broker feeds, uploads with the website closed, restart recovery, CPU/RAM peaks and API response times must be recorded during live trial checks. See [test evidence](../TEST-REPORT.md). Archiving or deleting stored rows never resets Neon's monthly transfer allowance; the new SQLite runtime avoids that dependency while preserving the old database.

## Official configuration references

- [Google web-server OAuth and offline access](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Google Sheets authorization scopes](https://developers.google.com/workspace/sheets/api/scopes)
- [Drive uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [Drive file metadata](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
