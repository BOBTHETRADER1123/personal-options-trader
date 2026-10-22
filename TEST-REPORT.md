# Verification report — 22 October 2026

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

The user created the hosted owner account and opened its dashboard. Session state was verified before the outage. Neon has now exhausted its free monthly public network-transfer allowance. Hosted health/database probes are unavailable. Cloud-only records cannot currently be exported, and Excel archiving does not reset the allowance. No records were deleted. The online companion was stopped to avoid unsuccessful polling.

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
