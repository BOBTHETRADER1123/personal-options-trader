## Market-feed corrections — 27 October 2026

- Complete automated suite: **423 passed**, one existing Starlette/httpx deprecation warning.
- Isolated production build and source-only runtime ZIP checksum validation passed.
- New tests cover zero/missing 5paisa last prices without losing later valid
  messages, OI timestamp retention, real authentication error handling, Kotak
  native price scaling, expiry business dates and unknown multiplier blocking.
- Live observations before deployment: owner 5paisa OAuth completed, official
  catalog loaded, 522 subscriptions initiated, and a zero-price protocol error
  stopped that feed. The corrected release still needs quote-delivery verification.
- Kotak official master URL and public NSE/MCX headers were reachable using
  private server credentials. Corrected full catalog import and live TOTP/feed
  acceptance still need verification. No real orders were sent.

## Broker connection update — 26 October 2026

- Full isolated automated suite: **387 passed**, one existing Starlette/httpx deprecation warning.
- Frontend TypeScript check and isolated production build passed.
- Added encrypted Kotak private setup and session-bound 5paisa daily authorization.
- Tested secret redaction, CSRF/owner boundaries, callback replay/expiry/fencing,
  encrypted restart restoration and non-blocking HTTP exchange.
- Source-only RamNaym package: 56 runtime files plus checksum manifest;
  credentials and workspace data excluded.
- **Not yet live tested:** authenticated Kotak/5paisa streaming, actual account
  permissions, sustained broker load, daily reauthorization and market recording
  using those live feeds. A saved setup or token is not a live feed test.

# Export CPU recovery — 26 October 2026

343 automated tests passed in 27.67 seconds with the supported Python-only SQLAlchemy 2.1.5 test build. The eight new resource tests cover lease renewal during slow normalization, replaced-owner fencing, refusal to revive expired ownership, recording priority pauses, cancellation/source retention, streamed deterministic ZIP processing, CPU-budget accounting and CSV completeness.

An isolated Windows synthetic run used20 strategies mirrored into40 broker trades,10,000 snapshots and20,000 leg rows. Export completed in37.241 seconds with13.062 process CPU seconds (35.08% CPU/wall ratio),85.21 MiB peak process working set, two XLSX parts plus paired CSV files and a master index. Every source snapshot remained, and row counts/trade coverage passed. This is a local synthetic measurement, not a RamNaym live-feed capacity guarantee.

Remote evidence before this fix: the25 October report was on attempt9, still generating with its lease expired by287 seconds; its six stored staging parts occupied96,474,962 bytes. The app worker used961.5 MiB RSS while reported container CPU stayed near100%. The fix renews/fences ownership independently, streams ZIP/CSV processing, defaults to one-million-cell parts, targets35% export-thread CPU cooperatively, and pauses heavy generation when active market recording needs the server. Short compression peaks can still exceed the target. No trading history is deleted by this change.

Google OAuth on dasarishreyas21@gmail.com and one actual completed-trade Excel upload were verified in the live app at08:44:50 IST. Full daily-history finalization and real broker connections remain separate acceptance checks.

---

# Backup and archival update — 26 October 2026

335 automated tests passed in 21.32 seconds using the official Python-only SQLAlchemy 2.1.5 build in an isolated test folder. Windows Application Control blocked the installed optional compiled extension during collection; no Windows security settings or live server dependencies were changed. One existing Starlette/httpx test-helper deprecation warning remains. TypeScript checking passed.

New coverage verifies disabled sample capture, Google account-bound metadata, upload/account-switch serialization, token rotation, Google network waits outside the recording lock, <=200-row archival transactions, rollback-journal headroom, durable partial-prune receipts, restart resume, current-day protection, checksum/source-coverage refusal, and capture-lock timeout/cancellation cleanup. Automatic archival retains account settings, active positions, orders, summaries, daily exports, legacy journals and per-trade/day continuity anchors. It does not clear the entire database or automatically VACUUM a nearly full volume.

The approved Google OAuth client is configured in Production mode with drive.file only. Credentials were saved in RamNaym private environment settings. Google Drive and Sheets APIs are enabled. Owner consent, authenticated upload/readback, genuine broker connectivity, sustained market-hour capture, and multi-GB archival throughput remain live acceptance tests. The 12 GiB trigger and 10 GiB reusable-space target are configurable. No actual source snapshots were pruned during validation.

---

# Current update: 25 October 2026

Live deployment acceptance, 25 October 2026: GitHub main 3b355116d7005b53c93580a62d8251e283c3fd2e was deployed by Vercel as 687ngUJXMnaggkfXwoQCUsQmg5KA. The public /api/health returns database_ready=true and /api/session recognizes an existing owner. Owner sign-in and authenticated WSS were observed. Approved migration checks passed: owners1, trades4, original quote_events78172, original timeline104612, integrityok, with browser sessions excluded from the copy. A source-only redeploy and container restart retained owners1/trades4, no /app/private_workspace payload, and GUNICORN_WORKERS1. RamNaym measured average CPU14.7%, peak20.2%, RAM10.6% and591MB storage during the observed sample workload; this is not a real broker market-hour peak-load result. The storage UI now uses application data against the configured15GB allowance instead of the host filesystem's free space.

