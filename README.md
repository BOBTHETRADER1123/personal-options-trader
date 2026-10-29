# Personal Options Trader

This is an update to the existing personal paper-trading app. It preserves working features and records. Only **Kotak Neo** is enabled for market data and new paper trades. Saved 5paisa and Dhan records remain historical; their integrations are disabled. Real broker order placement is disabled.

## Current setup and deployment status

The production deployment is **Vercel frontend + RamNaym server 1606 backend + SQLite at `/app/data/trading.db`** on the verified persistent volume. One asynchronous application worker owns market recording. The existing React/TypeScript frontend is retained.

**Migration verified on 25 October 2026:** the public Vercel website now connects to RamNaym SQLite and reports database_ready=true. The approved laptop copy contains one owner, four saved sample trades, 78,172 original quote records and 104,612 original history rows. Owner sign-in through Vercel and an authenticated WebSocket were observed. A subsequent source-only redeploy and container restart retained the account and four trades; the private bootstrap payload is absent from the active image, and the worker limit remains one. Those migration checks do not prove broker feeds or Google uploads. Kotak MCX quote delivery was subsequently observed on 28 October; full live-load and recovery acceptance remains outstanding. Sample trades remain separate from genuine broker-data paper trades. The old Neon database and laptop source remain preserved.

Website: [personal-options-trader.vercel.app](https://personal-options-trader.vercel.app/). Private source: [GitHub repository](https://github.com/BOBTHETRADER1123/personal-options-trader). `http://127.0.0.1:8011` is an isolated laptop preview, not the public cloud service.

## Start with these guides

- [Current RamNaym deployment and trial checks](docs/RAMNAYM-TRIAL.md)
- [Broker connection methods and limitations](docs/BROKERS.md)
- [Daily Excel Downloads and safe cloud cleanup](docs/DAILY-REPORTS.md)
- [Live Google Sheets and separate Google account setup](docs/GOOGLE-SHEETS-LIVE.md)
- [Google authorization and completed-trade archives](docs/GOOGLE-DRIVE-CLOUD.md)
- [Actual automated test results and pending live checks](TEST-REPORT.md)

## Current implemented workflows

- Owner authentication, secure sessions, protected APIs, independent broker prices and simulated fills.
- Buying/selling options and futures, multi-leg strategies, partial exits, strategy payoff and estimated margin/charges. No real orders.
- One-second captured P&L history with original quote times, stale/reused status, recording gaps and restart coordination. Production capture follows maintained verified exchange sessions.
- All-time strategy and broker-execution counts, responsive dashboard, option chain and strategy builder.
- Daily backend-generated XLSX, separate worksheets per broker trade, continued trade IDs across dates, large-file splitting, CSV.gz supplements and checksums.
- Durable approximately five-second Google Sheets batches, verification before cursor advancement, upload retries and explicitly confirmed daily cloud cleanup. Google OAuth must be configured and authorized before these uploads work.
- Container CPU/RAM, persistent-storage and queue monitoring. Dashboard/log alerts do not imply external email/SMS is configured.
- Optional automatic archiving at 12 GiB: fresh verification of complete older daily Google archives before bounded local snapshot cleanup, with continuity anchors, active state and retained export files preserved. SQLite freed pages are reused; this does not clear the database.
- Google account changes retain old remote files and previous synchronization metadata, then use new account-bound destinations. Google consent and live upload verification remain required.

Mocked tests are not proof of authenticated broker connectivity or a successful Google account connection. No live broker integration is marked passed until the owner's permitted credentials have actually been tested. Sample mode is explicitly synthetic and must not be mistaken for live prices.

## Development and recovery

Use Python 3.12 and the existing virtual environment. Backend dependencies are in `backend/requirements.txt`, with supported Kotak dependencies in `backend/requirements-brokers.txt`. Frontend scripts are in `frontend/package.json`. Production packaging is in `deploy/ramnaym`; environment templates are in `.env.example`. Never commit private environment files, database copies, OAuth tokens or credentials.

The laptop workspace and old cloud records remain preserved. [START-HERE.md](START-HERE.md), [laptop export/companion recovery](docs/EXPORTS.md) and [existing laptop Drive backups](docs/GOOGLE-DRIVE-BACKUPS.md) describe earlier local workflows. They are separate from the new unattended cloud setup. Do not delete the old database until replacement data and recovery evidence are verified.

## Latest verified update — 29 October 2026

- The Kotak feed, option-chain and responsive website update is published. The full isolated suite passed **543 tests**, and TypeScript/production builds passed.
- Normal contract-selection changes use incremental subscriptions. Touch-line data retains LTP, best bid/ask, OI, volume and original timestamps. Unrelated chain quotes no longer copy the entire saved ledger.
- A clearly labelled futures reference locates nearby option strikes when a fresh verified mark is available. It is not described as cash spot.
- Four genuine open paper strategies survived the backend update. Closed trades can be removed from website history and performance, then restored through **Removed trades**; original exports and records remain preserved.
- Sustained hosted live-feed CPU, recording/reconnection and complete Google backup acceptance must still be checked during applicable exchange sessions. Offline tests do not guarantee uninterrupted hosting or broker availability.

## Repository contents and housekeeping

`frontend/` holds the website; `backend/` holds the API, market-data engine, migrations and automated tests. `deploy/ramnaym/` builds the source-only server upload. `scripts/` and `companion/` retain installation, export and recovery tools.

Use [the deployment guide](docs/DEPLOYMENT.md) for the current Vercel/RamNaym connection. The retired Render deployment template has been removed; earlier versions remain in Git history.

Disposable test output and caches were cleaned locally. Private `.desktop/` state, local account databases, credentials, saved reports and recovery copies remain outside Git. Keep `.venv/` and frontend dependencies on the laptop while using its installed app. Do not upload the full laptop folder as a cloud release; use the allowlisted release packager.
