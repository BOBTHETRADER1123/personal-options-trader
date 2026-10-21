# Operating and checking your app

## What runs when the website is closed

The backend owns broker feeds, journal writes, one-second snapshots, export jobs and daily reconciliation. The desktop shortcut and **Open-Trading-App.cmd** start the backend and paired companion in the background. Closing the browser leaves them running while the laptop is awake. **Stop-Trading-App.cmd**, closing the laptop, or sleeping cloud compute stops capture. If using the optional **Start-Windows.cmd** console, closing that window stops its backend too. Pending durable jobs resume after restart, but prices never received during an outage cannot be recreated.

The companion owns laptop filesystem writes. It must be running on an awake laptop to save files. It does not need the website to be open. No browser website can silently write to an arbitrary laptop folder.

Use exactly one backend worker/instance for this personal deployment. The application serializes paper actions and quote processing. Running multiple replicas without a distributed feed-owner lease can create duplicate subscriptions and is outside the included deployment configuration.

## Back up local records

1. Open the main project folder and double-click **Backup-Local.cmd**.
2. It creates a timestamped SQLite backup in the **backups** folder and verifies its database integrity. This uses SQLite’s backup API, so committed WAL records are included.
3. Copy the verified backup to a separate secure drive. Also back up your completed Excel archive and the companion’s `.companion/manifest.sqlite3`.
4. Keep private `.env` configuration separately in a password manager or other private backup. Never attach it to GitHub or reports.

Do not back up a running SQLite application by copying only `trader.db`: recently committed journal rows may still be in its WAL file.

## Restore a local backup

1. Double-click **Stop-Trading-App.cmd** to stop the app and companion started by the launcher. If using separate console windows, stop the backend with **Ctrl+C** and close the companion too.
2. Double-click **Restore-Local.cmd**. Enter the full backup path.
3. Read the selected path and type **RESTORE** only if it is the correct backup. The helper validates the source and keeps a separate verified before-restore backup.
4. Start the app again. Use the owner password stored in that backup.
5. Open Excel reports and run reconciliation. A restored database can contain older export states; verified existing laptop files can be acknowledged idempotently.

The helper is intended for the standard local port 8000. If you run another port, stop that process too before restoring. It does not restore a remote PostgreSQL database.

## Back up and recover Neon PostgreSQL

Use the retention, restore and branch controls available on your actual Neon plan. Check the retained history window before relying on it. A paid option may be required for a longer recovery period. For an independent export, use the PostgreSQL `pg_dump` tool from a trusted installation and keep the resulting dump private. Never put a connection string in a screenshot or public command transcript.

Before a production restore: stop the backend and companion, take a backup of the current database, restore to a separate branch/database, verify trade counts and export manifests, then point the stopped backend at the verified database. Restart one backend instance and reconcile queued jobs before resuming capture.

An optional developer helper also supports a checksum-verified logical backup including report binary data. From the `backend` folder, with the intended private `DATABASE_URL` configured, use `python -m app.maintenance backup ../backups/journal.json.gz`. Restore requires `python -m app.maintenance restore ../backups/journal.json.gz --server-stopped`; it creates a safety backup before replacing rows transactionally. The flag is an operator acknowledgement: actually stop every backend and companion first. Prefer restoring into a separate database for cloud verification. The Windows buttons above remain the simpler local SQLite route.

## Journal and Excel recovery

- SQLite uses WAL, foreign keys and full synchronous commits. PostgreSQL stores the same trade/event/job tables in the configured Neon database.
- Live quote receipt is committed before processing. A durable pending-receipt table allows unfinished revaluation work to replay at startup. A stale event can still be preserved without treating it as a valid fill.
- Confirmed multi-broker actions use a shared idempotency key and database transaction. Retry the same network request with the same key; do not repeatedly create new actions after an uncertain response.
- Completion queues the immutable report payload in the closing transaction. The report worker generates a workbook separately and keeps its bytes/checksum in the database.
- A failed or locked convenience file does not discard the journal or its cloud report. Use Reports → Retry. For the background launcher, inspect `.desktop/companion.log` in the project folder; for a manually started companion, check its window.
- Crashed worker/companion claims expire and can resume. Restarts do not intentionally sample away received events. Workbook sheets split before Excel’s row limit.
- The detailed event journal is authoritative. A workbook contains observed updates and snapshots, not a guarantee of every exchange event or every millisecond.

