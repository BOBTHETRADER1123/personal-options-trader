# Connection and calculation update — 4 November 2026

Repository folder (all paths below resolve inside this existing folder):

`C:\Users\Dasari Sampath Kumar\OneDrive\Documents\ChatGPT\Creating a Paper Trading Platform\personal-options-trader`

This is an update to the existing app. Paper-only protections, one asynchronous Gunicorn worker, deployed SQLITE_JOURNAL_MODE=DELETE, authentication and existing records are preserved. No Kotak sign-in or live broker acceptance test was performed by this audit.

## Why the current feed can appear disconnected

The production connection page was read on 4 November and reported **Feed idle · sessions closed**, **Open-position sessions closed**, and encrypted Kotak setup already saved. The only remaining active strategy shown used NIFTY contracts expiring 3 November. Last-known prices on those expired contracts are not current quotes. Choose a current, unexpired contract yourself in Option chain to request its quotes. Settle the old paper position only after you verify the appropriate settlement reference; this update does not invent or overwrite a settlement.

## Changed files only

The following are all source/doc/test changes in this release, including the calculation audit that was already in progress. No runtime database, dotenv credentials, sessions, generated report or existing archive belongs in the patch.

### [backend/app/brokers/__init__.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/brokers/__init__.py>)

Resets retry delay after a connection that delivered quotes. Verified session idling is labelled market_closed, while genuine authentication/protocol errors stay visible. Only Kotak is active.

### [backend/app/brokers/kotak.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/brokers/kotak.py>)

Merged the supplied feed fixes without replacing the existing read-only margin adapter. Network errors and HTTP 429/5xx now retry; genuine authentication rejection stops. One malformed tick is skipped with bounded safe diagnostics, and a consumed TOTP time window is not immediately reused. Original exchange timestamp meaning is retained.

### [backend/app/daily_reports.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/daily_reports.py>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [backend/app/database.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/database.py>)

Adds short read-only stream snapshots with a 150 ms SQLite contention wait. Reads finish before asynchronous waits and restore connection settings before reuse. The normal writer timeout, FULL synchronization and deployed DELETE journal mode remain intact.

### [backend/app/engine.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/engine.py>)

Preserves the pending risk/accounting fixes and adds prospective entry charges to pending-limit cash checks. Explicitly stale exchange update times cannot authorize fills. Old last-trade timestamps are labelled separately because they need not describe the current bid/ask.

### [backend/app/exchange_margin.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/exchange_margin.py>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [backend/app/kotak_margin.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/kotak_margin.py>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [backend/app/main.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/main.py>)

Moves broker subscribe waits outside the global writer lock and runs background reconfiguration separately so one-second capture continues. WebSocket tickets no longer write SQLite or wait for the writer. One batched dashboard and session check is shared per second, with unchanged payloads suppressed and small heartbeats retained. Owner access, Origin, CSRF, single-use tickets and session revocation remain enforced.

### [backend/app/margin.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/margin.py>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [backend/app/payoff.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/payoff.py>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [backend/app/realtime.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/realtime.py>)

Adds bounded hashed one-use tickets and a shared dashboard publisher. Ten concurrent test viewers cause one build. The change detector ignores only elapsed age/server-time fields, retaining stale-state changes, prices and position changes. Session hashes are internal and never sent as dashboard data.

### [backend/app/schemas.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/schemas.py>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [backend/app/security.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/app/security.py>)

Shares the existing owner/Origin/CSRF validation between ordinary endpoints and the bounded stream-authentication path. Ordinary authentication still releases its reader transaction before waiting for the writer.

### [backend/tests/test_engine.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_engine.py>)

Adds or updates isolated automated regression coverage for engine. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_exchange_margin.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_exchange_margin.py>)

Adds or updates isolated automated regression coverage for exchange margin. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_feed_lock_latency.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_feed_lock_latency.py>)

Adds or updates isolated automated regression coverage for feed lock latency. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_feed_resilience.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_feed_resilience.py>)

Adds or updates isolated automated regression coverage for feed resilience. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_frontend_risk_math.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_frontend_risk_math.py>)

Adds or updates isolated automated regression coverage for frontend risk math. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_frontend_transport.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_frontend_transport.py>)

Adds or updates isolated automated regression coverage for frontend transport. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_full_recording.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_full_recording.py>)

