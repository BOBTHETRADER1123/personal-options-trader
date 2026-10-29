# Excel reports and the laptop companion

The current cloud service is RamNaym #1606 with persistent SQLite. This guide covers the optional individual-trade laptop companion; use [Daily Excel Downloads](DAILY-REPORTS.md) for complete daily one-second workbooks and [cloud backups](GOOGLE-DRIVE-CLOUD.md) for unattended Google uploads. Historical broker-labelled files remain readable; new trades use Kotak Neo only.

Every completed paper trade has its own export job, workbook and immutable manifest. Dhan, Kotak Neo and 5paisa reports are separated by broker, year, month and trade ID. All report times use **Asia/Kolkata**. Sample-mode reports state that prices and fills are simulated.

The closing transaction queues a compact trade summary. A background worker generates Excel without delaying the trading request. Workbook bytes stay in the database until collected; temporary runtime files are not their only copy. New summary jobs omit raw quote and timeline arrays. Report lists read metadata without loading full workbooks or histories.

## What each workbook contains

- **Trade Summary:** final gross/net result and charges; maximum profit and maximum loss actually reached with IST times; signed highest/lowest observed net P&L (MFE/MAE); entry/closing times and sample/live-data origin.
- **Strategy Legs:** symbol, expiry, strike, CE/PE, direction, lots, contract multiplier and average entry/exit prices.

New files omit individual price updates and one-second timelines. Existing detailed workbooks remain readable and are not rewritten. Extrema include estimated charges and only reflect received marks/actions. If a trade never became profitable, maximum profit is zero with no invented profit timestamp; the signed highest net P&L and its time remain available separately.

The connected [Google Drive backup workflow](GOOGLE-DRIVE-BACKUPS.md) saves available completed laptop reports before storage fills. It never deletes source records and does not reset Neon's monthly public network-transfer allowance. The current RamNaym SQLite workspace is independent of that legacy Neon outage.

## Set up automatic saving on your laptop

1. Finish the main Windows installation in the project README. Keep this entire project folder on your laptop.
2. Open the **companion** folder inside the project. Double-click **start-companion.bat**.
3. On the first run, enter your backend URL. For an app running only on this laptop, press Enter to use `http://127.0.0.1:8000`. For the current cloud app, enter `https://libzw8xd976f.ramnaymcloud.com`, without `/api`.
4. Choose the folder for your reports, or press Enter to use `Documents/PaperTrading/Trades`. The setup saves a private configuration file called `companion/.env` on your laptop.
5. Setup displays a **hash**. For local use, open the project's root `.env` file in Notepad and put the displayed hash after `COMPANION_TOKEN_SHA256=`. For the cloud app, add the same setting to RamNaym #1606's private environment settings. Do not put the raw companion token in Vercel, GitHub or browser settings.
6. Restart the backend after setting the hash. Leave the companion window running. It makes an outbound request to your backend approximately every ten seconds; it does not open a public port on your laptop.
7. Complete a paper trade. Open **Reports** in the website. It should first show the job as queued/preparing, then ready for your laptop, and finally saved after verification.
8. Open the chosen folder. The workbook is inside a broker/year/month/trade subfolder, for example `Kotak Neo/2026/10/Trade_<id>/NIFTY_<id>_<closing-time>_IST.xlsx`. The dates come from the actual closing time.

For Windows to launch the companion when you sign in: press **Windows+R**, type `shell:startup`, press Enter, then put a shortcut to `start-companion.bat` in that folder. Your laptop must be awake and online to save files. When it is offline, the backend retains the export jobs and retries when the companion reconnects.

To change the save folder later, stop the companion, edit `TRADES_FOLDER` in `companion/.env`, and start it again. Use forward slashes in that value. Keep the `.companion/manifest.sqlite3` file in its old folder until all pending acknowledgements have completed; moving folders does not move existing reports.

## How verification prevents data loss

The companion first claims a job with an expiring claim token. It downloads over HTTPS, checks the SHA-256 checksum and size, writes a temporary file, flushes the write, and reopens the entire workbook with all required sheets. It then saves the final file and independently verifies that final file. Only then does it acknowledge the job to the backend. A normal browser download does **not** count as a verified laptop save.

A local SQLite manifest records unfinished acknowledgements. If the laptop saves a file and the connection drops before acknowledgement, the next run reopens and checks the existing file, then sends the acknowledgement again. It does not create another trade or another export job. A file with the expected name but a different checksum is not overwritten; the job remains retryable.

The backend's own database persists pending and generating jobs. A generation lease abandoned by a server restart becomes eligible for recovery after fifteen minutes. Temporary errors retry with increasing delay; **Retry** on the Reports page requests another attempt sooner. Never delete the database to fix an export error.

## Optional online journal cleanup

Cleanup is **off by default**. Keep it off while learning the app and while testing your own laptop. A verified acknowledgement alone does not enable cleanup.

If you explicitly enable **Delete archived journal rows after verified laptop save** in Settings, the backend can remove only the completed trade's own detailed event and timeline rows after the verified acknowledgement, in the same database transaction. It retains the trade's summary, the permanent export manifest, and the verified cloud workbook. Shared quote-journal records are retained because another trade may rely on them. Active trades, account settings, broker configuration and unrelated records are not deleted.

This setting is a conservative journal-compaction option, not a complete erasure of all online trading data. Keep a second backup of your laptop reports before enabling it. If the file is unavailable, invalid or unacknowledged, the source journal is retained.

## Daily reconciliation

The backend refreshes a durable reconciliation record for each IST calendar date while it is running. The **Reports** page can request the current report, including pending, saved and failed counts and any saved job lacking verification. It includes older outstanding jobs too, so an offline laptop does not hide yesterday's reports. The report is also available to the authenticated owner at `/api/exports/reconciliation`.

Free backend hosting that sleeps cannot generate reports or capture market feeds while asleep. Durable queued work resumes after the backend restarts, but quote events that the broker never delivered while it was stopped cannot be reconstructed. Continuous market recording requires an awake backend and the broker's supported feed access.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Reports stay ready, laptop offline | Start the companion and check that the laptop has internet access. Cloud reports are retained. |
| Companion is not paired | Set the displayed SHA-256 hash on the backend and restart that backend. |
| Invalid companion token | Ensure `companion/.env` is from the same pairing. Rerun `configure.py` only if you want a new pairing; update the backend hash afterward. |
| HTTPS required | Use the Render HTTPS URL. Plain HTTP is accepted only for an actual loopback backend on this laptop. |
| Workbook generation failed | Use Retry. Check database availability and storage; the source journal remains queued. |
| File is locked or cannot be verified | Close that workbook in Excel and retry. Check the chosen folder's permissions and free space. |
| Existing file has a different checksum | Keep that file. Move it to a backup folder yourself, then retry. The companion does not overwrite it. |
| Claimed job after a crash | Restart the companion using its original folder; its local manifest can resume. An abandoned cloud claim expires after fifteen minutes. |
| Lost laptop reports | Download the retained cloud workbook from Reports if available; restore your laptop backup. Do not enable online cleanup until your backups have been checked. |

The automated reporting tests exercise offline queues, workbook-generation failure, duplicate enqueue and acknowledgement, expired worker leases, checksum rejection, local restart after save-before-ack, invalid workbook rejection, path traversal, and cleanup gating. Real Windows login-startup behavior, a real Render/Neon deployment and your laptop's own file-locking conditions still need a user-environment check.