## Sessions and access

Local development uses HTTP on loopback and a non-secure local cookie. The cloud template uses HTTPS, secure HttpOnly cookies and explicit allowed origins. The Vercel API proxy keeps browser sessions on the website’s origin; WebSocket access uses short-lived single-use tickets tied to that session. Logging out or changing a password revokes the session.

Optional owner two-factor login uses a backend-only `OWNER_TOTP_SECRET`. Set it only after enrolling the same secret in your own authenticator. Restart the backend, then check that a correct current six-digit code is required. This is independent of a broker’s TOTP. Keep a recovery copy of the secret privately. The app does not provide email password recovery or a recovery-code service.

Never bind a local unauthenticated companion port publicly, turn off TLS validation, or place broker secrets in any `VITE_` variable. Broker API permissions, token validity and subscription/IP limits remain controlled by the broker.

## Beginner acceptance checklist

### Local paper engine

- [ ] Create your own password and sign in; a wrong password is rejected.
- [ ] Sample badge remains visible on sample prices, contracts and reports.
- [ ] Select contracts from dropdowns. Shift strikes with minus/plus.
- [ ] Make a two-leg strategy and select all three paper accounts. Confirm once; see three separate results.
- [ ] Increase lots and check the weighted average entry.
- [ ] Partially exit one leg, then close the rest. Check realized and unrealized results.
- [ ] Add a leg, edit an assumption and inspect the audit trail.
- [ ] Submit a limit simulation that does not cross the current quote; cancel its pending order.
- [ ] Duplicate a strategy and confirm it has a new ID.
- [ ] Compare a known call spread or iron condor with its expiry payoff.
- [ ] Check that mixed-expiry risk is marked unsupported rather than zero.
- [ ] View MFE/MAE times, closing P&L/time, and profit giveback on a closed trade.

### Reports and companion

- [ ] Every completed broker trade has its own workbook and unique closing timestamp in its filename.
- [ ] Open its summary, timeline, events, legs and market-update sheets.
- [ ] Check IST timestamps, lots, monetary units, charges and sample/live source labels.
- [ ] Stop the companion, complete another trade, and verify the queue retains it.
- [ ] Restart the backend, then the companion; verify the file is saved once and acknowledged.
- [ ] Keep Excel open on a saved workbook and verify a retry does not overwrite an unrelated file.
- [ ] Run reconciliation. Pending, saved and failed jobs should match the dashboard.
- [ ] Leave online cleanup off until your own offline, error, restart and duplicate retry checks pass.

### Your real accounts and cloud setup

- [ ] Import each genuine broker master and configure subscribed contracts.
- [ ] Verify source tokens, expiry, lot size, and MCX monetary multipliers against official broker/exchange specifications.
- [ ] Confirm sessions, genuine quotes, reconnect/resubscribe behavior and token expiry using your own broker account.
- [ ] Disconnect a feed and check stale warnings appear; stale prices must not fill new market simulations.
- [ ] Verify every received source event is journaled under your actual expected load and database quota.
- [ ] Open the same HTTPS website on Android and Windows. A new paper trade on one must appear on the other.
- [ ] Test login expiry, logout, optional TOTP, PWA installation and network reconnection on those devices.
- [ ] Close the browser while keeping an always-on backend running; check journal capture continues.
- [ ] Restore a backup to a separate environment and verify trade IDs and export checksums.

## Troubleshooting without exposing secrets

Check the broker’s status detail and the backend’s error messages. The background launcher writes local logs to `.desktop/backend.log` and `.desktop/companion.log`; a manually started backend shows its messages in the console. Share only sanitized error text. Never share URLs containing access tokens, private environment contents or database connection strings. A disconnected API is not replaced with a synthetic price in market-data mode.

If a free service sleeps, restart it through its normal dashboard/website request. Do not claim that uptime pings make a free plan suitable for continuous capture. Review [the deployment guide](DEPLOYMENT.md) before changing to paid resources.
