# Deploy the existing Personal Options Trader

The current application uses **Vercel for the website, RamNaym server #1606 for FastAPI, and SQLite on RamNaym's persistent volume**. Kotak Neo is the only active broker. All orders remain simulated.

## Existing services

- Website: [personal-options-trader.vercel.app](https://personal-options-trader.vercel.app/).
- Backend: [libzw8xd976f.ramnaymcloud.com](https://libzw8xd976f.ramnaymcloud.com/).
- Database: `/app/data/trading.db`, inside server #1606's persistent `/app/data` volume.
- Source: [BOBTHETRADER1123/personal-options-trader](https://github.com/BOBTHETRADER1123/personal-options-trader), branch `main`.

The separate preliminary project #1605 (`yb1zu3mcerwr`) is not the production backend. Do not replace server #1606 or its data directory during source updates. Existing owner sign-in and saved trades should be retained. The old Neon database and original laptop records remain separate recovery sources; this deployment does not require querying or deleting Neon.

## Update GitHub and Vercel

1. Commit only application source, tests, templates and documentation. The repository ignores credentials, database files, workbooks, recovery imports, installed dependencies and temporary output.
2. Push to `main`. The existing Vercel project builds from the `frontend` root using `npm run build` and publishes `dist`.
3. Confirm Vercel shows **Ready** and the expected commit before testing the public website. GitHub contains the source needed to rebuild; generated frontend assets are built during deployment.
4. Keep the `/api/:path*` rewrite in `frontend/vercel.json` pointed at `https://libzw8xd976f.ramnaymcloud.com/api/:path*`.
5. Keep the Vercel public WebSocket setting `VITE_WS_URL=wss://libzw8xd976f.ramnaymcloud.com/ws`. API requests use the same-origin authenticated proxy; WebSocket connections obtain short-lived tickets. The website falls back to API polling if its socket is unavailable.

Pushing GitHub updates Vercel automatically. RamNaym's existing ZIP deployment requires its own source upload; do not assume a Git push updated the backend.

## Prepare the RamNaym source-only upload

From the existing laptop project folder, with the installed Python and Node dependencies available:

```powershell
.venv\Scripts\python.exe deploy\ramnaym\package_trial.py
```

This creates `PersonalOptionsTrader-RamNaym-Trial.zip` beside the project. It builds public frontend assets in isolation and validates the ZIP against an explicit runtime allowlist and checksum manifest. It excludes real environment files, database copies, reports, tokens, test output and private workspace imports.

In RamNaym, open **server #1606 -> Manage -> Redeploy ZIP** and choose this verified source ZIP. Use the packaged root `main.py` ASGI entry point. Preserve private environment settings and the persistent volume. Updating source briefly restarts the application; plan the update outside recording sessions where practical. Never claim the restart had no gaps without checking recorded timestamps.

## Production settings

The confirmed Medium allocation is 1 vCPU, 2 GB RAM and 15 GB persistent storage. Run exactly **one asynchronous Gunicorn application worker**, without preload. The package includes `gunicorn_conf.py` and the `safe_worker.SingleASGIWorker` implementation. When the provider supports an explicit command, use:

```sh
gunicorn -c gunicorn_conf.py main:app
```

The root gateway also checks production settings before loading the workspace. Required non-secret settings include:

```dotenv
GUNICORN_WORKERS=1
WEB_CONCURRENCY=1
DATA_DIR=/app/data
DATABASE_URL=sqlite:////app/data/trading.db
SQLITE_JOURNAL_MODE=DELETE
ENVIRONMENT=production
COOKIE_SECURE=true
COOKIE_SAMESITE=lax
ALLOWED_ORIGINS=https://personal-options-trader.vercel.app,https://libzw8xd976f.ramnaymcloud.com
```

The deployed volume was verified as local ext4 with functioning SQLite locks. Keep the verified DELETE journal configuration. If the storage provider changes, verify persistence and locking again before continuing writes. Do not switch to an unverified network filesystem or assume host-wide free space is the plan's 15 GB allocation.

Use `.env.example` for remaining settings. Keep existing broker/Google encryption keys in private server settings and a private recovery store. Replacing those keys can make encrypted credentials unreadable. Never put secrets in frontend `VITE_` variables, GitHub or the source ZIP.

## Broker and backup setup

- Follow [Kotak setup](BROKERS.md) to save credentials through the owner's private form, refresh its official master and inspect actual quote timestamps. A saved key is not proof of a live feed.
- Follow [Google authorization](GOOGLE-DRIVE-CLOUD.md) to select the intended backup account and grant offline access to app-created files.
- Follow [live Sheets](GOOGLE-SHEETS-LIVE.md) and [Daily Excel Downloads](DAILY-REPORTS.md) for synchronization, complete exports, verification and safe cloud cleanup.
- The optional [Windows companion](EXPORTS.md) saves reports while that laptop is awake. The cloud recording and Google workers run on RamNaym independently of the laptop when the server is available.

## Verify after deployment

1. Open `/health/live` on the backend: this checks the process without querying the database.
2. Check `/health/ready` and `/api/health` for application/database readiness. A running process alone does not prove broker or Google connectivity.
3. Sign in through the Vercel website using the existing password. Confirm the expected open trades and history remain.
4. Inspect authenticated **Data & settings -> Cloud backups & server health** for SQLite readiness, collector ownership, CPU/RAM, storage and backup queue state.
5. During an official exchange session, verify Kotak quote delivery, source timestamps, reference-price freshness and one-second recorded observations. Closed sessions should not create artificial fresh prices.
6. Confirm a completed daily export covers all expected trades and rows, then verify a real Google upload before relying on cloud archiving. Preserve source records on any failure.
7. Keep external monitoring on the lightweight liveness route; monitoring does not run the trading engine. Review recording gaps and pending uploads after restarts.

See [actual test results and remaining acceptance checks](../TEST-REPORT.md). Hosting health, payments/trial status and broker permissions remain external service conditions; source deployment does not guarantee future availability.
