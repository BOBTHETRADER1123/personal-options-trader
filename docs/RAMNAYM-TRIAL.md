# Current deployment: Vercel + RamNaym SQLite

This updates the existing app. No Neon data is deleted. The runtime uses only Kotak Neo market data and cannot place real broker orders. Existing 5paisa and Dhan trade records and archives are preserved as history; their feeds and new-trade choices are disabled.

## Verified hosting and limits
Server 1606 Medium: 1 vCPU, 2GB RAM, 15GB persistent /app/data, exactly one application worker. On 24 October 2026 the running container reported a local ext4 mount, writable /app/data, SQLite3.46.1 and GUNICORN_WORKERS=1. A second independent process correctly failed to acquire a competing SQLite write lock. Previous disposable probe data survived normal restart and rebuild. On 25 October the migrated owner and four saved sample trades also survived a source-only redeploy and container restart. That proves persisted account/trade state for those checks, not live broker reconnection or gap-free market recording.

Use rollback journal mode DELETE with synchronous FULL. WAL is not enabled for this cloud runtime. Startup rechecks mount type, write locking and rollback recovery using a separate bounded diagnostic database, refusing network or unknown volumes. Normal restart persistence does not protect against deletion of the server or provider failure: keep independently verified exports.

## Build and upload
Run the existing Python environment with deploy/ramnaym/package_trial.py after tests and the frontend build. Upload only PersonalOptionsTrader-RamNaym-Trial.zip to the existing server using Redeploy ZIP. It contains app source, public assets, calendar and migrations; it excludes passwords, private settings, records and tokens.

Set Runtime → Start File to main.py explicitly before a ZIP redeploy. Automatic detection can select backend/app/main.py and break package imports. The root main.py is a real FastAPI gateway that runs the workspace lifespan and forwards HTTP/WebSocket traffic. Confirm /health/live and /health/ready, not just a generic hosting success badge.

Supported startup: python start.py. It validates the environment then starts exactly one gunicorn worker using safe_worker.SingleASGIWorker, based on uvicorn_worker.UvicornWorker. Disable preload and autoscaling. If RamNaym manages the command, confirm it honors the supplied Gunicorn configuration. Do not bypass protected provider worker controls. Logs must not contain URL queries, OAuth codes or socket tickets.

Private environment settings:
- DATA_DIR=/app/data
- DATABASE_URL=sqlite:////app/data/trading.db
- SQLITE_JOURNAL_MODE=DELETE
- GUNICORN_WORKERS=1 and WEB_CONCURRENCY=1
- ENVIRONMENT=production and COOKIE_SECURE=true
- COOKIE_SAMESITE=lax
- ALLOWED_ORIGINS=https://personal-options-trader.vercel.app,https://libzw8xd976f.ramnaymcloud.com
- PERSISTENT_STORAGE_LIMIT_GB=15
- SETUP_TOKEN: private random one-time setup value only if no owner account is migrated.

Enter broker settings from docs/BROKERS.md in private server environment fields. Never put them in frontend settings or ZIP files. Broker passwords and verification codes are entered only by the owner. An existing account migration needs explicit destination confirmation and must exclude active browser sessions. Keep the original laptop and Neon records until the replacement and required history are verified. Never create paid subscriptions automatically.

## Link the existing website
After authenticated readiness succeeds on RamNaym, change the /api rewrite in frontend/vercel.json from Render to https://libzw8xd976f.ramnaymcloud.com/api/:path*. Set Vercel VITE_WS_URL=wss://libzw8xd976f.ramnaymcloud.com/ws and redeploy. Keep the public website URL unchanged. Do not direct users to a backend which cannot read its records.

## Separate Google account
Create an OAuth web client with Drive and Sheets APIs enabled. Use the Vercel callback above so owner session cookies match. Store client ID, client secret and a new Fernet token encryption key exclusively on the backend. Connect from Data & settings; Google asks which account to use. Choose the separate 15GB account. The app requests only `drive.file` with offline consent, for files it creates or that the owner selects; this scope also permits its Sheets updates. Review the permission screen yourself. A test-mode OAuth refresh token may expire after seven days; configure the consent app appropriately and watch renewal alerts. No Codex connector token is copied into the app.

Enable GOOGLE_SHEETS_SYNC_ENABLED=true only after consent works. Five-second batches update dated spreadsheets from durable one-second SQLite rows. Daily XLSX exports are created locally, split at configured limits and include a compressed CSV supplement. Use Daily Excel Downloads to inspect records, download every part, compare checksums, reopen worksheets and explicitly verify a local backup. Download alone is never acknowledgement. Delete Verified Cloud Copies moves eligible app-owned files to Trash; empty Trash yourself only after retaining independent verified copies. This cloud-file cleanup does not delete SQLite history; the separately enabled verified local-history archiver has its own safeguards.

## Automatic history retention
Set `AUTO_ARCHIVE_ENABLED=true`, `AUTO_ARCHIVE_TRIGGER_GB=12`, `AUTO_ARCHIVE_TARGET_GB=10` and `AUTO_ARCHIVE_INTERVAL_SECONDS=60` only after Google uploads and verification work. At the trigger it uploads and freshly verifies complete older finalized daily XLSX/CSV parts, then removes eligible one-second detail in bounded resumable transactions outside active recording. Unsynchronized, current-day, missing or unverified data remains protected. It keeps each trade/date's last snapshot, account/position/order state, summaries, original quote/timeline journals, exports and audit evidence. No whole-database clearing or automatic Neon removal occurs.

Freed SQLite pages are reusable even if the database file does not shrink. The archiver does not run a full-volume vacuum near capacity and checks temporary rollback headroom before deletion. Retained data may prevent reaching the 10 GiB target; review visible alerts and keep independent verified backups. Full details: [Google archival policy](GOOGLE-DRIVE-CLOUD.md#automatic-local-history-archiving-at-12-gb).

## Monitor and recover
Use /health/live for lightweight process monitoring, and /health/ready for cached database readiness. Configure cron-job.org to alert on HTTP failures; it does not run or keep alive the trading engine. Owner-only health screens distinguish feeds, stale quotes, recording gaps, backup queues and resource warnings. Local storage warns at 60%,75%,85%,95%. 15GB is finite; holding many positions for months can exceed it, so verify daily archives and review capacity rather than assuming unlimited retention.

Never copy a live SQLite file alone: use SQLite backup API, include the full transactional snapshot, verify integrity, and test restore with the server stopped. Downloaded Excel is useful trade history but does not reconstruct owner credentials or pending-order state. Keep an encrypted independent recovery backup outside this one server. The app never auto-deletes Neon or unarchived source rows.

## Trial acceptance checks still required
Actual Kotak authenticated WebSocket; official master permissions; closed-browser recording during real sessions; one-second gap report; full app container restart recovery; Google five-second writes and download integrity; CPU/RAM/disk and response-time measurements. Automated mocks do not prove these live checks. No guaranteed uninterrupted uptime is claimed.
