# Live daily Google Sheets

Google Sheets is a near-live secondary view. The backend's durable database is the primary recording layer. On the current RamNaym design that database is `/app/data/trading.db`, using the separately verified persistent volume. Closing the website does not stop a correctly deployed backend recorder or its upload queue.

This feature is implemented with automated, mocked API tests. Actual owner consent, unattended Google uploads and production restart recovery require live verification. This guide does not claim that an account is already connected.

## One-time server setup

A developer or administrator must configure the application's own Google OAuth client. Do not copy Codex connector tokens into the server.

1. In the owner's Google Cloud project, enable **Google Drive API** and **Google Sheets API**.
2. Configure the consent application and create a **Web application** OAuth client. If its audience is restricted to test users, include the separate Google account you intend to use and review Google's authorization-expiry rules for that publishing status.
3. Register this exact redirect URI:
   `https://personal-options-trader.vercel.app/api/cloud/drive/callback`
4. Confirm that Vercel's `/api` proxy reaches the new ready RamNaym backend. Consent starts and finishes in the same authenticated app session.
5. Set these values in **RamNaym's private backend environment**, never public Vercel variables or GitHub:

   ```dotenv
   GOOGLE_DRIVE_CLIENT_ID=YOUR_WEB_CLIENT_ID
   GOOGLE_DRIVE_CLIENT_SECRET=YOUR_PRIVATE_WEB_CLIENT_SECRET
   GOOGLE_DRIVE_TOKEN_KEY=YOUR_PRIVATE_FERNET_KEY
   GOOGLE_DRIVE_REDIRECT_URI=https://personal-options-trader.vercel.app/api/cloud/drive/callback
   GOOGLE_SHEETS_SYNC_ENABLED=true
   GOOGLE_SHEETS_SYNC_SECONDS=5
   GOOGLE_SHEETS_BATCH_SNAPSHOTS=300
   GOOGLE_SHEETS_MAX_CELLS=9000000
   DRIVE_MAX_FILE_MB=64
   ```

   Keep the token-encryption key in a separate private recovery store. Replacing it without migrating encrypted tokens requires renewed authorization. Generate it only in a trusted private terminal; see [cloud authorization setup](GOOGLE-DRIVE-CLOUD.md).
6. Restart the backend to apply settings. `GOOGLE_DRIVE_BACKUP_ENABLED=true` separately enables the completed individual-trade backup worker; it is distinct from `GOOGLE_SHEETS_SYNC_ENABLED`, which controls live sheets and finalized daily archive uploads.

The current consent request includes both `https://www.googleapis.com/auth/drive.file` and `https://www.googleapis.com/auth/spreadsheets`. Drive file access is restricted to files this app creates or is granted access to; the Sheets scope is broader. Read the actual Google permission screen before accepting.

## Choose your separate 15 GB account

1. Sign in to the paper-trading website and open **Data & settings**.
2. Under **Cloud backups & server health**, choose **Connect Google Drive** (or **Reconnect Google Drive**).
3. Google displays an account chooser. Select the separate free-storage account you intend to use; it can differ from your hosting accounts.
4. Enter Google passwords or verification codes only on Google's page. Review and accept the consent yourself.
5. Return to the app in the same session. Check connection status, then open **Daily Excel Downloads**. Its **Live Google Sheets** panel shows the account email when Google account metadata is available and links to created daily sheets.

Offline authorization allows token refresh on the server while your devices are off, subject to Google's token lifetime, revocation and app publishing rules. The app stores refresh tokens encrypted; it cannot guarantee that Google will never require renewed consent. If the wrong account was chosen before any uploads, reconnect and choose the correct one. If files already exist in another account, pause and review migration rather than expecting those file IDs to transfer automatically.

## How recording and upload differ

- The recorder saves one-second P&L snapshots during verified trading sessions. It retains original quote times and stale/reused indicators.
- The Google worker collects pending snapshots and writes batches no more frequently than the configured interval, with a minimum of five seconds. Under normal conditions a batch can include the preceding five seconds across several trade tabs.
- API delays, quotas, disconnections and large backlogs can make Sheets fall further behind. The five-second setting is an intended batching cadence, not a guaranteed delivery deadline.
- A durable plan stores the exact destination ranges before sending. Google-returned values must match the plan before upload cursors advance. Retrying an interrupted batch writes the same ranges rather than appending duplicate rows.
- The backend limits batch size and payload, uses a singleton lease and persisted exponential backoff, and resumes pending work after restart. A storage-full condition pauses repeated uploads and retains local records while disk capacity remains.

A sheet is named `Paper_Trading_YYYY-MM-DD`. Further documents have `_Part_002`, `_Part_003`, and so on. Each has `Daily_Summary` and separate trade tabs. Live trade-tab names use a stable trade-ID token and broker suffix, such as `Trade_<token>_Kotak`; they need not exactly match the numbered worksheet names in the downloadable daily XLSX. Trade and strategy ID columns link the same lifecycle across dates and broker executions.

Live spreadsheets are created when authorized recorded data is available, not as artificial empty market records. Current limits allocate at most the configured cell allowance per document, with a default of nine million cells and a 200,000-row trade-tab cap; overflow continues in another tab/document. Local capture does not discard seconds to fit Google limits.

Finalized daily XLSX and CSV files are uploaded under `Paper Trading Records / year / month / Daily Reports`. Live spreadsheet documents are app-created Drive files; the current live-document creation code does not place them inside that archive folder. Use the website's **Open sheet** links to find them.

## Status, errors and safe cleanup

In **Daily Excel Downloads**, inspect pending records, last synchronization/error, available spreadsheet links, and account storage when metrics are available. **Data & settings** also shows Google authorization and container resources. These page refreshes are independent of the server recording and upload workers.

If authorization expires, reconnect the same intended account. If storage is full, download and verify completed daily files, then use **Delete Verified Cloud Copies** only after the explicit local-backup confirmation. See the [daily report guide](DAILY-REPORTS.md). Emptying Google Drive Trash is a separate permanent action. Do not delete source database files to resolve a Google quota problem.

Never manually delete unsynchronized rows, pending batches or credentials from SQLite to make a warning disappear. Keep enough local storage for the backlog, respond to resource warnings, and check that pending records decrease after recovery. No external email/SMS alerts are configured merely by enabling this worker.

## Live acceptance checks

Before describing this as working unattended, record the results of owner consent, a real recorded batch with correct seconds and trade IDs, readback verification, a lost-acknowledgement retry, closed-browser capture, a container restart with pending work, finalized daily file checksums, and a deliberately rejected unsafe-cleanup request. Broker connections require their own authenticated live tests. Mocked Google API tests do not prove production account access.
