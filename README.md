# Personal Options Trader

A responsive personal options paper trading desk for Indian indices and MCX commodities. It includes a React/TypeScript frontend, FastAPI backend, persistent journal, independent read-only broker adapters, strategy payoff engine, durable Excel export queue, and verified Windows local-save companion.

**New to coding? Start with [START-HERE.md](START-HERE.md).**

## Included workflows

- Owner login, expiring server sessions, password hashing, optional TOTP, origin/CSRF checks, and a separate companion credential.
- Market and limit paper simulations for Dhan, Kotak Neo and 5paisa, with one action ID and independent broker results.
- Custom strategies and 15 templates; selectable contracts and strikes, enable/disable legs, buy/sell, lots and optional stops/targets.
- Add legs and lots, weighted average accounting, partial and full exits, cancel pending, duplicate and audited paper assumption edits.
- Received quote journal, one-second snapshots and carry-forward labels, stale detection, source timestamps, WebSocket delivery and polling fallback.
- Gross/realized/unrealized/net P&L, estimated fees, same-expiry piecewise payoff, finite or unbounded risk, heuristic margin and hedge estimates.
- MFE, MAE, their times, closing result and profit giveback.
- Per-trade/broker Excel workbooks, durable jobs, reconciliation and companion verification before optional journal compaction.
- Responsive PWA shell with no offline caching of private financial data.

Sample mode uses **fictional contracts and synthetic quotes**, generated relative to the current date. It is kept separate from master-derived market-data paper trading. The source contains no real-order submission path or live-trading switch.

## Project map

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
Open-Trading-App.cmd     Start the local app and paired report companion
Stop-Trading-App.cmd     Stop the local services started by the launcher
.env.example             Private backend configuration template
render.yaml              Render deployment template (free tier initially)
frontend/vercel.json     Website deployment proxy template
```

## Open the Windows app

On the configured laptop, double-click the **Personal Options Trader** desktop shortcut or **Open-Trading-App.cmd** in the project folder. It starts the backend and paired Excel companion in the background and opens the app in your browser. Sign in with your existing owner password. Closing the browser leaves the services running while the laptop is awake. Double-click **Stop-Trading-App.cmd** to stop them; saved records remain on disk.

For a fresh installation, install Python and Node.js, run **Setup-Windows.cmd** once, then use **Open-Trading-App.cmd**. The launcher prepares local companion pairing and the default report folder automatically. See [START-HERE.md](START-HERE.md) for first-owner setup and manual companion options.

## Local development

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

## Operating rules and limits

- Received broker updates are journaled; the provider’s delivery capabilities and network interruptions bound what can be recorded. Timestamp precision is not delivery accuracy. No intermediate ticks are invented.
- One-second snapshots are produced while the backend runs; a stopped or sleeping server cannot record the missing interval. Reports preserve observed data and identify carried-forward marks.
- Margin is a disclosed heuristic, not the broker’s blocked margin. Broker margin and account balances remain unavailable until explicitly supported and verified. Paper capital is an assumption.
- Expiry payoff requires the same underlying and expiry; calendar strategies can be recorded but do not receive a misleading combined expiry risk estimate.
- Live contracts come from imported current broker masters. A contract without a verified MCX price multiplier cannot be used for monetary paper P&L. The exchange calendar must be configured to claim session-open status accurately.
- Live broker sessions, production Neon/PostgreSQL, cloud hosting, Android installation and sustained high-volume throughput require checks in the actual user environment. Read [the test report](TEST-REPORT.md) for what was run.
- Excel is generated on trade completion and can be downloaded from Reports. The journal remains authoritative while a trade is active. Automatic journal compaction retains summary/manifests/cloud workbooks and shared quotes; it is not complete online data erasure.
- Secrets stay in backend environment variables. SQLite backups include private account/trading data; store them securely. Never upload `.env`, database files, raw masters, generated workbooks, or the companion’s private manifest to GitHub.

## Backups, recovery and tests

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for database backups, exact-match restores, cloud database recovery, reporting recovery, log troubleshooting and the beginner acceptance checklist.

Run the suite from the project folder:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q --basetemp .test-run
cd frontend
npm run build
```

The deployment and broker guides link to the official sources checked for this implementation. No external account has been created, connected, charged, or deployed by these local files.