289 automated backend tests passed in 27.31 seconds, with one existing Starlette/httpx deprecation warning. TypeScript and the Vite production build passed. Eight Google Sheets tests use mocked Google responses: batching 20 strategies mirrored into 40 broker executions, durable cursors, replaying interrupted writes into identical ranges, quota backoff, source retention, verified readback mismatch and cleanup refusal. These are not authenticated Google integration results.

Isolated browser preview: dark overview, all-time saved strategy/execution counts, light strategy builder and explicit sample labels inspected. Full app sample database survives local process restart. A rollback-journal read/write lock inversion was found during browser testing and fixed by releasing authentication reads before waiting on async write locks and moving commits to worker threads. Regression test verifies a blocked ticket commit leaves liveness responsive.

RamNaym server 1606: actual DATA_DIR /app/data is a writable local ext4 Docker volume. GUNICORN_WORKERS=1, SQLite3.46.1. A second independent process correctly received database-is-locked while the first held an IMMEDIATE transaction. The earlier disposable compatibility marker survived restart and rebuild. The full app now passes its RamNaym /health/live, /health/ready and /api/health checks with database_ready=true. Root main.py is explicitly selected because automatic detection selected the nested package module incorrectly. Owner workspace transfer, full-app cloud restart and live market-session performance are checked separately; a generic hosting success badge is not treated as proof.

Daily exports cover separate broker-specific trades, linked strategy IDs, carried positions on multiple IST dates, complete captured leg rows, split XLSX/CSV parts, manifest checksums and explicit owner backup acknowledgement. No source records or Neon database were deleted. Google cleanup uses Trash, never automatic permanent deletion.

Remaining live acceptance tests: actual broker credentials and permissions; app-owned Google OAuth on the selected 15GB account; five-second Google writes; real market-session recording with browser closed; cloud app restart; resource and API latency measurements under sustained realistic market load. Expired contracts require explicit owner-confirmed paper cash settlement; official settlement prices are not fetched or invented. MCX physical delivery/devolution is not simulated. Muhurat sessions require verified product-specific overrides before recording. The existing working React/TypeScript frontend is deliberately retained rather than rewritten solely to change framework names.

---

# Verification report — 23 October 2026

**Current result: 187 automated tests passed** (180 backend/launcher tests and 7 VM scaffold tests, with 14 subtests) in 9.98 seconds using `.test-feed-selection-release`; one upstream test-helper deprecation warning remains. TypeScript and the Vite production build previously passed; no frontend source changed in this verification. The user confirmed the installed **Laptop workspace** dashboard opens with the existing local account.

## Unattended position feed correction

The feed manager no longer applies the configured watchlist a second time after the application has selected watched contracts plus open and pending trade legs. The second filter could exclude an active trade outside the watchlist, leaving it without streaming quotes when no option-chain page was refreshing it. Broker and demo filtering remain in place.

Two isolated regression cases cover configured and empty watchlists: active and pending legs reach the adapter, while closed trades, closed legs and unused catalog entries stay excluded. The focused broker tests passed (30 tests), followed by the complete backend and VM suite above. The tests use a fake adapter and an isolated database; genuine broker connectivity and server deployment are not asserted by this result.

## Phone controls and persistent VM preparation

Narrow phone confirmation dialogs now stack full-width buttons; option-chain Buy/Sell controls use 44 px touch targets on narrow or coarse-pointer screens. Existing paper-trade create/close flows remain in place. This was verified by source review and the production build; physical-phone trading still requires a working hosted backend and has not been claimed complete.

The Ubuntu 24.04 VM scaffold keeps SQLite outside the release directory, uses an unprivileged single worker behind HTTPS, preserves private configuration and archives, and requires explicit service activation. Seven temporary-file tests cover unsafe hostnames/paths, private-file staging, non-overwriting writes, production settings, backup limits and activation constraints. Python compilation, Bash syntax and diff checks passed. No installer, VM, firewall rule, cloud account or paid resource was created. Real Ubuntu operation, certificates, data migration, restart recovery and phone access remain to be verified on the selected host.

An isolated catalog benchmark used 126,432 synthetic contracts across 434 underlyings with the actual parser/writer: about 339 MiB peak process memory and 158 MiB after selecting bounded chains. It accessed no user database or network. Windows measurements exclude the complete app, Linux and proxy; 1 GB is a plausible starting VM size, not a proven production guarantee.

## Local connection repair

The local application was incorrectly treating brief SQLite write conflicts as a disconnected database, returning 503 for every API route. Capture and daily reconciliation now retry the complete transaction on a fresh Session with bounded backoff. Extended SQLite BUSY/LOCKED codes, including a real WAL snapshot error 517, are tested. Integrity and statement failures remain local to the failed action; genuine connection/storage failures still pause workers. Receipt commit and valuation retry remain separate to prevent receiving a quote twice. Safe diagnostics omit SQL and bound account/session values.