Adds or updates isolated automated regression coverage for full recording. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_kotak_margin.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_kotak_margin.py>)

Adds or updates isolated automated regression coverage for kotak margin. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_kotak_margin_routes.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_kotak_margin_routes.py>)

Adds or updates isolated automated regression coverage for kotak margin routes. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_margin.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_margin.py>)

Adds or updates isolated automated regression coverage for margin. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_margin_display_regression.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_margin_display_regression.py>)

Adds or updates isolated automated regression coverage for margin display regression. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_naturalgas_risk_regression.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_naturalgas_risk_regression.py>)

Adds or updates isolated automated regression coverage for naturalgas risk regression. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_release_integration.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_release_integration.py>)

Adds or updates isolated automated regression coverage for release integration. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_risk_lifecycle.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_risk_lifecycle.py>)

Adds or updates isolated automated regression coverage for risk lifecycle. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_runtime.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_runtime.py>)

Adds or updates isolated automated regression coverage for runtime. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [backend/tests/test_stream_performance.py](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/backend/tests/test_stream_performance.py>)

Adds or updates isolated automated regression coverage for stream performance. These tests use temporary databases and synthetic or mocked broker data; a passing test is not a live broker connection.

### [docs/CALCULATION-AUDIT-2026-10-30.md](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/docs/CALCULATION-AUDIT-2026-10-30.md>)

Documents the calculation audit, official-source evidence and explicit limits of verified broker/exchange margin support. Actual saved PE contracts remain distinct from the hypothetical CE example.

### [docs/EXCHANGE-MARGIN-DATA.md](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/docs/EXCHANGE-MARGIN-DATA.md>)

Documents the calculation audit, official-source evidence and explicit limits of verified broker/exchange margin support. Actual saved PE contracts remain distinct from the hypothetical CE example.

### [docs/KOTAK-SINGLE-ORDER-MARGIN.md](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/docs/KOTAK-SINGLE-ORDER-MARGIN.md>)

Documents the calculation audit, official-source evidence and explicit limits of verified broker/exchange margin support. Actual saved PE contracts remain distinct from the hypothetical CE example.

### [docs/MARGIN-CALCULATIONS.md](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/docs/MARGIN-CALCULATIONS.md>)

Documents the calculation audit, official-source evidence and explicit limits of verified broker/exchange margin support. Actual saved PE contracts remain distinct from the hypothetical CE example.

### [frontend/.env.example](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/frontend/.env.example>)

Replaces stale Render instructions with the RamNaym address and explains that VITE_WS_URL is public build-time configuration. Vercel Production and Preview already contain this variable; the final compiled JavaScript was checked for the expected URL.

### [frontend/src/App.tsx](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/frontend/src/App.tsx>)

Adds an eight-second ticket-fetch timeout and socket-opening timeout, one in-flight connection, capped exponential reconnect delay with jitter, and proper hidden-tab/unmount cleanup. Server heartbeats maintain transport health without redrawing the dashboard; HTTP fallback remains available.

### [frontend/src/Builder.tsx](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/frontend/src/Builder.tsx>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [frontend/src/Manage.tsx](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/frontend/src/Manage.tsx>)

Retains the current design while showing timestamp warnings beside the existing quote information and accurate analytical risk/margin availability.

### [frontend/src/MarginBreakdown.tsx](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/frontend/src/MarginBreakdown.tsx>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [frontend/src/PaperTradeReview.tsx](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/frontend/src/PaperTradeReview.tsx>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [frontend/src/pages.tsx](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/frontend/src/pages.tsx>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

### [frontend/src/tradeMath.ts](<C:/Users/Dasari Sampath Kumar/OneDrive/Documents/ChatGPT/Creating a Paper Trading Platform/personal-options-trader/frontend/src/tradeMath.ts>)

Preserves the pending calculation audit: analytical combined expiry risk, unlimited tails, correct costs/remaining quantities and honest unavailable margin labels, with the existing website layout retained. See CALCULATION-AUDIT-2026-10-30.md for formulas and root causes.

## Tested here / needs a real market session

