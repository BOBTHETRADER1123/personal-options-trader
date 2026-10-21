# Verification and setup report — 21 October 2026

## Result

**99 automated backend tests passed.** The final TypeScript check and Vite production build passed. A real local HTTP service launch also passed its health, authentication, static website and PWA-manifest checks.

This report distinguishes code verification from external-account and device verification. No real broker order was placed. Genuine broker connectivity remains unverified, and no paid service has been selected.

## Setup confirmed as of 21 October 2026

- The laptop app is installed and running with an existing owner account.
- The local companion is paired. A completed Excel workbook was saved in `Documents/PaperTrading/Trades` and reopened successfully.
- App source was pushed to `main` in the private [GitHub repository](https://github.com/BOBTHETRADER1123/personal-options-trader), with initial source commit `f2d290d`.
- The **personal-options-trader** Neon Free project was created in Singapore. Render and Vercel deployment are pending.

## Checks actually run

| Area | Verified behavior |
| --- | --- |
| Paper accounting | Long/short formulas; monetary MCX multiplier; weighted average additions; partial and full exits; fees; realized/unrealized results. |
| Strategies | Known spread/condor payoff cases; finite/unbounded risk; mixed-expiry payoff marked unsupported while conservative standalone margin stays nonzero. |
| Trade controls | New trades, add legs/lots, duplicate, audited edits, limit fills/cancellation, and close-to-export enqueue. |
| Multi-broker actions | Separate broker results, shared action identity, repeat-request deduplication and SQLite rollback boundaries. |
| Market journal | Every received test event persists; carried-forward snapshots; stale-event preservation; durable processing queue; restart replay. |
| Replay execution | Limit/stop/target fills use the triggering receipt, not a later quote; buy/sell limit bounds remain respected with configured slippage. |
| Excursions | MFE/MAE and their times; closing result and profit giveback; sample history explicitly identified. |
| Authentication | Owner setup/login, password hashing, optional TOTP, origin/CSRF rejection, password/session invalidation, one-use WebSocket ticket and replay rejection. |
| Sample separation | Fictional demo records excluded from market-data-mode metrics; retained sample contracts recover across a date-changing restart. |
| Broker protocol adapters | Synthetic documented Dhan binary, 5paisa JSON and Kotak SFeed payloads; timestamps/missing fields; source mapping; master schema validation; MCX units and calendar cases. |
| Excel reports | Reopenable workbooks, required sheets/history, IST times, formula-injection escaping, monetary units, checksums and explicit synthetic-sample labels. |
| Export recovery | Offline queue retention, failed generation, server/companion restart, duplicate retries/acknowledgements, invalid checksum/workbook/path rejection, local-save failure and default-off trade-specific cleanup. |
| Local backup | Verified SQLite copy, invalid-backup rejection, and restore with retained before-restore records. |
| Logical backup | Blob-safe, checksum-verified database backup/restore and transactional safety backup. |
| Frontend | Strict TypeScript compilation and production asset generation. Keyboard modal focus/escape handling and responsive breakpoints are implemented. |

The isolated local launch check verified HTTP 200 for `/api/health`, first-owner setup state in its temporary test database, HTTP 401 for an unauthenticated dashboard, successful static website/PWA-manifest serving, and HTTP 404 for a real-order route. The installed laptop app now has its owner account already created.

The suite produced one upstream Starlette/httpx test-helper deprecation warning. It produced no failing tests. The backend and frontend were tested in this Windows workspace with the project’s local environment and installed dependencies.

## Verification limits

- **Browser visual and interaction QA was blocked by the desktop browser security policy.** No screenshot, browser click-through, or visual-layout pass is claimed. No alternate browser/control path was used to bypass that block. Static build and API-level checks passed; actual phone/desktop appearance needs a user-device check.
- **Genuine account connectivity is not verified.** The adapters follow reviewed official documentation and have protocol/master tests. Broker authentication, subscription permissions, token expiry, rate/subscription limits, reconnect behavior and sustained live load must be checked with your accounts.
- **Render and Vercel deployment are pending.** GitHub source publication and Neon project creation are complete. A hosted app URL, production PostgreSQL operation, TLS cookie behavior across the actual hosts, and cross-device synchronization remain unverified.
- **Android installation is not verified on a physical phone.** The responsive PWA shell, service worker and manifest are included; actual device checks are in docs/OPERATIONS.md.
- **No continuous high-volume benchmark is claimed.** Durable recovery tests do not establish millisecond exchange completeness, maximum throughput, continuous uptime, storage sufficiency, or any broker’s actual feed latency. Free hosting that sleeps cannot capture its missing interval.
- **Real Excel file locking and automatic Windows sign-in startup remain unverified.** Companion save/reopen/checksum/retry behavior passed temporary-file and simulated-HTTP tests, and a workbook was also saved and reopened in the laptop’s selected report folder. The desktop launch shortcut exists; automatic launch at Windows sign-in has not been verified.

## Deliberate implementation boundaries

1. Broker integrations are market-data-only. There is no future live-order mode, real-order submission method or enable-live switch.
2. Available broker cash and broker SPAN margin are not presented as connected account values. Paper capital is a user assumption; margin/hedge benefits are disclosed heuristics.
3. Reports are generated when a trade completes. While active, the durable journal and snapshots are the authoritative history. The app does not rewrite an open Excel workbook on every tick.
4. Online cleanup is optional and disabled by default. After verified acknowledgement it compacts only that completed trade’s detailed event/timeline rows. It retains trade summaries, shared quote records, manifests and cloud workbook bytes. It is not complete online-data erasure.
5. Production deployment uses one backend process. Distributed feed ownership, multi-user tenancy, email recovery and exchange-grade high-availability infrastructure are outside this personal app’s included configuration.

## Repeat the checks

From the project folder:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q --basetemp .test-verification
cd frontend
npm run build
```

Use a fresh temporary-test directory if Windows keeps a prior test file locked. Do not delete the real database to resolve a test or export failure. The beginner checklist and backup/recovery instructions are in [docs/OPERATIONS.md](docs/OPERATIONS.md).
