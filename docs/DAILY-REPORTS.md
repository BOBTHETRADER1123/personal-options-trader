# Daily Excel Downloads

This guide describes the implemented daily-report workflow. It does not certify that a particular cloud deployment or Google account has passed live testing. Check [test evidence](../TEST-REPORT.md) and the [current deployment guide](RAMNAYM-TRIAL.md).

## What a daily report contains

Dates use **Asia/Kolkata (IST)**. A broker-specific trade has its own worksheet, such as `Trade_001_Kotak` or `Trade_002_5paisa`. Mirrored broker executions remain separate and share a strategy ID. A position held over several dates keeps its original trade ID; subsequent daily worksheets are marked `Continued`.

Each workbook includes `Daily_Summary`, trade identity and lifecycle information, and the observations actually captured for that date. Columns include original quote timestamps, quote age, stale/reused status, quantities, leg P&L, strategy P&L, costs, estimated margin, theoretical payoff and observed P&L extrema when those values were captured. Missing historical values are not recreated from today's prices. Estimated costs and margin are not official broker figures.

**Snapshot count** counts trade-seconds. **Record count** counts leg rows: a two-leg strategy can produce two rows for a single snapshot. These counts need not match. A trade with no recorded observations gets an explicitly empty worksheet; the app does not invent seconds. Recording gaps are retained in the report manifest.

Preparation does not close a position. Finalization waits for the relevant verified exchange sessions to end and checks that the captured source has not changed during generation. An unknown calendar prevents finalization rather than guessing a closing time.

## Download a completed trading day

1. Sign in to [your website](https://personal-options-trader.vercel.app/) after the deployed backend is ready.
2. Open **Daily Excel Downloads** in the sidebar.
3. Select an IST trading date and choose **Prepare daily export** if it has not already been queued automatically. The server runs preparation independently of this page.
4. Wait for `finalized`. If the session is still open or an error is shown, retain the source records and resolve that condition first.
5. Choose **View verified files** to load the full manifest when necessary.
6. Download **every Excel part**, its **CSV.gz** supplement, and the **Master index** when the day has multiple workbook parts. Filenames follow `Paper_Trading_YYYY-MM-DD_Part_001.xlsx` and `.csv.gz`; a split day also has `Paper_Trading_YYYY-MM-DD_Index.xlsx`.
7. Save the files to a clearly named Windows folder. Reopen the workbooks, inspect `Daily_Summary`, and check that all trade worksheets and numbered parts are present.

Before offering a file for saving, the website compares its received byte size and SHA-256 checksum with the server manifest. That verifies the download bytes, not that Windows successfully saved a lasting copy. The server separately checks generated worksheet row counts and coverage. Keep another independent verified copy before removing cloud copies.

To recheck a saved file in PowerShell, use its actual path:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'C:\Users\YOUR-NAME\Downloads\Paper_Trading_2026-10-26_Part_001.xlsx'
```

Compare the result with that file's checksum under **integrity details**. Each file has its own checksum. The **export checksum** used for cleanup identifies the complete manifest and is different from an individual XLSX checksum.

## Large trading days

The backend generates XLSX itself; it does not rely on Google's native spreadsheet export. Large trade histories continue into numbered worksheets/workbooks. Every generated workbook part has a matching compressed CSV supplement. A master index lists file checksums and trade-sheet coverage when multiple workbook parts are needed.

Current configurable backend limits are:

```dotenv
DAILY_EXPORT_MAX_CELLS=1000000
DAILY_EXPORT_CPU_PERCENT=35
DAILY_EXPORT_MAX_FILE_MB=64
DAILY_EXPORT_SHEET_DATA_ROWS=100000
```

These limits control report size, not how many source records may be silently discarded. If safe generation fails, the day remains incomplete with an error and its source rows remain stored. Raising limits can increase server memory and disk use; do not do so without measuring the actual workload.

## Verify a local backup and clean cloud copies

1. Finish downloading, reopening and checking every listed file. Keep an additional independent copy.
2. Choose **Delete Verified Cloud Copies** for the finalized date.
3. Read the displayed record count and export checksum. Enter that exact **export checksum**.
4. Tick the statement confirming that all completed files were saved, reopened and checked, then choose **Verify local backup**.
5. Review the warning and separately choose **Delete Verified Cloud Copies** only if you intend cleanup.

The server refuses cleanup if the export is not finalized, its checksum/count changed, snapshots or Google write batches remain unsynchronized, required cloud archives are unverified, or duplicate cloud files need review. It only moves app-owned copies to **Google Drive Trash**. This button does not delete active positions or SQLite history. Trash is recoverable until you separately empty it in Google Drive; emptying Trash is permanent. Account storage includes Gmail, Photos and other files, so deleting these reports does not imply zero account usage.

A download click is never treated as proof of a saved local backup. A laptop copy alone does not protect against laptop loss or failure. Daily trade reports also do not replace a private, consistent database recovery backup containing settings and pending trading state.

## Automatic server-history retention

The separately configured [12 GB archival worker](GOOGLE-DRIVE-CLOUD.md#automatic-local-history-archiving-at-12-gb) can remove eligible older one-second snapshot detail only after the complete finalized XLSX/CSV exports and Google copies have been verified. It keeps each trade/date's last snapshot, all active position state, trade summaries and export bytes. Downloading or cloud-file cleanup alone does not trigger this policy. Partial local pruning has a durable receipt and must complete before cloud-copy cleanup is allowed.

Archived daily files remain the complete history even after source detail is pruned. The website does not manufacture the removed seconds from the continuity anchor. Keep all export parts and their checksums in independent storage before separately trashing cloud copies.

## Live sheets and recovery

See [live Google Sheets setup](GOOGLE-SHEETS-LIVE.md). Google authorization is needed for uploads, not for generating or downloading a local daily workbook. Unavailable Google services leave captured records on the durable server database and pending work queued. Export leases and retry state support restart recovery; real-host restart and Google-account checks must still be recorded separately in the trial evidence.

## Export resource controls

Daily exports renew their fenced ownership lease while building and verifying files. Large parts split at a default one million cells. ZIP members and compressed CSV are processed in chunks to avoid holding full uncompressed copies in memory. The export thread targets 35% CPU through cooperative pauses; this is not a hard container cap, and brief compression bursts can still peak higher. Heavy export work pauses while active market recording needs the server, then resumes with the same job ownership. Source snapshots and unfinished parts remain available after interruptions.