The desktop launcher now requires reported database readiness. The website labels its separate Laptop and Online workspaces and no longer implies a laptop password automatically belongs to the online account.

- The installed backend and Excel companion ran together during a 75-second probe: **74 health checks and 74 session checks passed**, with zero failures and the existing owner retained.
- An isolated 65-second runtime test exercised 742 successful API reads and saved/reopened compact reports. It exposed additional contention in daily reconciliation; that write now uses the same transaction retry helper.
- The affected-path repeat after the final fix passed **235 API requests and 34 concurrent reconciliation writes, with zero errors**. A sample BUY trade was opened and closed; two generated compact workbooks were saved, reopened, checksum-verified and acknowledged by an isolated companion. Pending quote receipts and raw quote history remained zero.
- Temporary test processes were stopped. All runtime test databases, owner credentials and report folders were isolated from the user's actual records. A fresh private SQLite safety backup was retained before applying the repair.

The online workspace still depends on restoring Neon's exhausted allowance. This local SQLite repair does not change the online database plan or quota.

## Prior verification — 22 October 2026

**164 automated tests passed** in 5.46 seconds using `.test-final-summary-drive-verified`. One upstream Starlette/httpx deprecation warning remains. Strict TypeScript compilation and the Vite production build passed. No real broker order was placed and no paid plan was selected.

## Verified changes

- Navy/mint dashboard with four P&L cards, active strategies, broker results, market watch and desk status; responsive navigation and existing authentication/trade/report flows remain integrated.
- Option chain for NSE/BSE stock and index options: expiry selection, paired CE/PE data, ATM position, OI/volume/IV/Greeks/PCR/buildup, and exact contract/side/lot basket handoff into the builder. Tests cover official schema identity, missing fields, stale quotes, token mismatch, cache bounds, owner/CSRF checks and read-only endpoint restrictions.
- The current official Dhan public master downloaded and parsed successfully: **434 exchange-specific underlying entries** (424 stock entries and 10 index entries; 215 BSE and 219 NSE), totaling **126,432 option contracts**. These are exchange-specific entries, not a count of unique companies. Coverage changes with the official master.
- Compact recording defaults apply to older installations. Tests verify exact observed MFE/MAE and times without tick/timeline growth, stop/target/limit trigger prices, receipt recovery and monotonic IDs, retained failures, current marks after restart, rollback cache invalidation and IST day changes.
- Warmed compact dashboard refreshes issue **zero SQL statements** in the regression test. Report lists omit full payloads and XLSX bytes. Browsing chains does not append snapshot history or subscribe to the whole instrument universe.
- Slow database work leaves the HTTP event loop responsive. Outages pause SQL workers, return a clear 503 for affected requests, keep health available and retry initialization with backoff.
- Compact Excel contains Trade Summary and Strategy Legs. Checks cover final results, observed profit/loss extremes and IST times, no invented profit timestamp for an all-losing trade, formula-injection escaping and verification of new and legacy files.
- Drive outbox checks cover read-only sources, data allowlists, duplicate prevention, tampered files, checksum/size mismatch, path bounds and private configuration isolation. Two actual laptop reports were uploaded to Drive and downloaded again; byte sizes, MD5 and SHA-256 matched. Re-preparation reported zero pending and two uploaded files.
- Existing accounting, fees, payoff, action idempotence, multi-broker results, security, TOTP, sessions/CSRF, one-use socket tickets, backup/restore, companion acknowledgement and safe Windows launcher checks passed.

## External status and limits

The user created the hosted owner account and opened its dashboard. Session state was verified before the outage. The update at commit `7707832` is published: Vercel reports Ready, and Render deploy `dep-dbcs9emi0phs73fsg4qg` is Live. Its health endpoint responds with `database_ready: false`, confirming the app is running while the database remains unavailable after the Neon transfer-quota warning. Cloud-only records cannot currently be exported. Excel archiving does not reset the allowance. No records were deleted. The online companion was stopped to avoid unsuccessful polling.

The local app was stopped safely, a private verified SQLite safety copy was saved, and the new local backend/companion were started. Four existing trades remain present and the effective recording mode is `summary`.

The daily **4:00 pm IST** Google Drive heartbeat is configured in Codex. It depends on this laptop and Codex being available and backs up completed reports available locally. Google credentials are not installed into the trading application. This is not a continuously running cloud Drive service.

The website build is verified. Authenticated visual/interaction QA of the updated hosted dashboard and option chain is limited by the outage. Earlier local browser access was blocked by browser security policy; no alternate control path bypassed it. Genuine broker sessions, subscription permissions/reconnect behavior, the first completed hosted workbook, physical Android use and sustained live throughput remain unverified.

Current marks, trade extrema and final results persist. The short session chart lives in memory and resets on restart. Free hosting cannot recreate prices never received. Existing detailed histories and workbooks are preserved; compact mode avoids new detailed history by default.

## Repeat checks

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp .test-verification
cd frontend
npm run build
```

Tests use isolated databases and report folders. One initial attempt encountered a transient Windows Application Control load failure in SQLAlchemy; the unchanged library subsequently imported and the final complete suite passed. No Windows security controls were changed.
