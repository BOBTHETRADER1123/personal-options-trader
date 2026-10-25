# Personal Options Trader

This is an update to the existing personal paper-trading app. It preserves working features and records. Only **Kotak Neo** and **5paisa** are active broker integrations; legacy Dhan records remain historical. Real broker order placement is disabled.

## Current setup and deployment status

The production deployment is **Vercel frontend + RamNaym server 1606 backend + SQLite at `/app/data/trading.db`** on the verified persistent volume. One asynchronous application worker owns market recording. The existing React/TypeScript frontend is retained.

**Verified on 25 October 2026:** the public Vercel website now connects to RamNaym SQLite and reports database_ready=true. The approved laptop copy contains one owner, four saved sample trades, 78,172 original quote records and 104,612 original history rows. Owner sign-in through Vercel and an authenticated WebSocket were observed. A subsequent source-only redeploy and container restart retained the account and four trades; the private bootstrap payload is absent from the active image, and the worker limit remains one. These checks do not prove authenticated broker feeds or Google uploads: those external connections still require setup and live testing. Sample trades remain separate from genuine broker-data paper trades. The old Neon database and laptop source remain preserved.

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

Mocked tests are not proof of authenticated broker connectivity or a successful Google account connection. No live broker integration is marked passed until the owner's permitted credentials have actually been tested. Sample mode is explicitly synthetic and must not be mistaken for live prices.

## Development and recovery

Use Python 3.12 and the existing virtual environment. Backend dependencies are in `backend/requirements.txt`, with supported Kotak dependencies in `backend/requirements-brokers.txt`. Frontend scripts are in `frontend/package.json`. Production packaging is in `deploy/ramnaym`; environment templates are in `.env.example`. Never commit private environment files, database copies, OAuth tokens or credentials.

The laptop workspace and old cloud records remain preserved. [START-HERE.md](START-HERE.md), [laptop export/companion recovery](docs/EXPORTS.md) and [existing laptop Drive backups](docs/GOOGLE-DRIVE-BACKUPS.md) describe earlier local workflows. They are separate from the new unattended cloud setup. Do not delete the old database until replacement data and recovery evidence are verified.

## Historical notes from earlier releases

**The following retained notes describe the earlier laptop / Render / Neon release. They are historical recovery context, not current production deployment instructions. References below to Dhan, compact recording, waiting for Neon, old hosting or older UI apply to that earlier release. Use the current guides above for new deployment and recording behavior.**

A responsive personal options paper trading desk for Indian indices and MCX commodities. It includes a React/TypeScript frontend, FastAPI backend, persistent journal, independent read-only broker adapters, strategy payoff engine, durable Excel export queue, and verified Windows local-save companion.

**New to coding? Start with [START-HERE.md](START-HERE.md).**