| Check | Tested here | Still required |
|---|---|---|
| Full requested backend suite | Initial connection/calculation run: **734 passed**. Final run after approved retired-code cleanup: **691 passed, 0 failed**, 1 test-library warning; 62.72 s | Live market acceptance remains separate |
| ZIP feed fixes | Mocked 4xx vs 429/5xx/network errors, backoff reset, malformed ticks, TOTP-window reuse | Owner starts Kotak and observes real reconnects |
| Stream/auth delay | Real temporary DELETE-journal exclusive writer returns retry in **under 0.8 s** in regression, rather than waiting 30 s; tickets/accept do not wait for global writer lock | Production response timing under actual load |
| Shared dashboard | 10 concurrent viewers -> **1 build per second**, unchanged ages do not force resend; stale flips still change payload | Production CPU/RAM comparison |
| Slow subscriptions | A blocked fake subscribe leaves the writer available and one-second capture continues | Real broker subscription latency |
| Frontend reconnects | Fake-clock fetch/open timeouts, backoff up to 60 s, heartbeat, visibility and cleanup tests | Actual phone network switching and WSS |
| Frontend build | TypeScript and Vite build passed; expected public WSS URL found in built JavaScript | Confirm new Vercel deployment is Ready |
| Risk/math | NIFTY CE/actual PE, NATURALGAS, partial exits, fees, tails, timestamps and unavailable margin regression tests passed | No verified numerical Kotak basket/complete exchange SPAN comparison yet |
| Hosting source package | RamNaym ZIP integrity, file allowlist and SHA-256 manifest verified | Deploy and verify runtime health/version |

The full suite used the existing pure-Python SQLAlchemy test fallback because Windows application control blocks the installed compiled extension. No security protection was disabled. The commands below set PYTHONPATH only for that PowerShell process.

## Windows rebuild and redeploy

1. Open **PowerShell** and change to this folder:

   ```powershell
   Set-Location -LiteralPath "C:\Users\Dasari Sampath Kumar\OneDrive\Documents\ChatGPT\Creating a Paper Trading Platform\personal-options-trader"
   ```

2. Run the tests (use the existing local fallback only if the compiled-extension block still occurs):

   ```powershell
   $env:PYTHONPATH=(Resolve-Path '.desktop/test-pure-sqlalchemy').Path
   .venv\Scripts\python.exe -m pytest backend/tests -q
   ```

3. Build the upload ZIP. This excludes database files and credentials:

   ```powershell
   $env:Path="C:\Users\Dasari Sampath Kumar\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;"+$env:Path
   .venv\Scripts\python.exe deploy/ramnaym/package_trial.py
   Get-FileHash -Algorithm SHA256 -LiteralPath '..\PersonalOptionsTrader-RamNaym-Trial.zip'
   ```

4. Open RamNaym -> existing **server 1606** -> **Redeploy ZIP**. Choose `PersonalOptionsTrader-RamNaym-Trial.zip` from the parent folder. Preserve the existing environment and `/app/data` volume. Do not create a replacement server or erase the database. Keep `GUNICORN_WORKERS=1`, `WEB_CONCURRENCY=1`, `DATA_DIR=/app/data`, `DATABASE_URL=sqlite:////app/data/trading.db`, and `SQLITE_JOURNAL_MODE=DELETE`. The plan's maximum-worker allowance is separate from the required application worker count.

5. Wait for success. Check `/health/live`, `/health/ready` and the normal website. Confirm saved positions and reports remain present. This is not proof of live broker data.

6. Vercel builds from GitHub main. Production/Preview must contain `VITE_WS_URL=wss://libzw8xd976f.ramnaymcloud.com/ws` **before a new build**. Changing backend environment variables cannot alter an already-built frontend. Wait for the latest Vercel deployment to be Ready, then refresh the website.

7. During a verified session, personally select an unexpired instrument, start Kotak market data and check the original quote timestamp advances. Test for at least several minutes with the website closed, then reopen and examine recording gaps and CPU/RAM. No live order should ever be submitted.

## Calendar work the owner must verify

`backend/calendars/exchange_sessions.json` contains no verified 2027 coverage and its final ranges end **31 December 2026**. Some individual rule ranges end earlier and are followed by segment-specific rules; do not extend them by guesswork. **8 November 2026 Muhurat trading has no session override.** The file deliberately leaves that session closed until verified product-specific hours are supplied.