Open your online app at **[personal-options-trader.vercel.app](https://personal-options-trader.vercel.app)**. On the configured laptop, the **Personal Options Trader Online** desktop shortcut or **Open-Online-Trading-App.cmd** also starts its separate Excel companion. Online reports are configured to save in **Documents/PaperTrading/OnlineTrades**.

As of **22 October 2026**, the hosted owner account is created. **Neon's monthly public network transfer allowance is exhausted, so the online database is temporarily unavailable.** Excel archiving does not reset that allowance. No paid upgrade has been selected. The local practice app and records remain available separately. The update adds a navy/mint dashboard, a searchable stock/index option chain, compact reports, and reduced database reads. Source is in the private [GitHub repository](https://github.com/BOBTHETRADER1123/personal-options-trader).

Available laptop reports have been backed up and downloaded again for checksum verification in [Personal Options Trader Backups](https://drive.google.com/drive/folders/1CxpHsKBCJ8dxxNYbKAx6NMwEU9tojMn5). A daily **4:00 pm IST** Codex backup checks new completed reports when this laptop and Codex are available. Cloud-only records cannot be exported until database access returns. See [Google Drive backups](docs/GOOGLE-DRIVE-BACKUPS.md).

**23 October laptop repair:** short SQLite write conflicts now retry on a fresh transaction without disabling the whole workspace. The user confirmed that the local dashboard opens. The installed app identifies itself as **Laptop workspace** at `http://127.0.0.1:8000`; the Vercel address identifies the separate **Online workspace**. The laptop launcher's readiness check now requires a usable database, not merely a running web server.

**Free cloud migration being prepared:** a persistent VM can run both this app and SQLite while the laptop is off. The [free cloud hosting guide](docs/FREE-CLOUD-HOSTING.md) describes the proposed setup and free-tier limits. No replacement server has been provisioned yet; Oracle account signup and free compute availability still need verification. The current Vercel/Render/Neon deployment is unchanged.

### Included workflows

- Owner login, expiring server sessions, password hashing, optional TOTP, origin/CSRF checks, and a separate companion credential.
- Market and limit paper simulations for Dhan, Kotak Neo and 5paisa, with one action ID and independent broker results.
- Custom strategies and 15 templates; selectable contracts and strikes, enable/disable legs, buy/sell, lots and optional stops/targets.
- Add legs and lots, weighted average accounting, partial and full exits, cancel pending, duplicate and audited paper assumption edits.
- Compact recording by default: current marks, durable processing of active-position quotes, observed extrema, stale detection, and WebSocket delivery. Successful compact receipts are removed after valuation; existing historical records are preserved.
- Searchable current NSE/BSE stock and index option chains from the official Dhan master, with expiry selection, paired CE/PE columns and a paper-strategy basket. Live prices and Greeks require a working Dhan market-data session.
- Gross/realized/unrealized/net P&L, estimated fees, same-expiry piecewise payoff, finite or unbounded risk, heuristic margin and hedge estimates.
- MFE, MAE, their times, closing result and profit giveback.
- Per-trade/broker Excel workbooks, durable jobs, reconciliation and companion verification before optional journal compaction.
- Responsive PWA shell with no offline caching of private financial data.

Sample mode uses **fictional contracts and synthetic quotes**, generated relative to the current date. It is kept separate from master-derived market-data paper trading. The source contains no real-order submission path or live-trading switch.

### Project map

```text
frontend/                React + TypeScript website and installable PWA shell
backend/app/             FastAPI, authentication, database and paper engine
backend/app/brokers/     Dhan v2, Kotak SFeed, 5paisa Xstream read-only adapters
backend/tests/           Accounting, broker protocol, security and recovery tests
companion/               Windows export polling, safe saves and verification
docs/BROKERS.md          Official APIs, session setup and master ingestion
docs/EXPORTS.md          Pairing, acknowledgement, recovery and retention
docs/DEPLOYMENT.md       GitHub Desktop, Vercel, Render, Neon and Android
scripts/                 Installation, backup, restore and packaging helpers
Open-Online-Trading-App.cmd Open the hosted app and start its report companion
Stop-Online-Trading-App.cmd Stop only the online report companion
Open-Trading-App.cmd     Start the local app and paired report companion
Stop-Trading-App.cmd     Stop the local services started by the launcher
.env.example             Private backend configuration template
render.yaml              Render deployment template (free tier initially)
frontend/vercel.json     Website deployment proxy template
```

### Open the online app

Open the hosted website and sign in with the password you already created once Neon database access returns. The online report companion has been stopped during the quota outage. **Personal Options Trader Online** or **Open-Online-Trading-App.cmd** starts it again. Physical-phone synchronization still needs verification.

The online launcher runs the outbound Excel companion in the background and keeps its settings and report folder separate from local practice. Its connection is paired; the first completed hosted report still needs a save-and-reopen check in **Documents/PaperTrading/OnlineTrades**. **Stop-Online-Trading-App.cmd** stops that companion. It does not stop the hosted service or the local practice app.

Render Free can sleep or restart, so it cannot guarantee uninterrupted feed capture. A running companion does not make the free backend continuously available.

### Keep using the local practice app

On the configured laptop, double-click the **Personal Options Trader** desktop shortcut or **Open-Trading-App.cmd** in the project folder. It starts the backend and paired Excel companion in the background and opens the app in your browser. Sign in with your existing owner password. Closing the browser leaves the services running while the laptop is awake. Double-click **Stop-Trading-App.cmd** to stop them; saved records remain on disk.

For a fresh installation, install Python and Node.js, run **Setup-Windows.cmd** once, then use **Open-Trading-App.cmd**. The launcher prepares local companion pairing and the default report folder automatically. See [START-HERE.md](START-HERE.md) for first-owner setup and manual companion options.

### Local development

Requires Python 3.12+ and Node.js LTS supporting Vite 7 (Node 20.19+ or 22.12+). Windows users can use the setup and app launchers above.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
Copy-Item .env.example .env
cd frontend
npm install
npm run build
cd ..
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
```

Open `http://127.0.0.1:8000`. Keep one backend worker: it owns feed subscriptions and capture tasks. For frontend development, run `npm run dev` in a separate terminal inside `frontend`; use `http://127.0.0.1:5173`. The Vite proxy forwards authenticated API requests and WebSocket tickets to the backend. If changing ports, update the allowed origin configuration accordingly.

For PostgreSQL set the root `DATABASE_URL` to the private Neon connection string with TLS. SQLite uses WAL and FULL synchronous commits locally. Database tables and the schema-version record are initialized at startup; startup performs the included additive compatibility changes. Do not replace the database to apply an update.

### Operating rules and limits

- Observed extrema reflect received quotes and recorded actions, including estimated charges. No intermediate ticks are invented. Unfinished quote processing remains recoverable; successful compact processing retains current state rather than a growing tick history.
- The short live chart is held in memory and resets on restart. Trade extrema and final results persist. New Excel files contain Trade Summary and Strategy Legs; existing detailed workbooks remain readable.
- Margin is a disclosed heuristic, not the broker’s blocked margin. Broker margin and account balances remain unavailable until explicitly supported and verified. Paper capital is an assumption.
- Expiry payoff requires the same underlying and expiry; calendar strategies can be recorded but do not receive a misleading combined expiry risk estimate.
- Live contracts come from imported current broker masters. A contract without a verified MCX price multiplier cannot be used for monetary paper P&L. The exchange calendar must be configured to claim session-open status accurately.
- Live broker sessions, completed hosted trade/export flows, Android installation and sustained live throughput still require checks in the actual user environment. Hosted owner setup and initial session/proxy/companion authentication were verified before the quota outage. Read [the test report](TEST-REPORT.md).
- Excel is generated on trade completion and can be downloaded from Reports. The journal remains authoritative while a trade is active. Automatic journal compaction retains summary/manifests/cloud workbooks and shared quotes; it is not complete online data erasure.
- Secrets stay in backend environment variables. SQLite backups include private account/trading data; store them securely. Never upload `.env`, database files, raw masters, generated workbooks, or the companion’s private manifest to GitHub.

### Backups, recovery and tests

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for database backups, exact-match restores, cloud database recovery, reporting recovery, log troubleshooting and the beginner acceptance checklist.

Run the suite from the project folder:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q --basetemp .test-run
cd frontend
npm run build
```

The deployment and broker guides link to the official sources checked for this implementation. See [the deployment guide](docs/DEPLOYMENT.md) for the current setup status and remaining hosting steps. No paid service has been selected, and genuine broker connectivity remains unverified.