Look up the official NSE and BSE equity-derivatives Muhurat circulars, the MCX energy-product Muhurat circular and seasonal commodity close schedule, and each exchange's official 2027 trading-holiday/session circulars. Verify date, exchange/segment, contract group, timezone, session start/end, holiday exceptions and effective period; retain the circular URL/number and owner verification date. Do not copy cash-market hours into derivatives or invent 2027 entries. Existing source links are listed in that JSON file. No calendar dates or hours were changed in this release.

## Retired 5paisa implementation — cleanup approved

- `backend/app/broker_auth.py`
- `backend/app/brokers/fivepaisa.py`

The owner explicitly approved removing these modules and unused references on 4 November. The implementation files and their obsolete auth/feed-only tests have been removed. Main, maintenance and migration imports now register historical table definitions from `backend/app/models.py`. The migration revision ID remains unchanged so already-migrated databases remain valid. No stored tables, encrypted records, trades or exports are deleted.

Additional changed files for this cleanup are `backend/app/models.py`, `backend/app/maintenance.py`, `backend/migrations/env.py`, `backend/migrations/versions/0006_private_broker_auth.py`, `backend/tests/test_brokers.py`, and new `backend/tests/test_retired_module_cleanup.py`. Deleted obsolete tests are `backend/tests/test_broker_auth.py` and `backend/tests/test_fivepaisa_unavailable_prices.py`. Tests that ensure retired routes stay disabled and old trade history remains readable are retained. The changed-files ZIP includes an explicit removed-file manifest instead of restoring deleted code.

### Additional cleanup file summaries

`backend/app/models.py` now owns the three historical 5paisa table definitions only. Their names and columns are preserved for recovery; no authentication, token refresh, feed or order service remains.

`backend/app/maintenance.py` and `backend/migrations/env.py` remove the deleted service import and obtain the same historical schema through the existing models import. They do not drop or rewrite records.

`backend/migrations/versions/0006_private_broker_auth.py` imports the historical definitions from models. Its already-applied revision identity and non-destructive behavior stay intact.

`backend/tests/test_brokers.py` removes obsolete 5paisa network/parser-service tests while retaining Kotak tests, the no-real-order check and historical metadata tests.

`backend/tests/test_retired_module_cleanup.py` proves that deleted implementation modules cannot be imported and that a real temporary SQLite migration preserves legacy encrypted rows and a historical paper trade.

## Make the repository private

In GitHub open `BOBTHETRADER1123/personal-options-trader` -> **Settings** -> **General** -> **Danger Zone** -> **Change repository visibility** -> **Make private**. Read GitHub's confirmation and complete it yourself. Existing public forks/downloads are not made private. Check the Vercel GitHub app still has repository access afterwards. See [GitHub's official instructions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility). Repository visibility was not changed by this update.

## If the feed disconnects again

Send the approximate drop time including IST date, RamNaym application/error logs covering roughly one minute before and after it, CPU/RAM graph for the same interval, and the **exact visible connection status**. Also include instrument, exchange and expiry, and whether it happened while using Wi-Fi/mobile data. Hide MPIN, TOTP/setup secrets, access/session tokens, cookies and authorization headers. Do not send full environment settings, credential forms or a database dump.

## Release status

The connection/calculation update was pushed as commit `d11fda2be4bf8e96e72c3e877525e54d1d710223`. Vercel deployment [5hJq6KEFasSA5ssjJPTgfdaKrar8](https://vercel.com/paper-trading1/personal-options-trader/5hJq6KEFasSA5ssjJPTgfdaKrar8) showed Ready. The verified 343,174-byte runtime ZIP was uploaded to existing RamNaym #1606; its liveness and readiness endpoints both returned HTTP 200. The website retained 12 saved broker executions (11 closed and one open), and its footer showed Backend connected. The public compiled JavaScript contains the correct RamNaym WSS URL. No broker login or live quote acceptance test was performed.

After the approved removal of obsolete 5paisa API tests and addition of history-preservation checks, the final full suite passed **691 tests, 0 failures**, with one test-library warning, in 62.72 seconds. The earlier 734-pass result is retained as evidence of the complete connection/calculation run before that cleanup. The cleanup source ZIP is rebuilt separately for the same existing server; production acceptance still requires the owner to test a current contract during its verified market session.
