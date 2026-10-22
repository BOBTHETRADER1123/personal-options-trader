# Put the same app on your laptop and Android phone

This guide uses **GitHub for source, Vercel for the website, Render for the
Python service, and Neon PostgreSQL for persistent records**, as requested.
The laptop companion connects outward to Render to save completed workbooks.
Your phone and laptop open the same HTTPS website and use the same owner
password and database. The Python service owns market feeds and export jobs.

## Current status — 22 October 2026

The owner account is created and the user has opened the hosted dashboard. Neon's **monthly public network-transfer allowance is exhausted**, leaving the online database temporarily unavailable. No paid upgrade has been selected. The online companion is stopped during the outage. The new compact recording policy reduces repeated history reads, stores current marks and extrema, and produces small Excel summaries. Available laptop reports are verified in [Google Drive](https://drive.google.com/drive/folders/1CxpHsKBCJ8dxxNYbKAx6NMwEU9tojMn5); daily backup scheduling depends on this laptop and Codex being available. This does not reset Neon's transfer quota.

The infrastructure and first-owner instructions below are retained for **fresh installations and recovery**, not as instructions to reset the existing owner account. End-to-end hosted trade/export and physical-phone checks remain outstanding until database access returns.

## Initial deployment record — 21 October 2026

- The website is live on Vercel Hobby at
  **[personal-options-trader.vercel.app](https://personal-options-trader.vercel.app)**.
  Its project root is `frontend`, the API proxy is connected, and
  `VITE_WS_URL` is configured.
- The backend is live on Render Free in Singapore at
  **[personal-options-trader-api.onrender.com](https://personal-options-trader-api.onrender.com)**.
  It uses the **personal-options-trader** Neon Free PostgreSQL project in
  Singapore. Hosted health and the website's session endpoint returned HTTP
  200 before the quota outage. The user subsequently created the owner account,
  and the session endpoint confirmed first-owner setup was no longer required.
- The **Personal Options Trader Online** desktop shortcut and
  **Open-Online-Trading-App.cmd** start a separate authenticated companion.
  Its destination is `Documents/PaperTrading/OnlineTrades`. Companion polling
  returned HTTP 200, but no hosted workbook save has yet been verified.
- The local practice app, existing local owner account and data are retained.
  Its companion has saved and reopened a verified Excel workbook in
  `Documents/PaperTrading/Trades`.
- App source is in `main` in the private
  [personal-options-trader repository](https://github.com/BOBTHETRADER1123/personal-options-trader).
  No paid service has been selected. Genuine broker connectivity is unverified.

The infrastructure creation steps below are retained for reference and
recovery; the existing repository and services are already configured. The
next operational check is database access recovery, followed by the first
online workbook and phone checks. Keep online deletion turned off while testing.

## 1. Understand the free-service limits

The supplied `render.yaml` deliberately selects **Free** and provisions no
paid disk/database. Free Render services sleep after idle periods, can restart,
and lose local files. They cannot guarantee market recording while your
website is closed. For reliable unattended recording you must deliberately
choose suitable always-on compute after reviewing the displayed charge.
Do not use an artificial keep-alive service as a reliability guarantee.
[Render's free-service limits](https://render.com/docs/free)

Vercel Hobby is intended for personal, noncommercial projects and has usage
limits. Neon Free has storage, compute and transfer limits and can suspend
idle compute. High-frequency journal rows plus saved workbook bytes consume
database capacity. Check usage before each long recording session. Start on
the free options; if any provider asks for payment details, stop and inspect
its plan/charge before choosing to continue.
[Vercel Hobby](https://vercel.com/docs/plans/hobby),
[Neon plans](https://neon.com/docs/introduction/plans)

## 2. Put the project in a private GitHub repository

1. Install [GitHub Desktop](https://desktop.github.com/) and sign in to your
   GitHub account.
2. In File Explorer, locate the **personal-options-trader** folder delivered
   with this app. It contains `frontend`, `backend`, `companion`, `docs`,
   `.gitignore` and `render.yaml`. Do not select only the `frontend` folder.
3. In GitHub Desktop choose **File → Add local repository → Choose** and
   select that project folder. If Desktop says it is not a Git repository,
   choose its **create a repository here** option for that exact folder.
4. Review Changes. `.env`, `.venv`, `node_modules`, real trade databases,
   generated reports and companion tokens must not be listed. The supplied
   `.gitignore` excludes them. If a secret appears, remove it from tracking
   and rotate that credential before proceeding.
5. Enter **Initial paper trading app** in Summary, then click **Commit to main**.
6. Click **Publish repository**, keep **Keep this code private** selected and
   publish. Give Vercel and Render access only to this repository when asked.

GitHub's [add-repository guide](https://docs.github.com/en/desktop/adding-and-cloning-repositories/adding-a-repository-from-your-local-computer-to-github-desktop)
and [publish-repository guide](https://docs.github.com/en/desktop/adding-and-cloning-repositories/adding-an-existing-project-to-github-using-github-desktop)
explain the Desktop controls. Never publish the `.env` file to get a deployment
working; environment variables belong in each hosting service's settings.

## 3. Create Neon PostgreSQL

1. Sign in at [Neon](https://console.neon.tech/) and create a project on its
   **Free** plan. Name it **personal-options-trader**. Choose a region near
   your planned Render service where available.
2. Open the project and click **Connect**. Select your production branch,
   database and database role. Copy the connection string from this dialog.
   Leave TLS options such as `sslmode=require` and `channel_binding=require`
   intact. A pooled string is supported; do not guess its host or password.
3. Keep the string private. You will paste it into **Render → Environment →
   DATABASE_URL**, never Vercel and never the companion.
4. The backend automatically converts a normal `postgresql://` string to the
   installed psycopg driver. Do not enter a SQLite URL for the hosted service.
   Tables are created on first startup; the `schema_version` record tracks
   this initial schema. Back up before future application upgrades.

The [Neon connection guide](https://neon.com/docs/connect/connect-from-any-app)
shows the Connect dialog and pooled/direct connection choices.

## 4. Create the Render backend

1. Sign in at [Render](https://dashboard.render.com/) with GitHub.
2. Choose **New → Blueprint**, connect the private repository and let Render
   read the root `render.yaml`. This template creates one Python web service
   named **personal-options-trader-api**. Review the plan and keep **Free**
   for initial testing. No paid resource is requested by the template.
3. At the environment prompt set **DATABASE_URL** to your Neon string.
4. For **ALLOWED_ORIGINS**, temporarily use an HTTPS origin you control, then
   replace it with the exact Vercel production origin in Step 6. If you do not
   know it yet, use `https://setup-pending.invalid` as a deliberate placeholder.
   The app will reject browser setup from that placeholder until you replace it.
5. The template generates a random **SETUP_TOKEN**. In Render's Environment
   page, reveal and save it in your password manager. It protects the first
   owner-account creation. Do not share it or put it in frontend variables.
6. Review the required settings below, then create/deploy the service:

   | Render field | Exact value |
   | --- | --- |
   | Root directory | `backend` |
   | Runtime | Python 3 |
   | Build command | `pip install -r requirements.txt` |
   | Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips=*` |
   | Health check | `/api/health` |
   | `ENVIRONMENT` | `production` |
   | `COOKIE_SECURE` | `true` |
   | `COOKIE_SAMESITE` | `lax` |
   | `SESSION_HOURS` | `12` |

7. The `.python-version` files select the current Render patch release of
   Python 3.12. Keep **one worker and one service instance**: this version
   intentionally has one feed/logger owner. Multiple worker processes would
   duplicate subscriptions and logging.
8. Wait for Render to show **Live**. Copy its actual service URL, such as
   `https://your-chosen-service.onrender.com`. Open that URL followed by
   `/api/health`; the response should show `ok: true`, `mode: PAPER`, and
   `live_orders_supported: false`.

The command follows Render's [FastAPI setup](https://render.com/docs/deploy-fastapi).
The proxy trust flag applies only to the managed Render ingress, where TLS is
terminated before the Python process. Do not copy wildcard forwarded-header
trust to an unprotected Windows/public-port server.
[FastAPI proxy documentation](https://fastapi.tiangolo.com/deployment/https/),
[Render deployment guidance](https://render.com/articles/fastapi-production-best-practices)

The template disables automatic backend redeploys to avoid interrupting a
recording session after every code push. Use **Manual Deploy** during a quiet
period when you intentionally update the backend.

## 5. Connect Vercel to Render

The current deployment already has this connection. For a replacement
deployment, use the actual backend's hostname consistently.

1. The API rewrite in `frontend/vercel.json` points to:

   ```json
   "destination": "https://personal-options-trader-api.onrender.com/api/:path*"
   ```

2. If you change this destination for a replacement service, save, commit
   and push the file before redeploying. The hostname is public configuration.
3. Sign in at [Vercel](https://vercel.com/) and choose **Add New → Project**.
   Import your GitHub repository.
4. Set **Root Directory** to `frontend`, **Framework Preset** to **Vite**,
   **Build Command** to `npm run build`, and **Output Directory** to `dist`.
5. In Vercel Environment Variables add **VITE_WS_URL** with your real Render
   WebSocket URL, replacing `https://` with `wss://` and adding `/ws`:

   ```text
   wss://personal-options-trader-api.onrender.com/ws
   ```

6. Leave **VITE_API_BASE_URL unset**. Browser `/api` requests travel through
   the supplied Vercel rewrite, so login cookies belong to the Vercel website.
   The WebSocket connects directly to Render using an authenticated, one-use,
   short-lived ticket created by the backend. Broker secrets and database
   credentials must never have a `VITE_` prefix.
7. Choose the personal free/Hobby option if eligible, inspect any plan prompt,
   and deploy. Copy your stable production address, such as
   `https://personal-options-trader.vercel.app`. Use this stable address on both
   devices rather than a temporary preview deployment URL.

Vercel's [external rewrites](https://vercel.com/docs/routing/rewrites) support
this API proxy. API responses explicitly disable browser/CDN caching so one
session's records are not cached as public content. This deployment's API proxy
has passed its initial session/setup-state check.

## 6. Finish the owner account and shared-device test

The existing hosted owner account is already created. The setup steps below apply only to a new installation. The local practice owner account remains separate.

1. The configured **ALLOWED_ORIGINS** is the Vercel production origin,
   `https://personal-options-trader.vercel.app`. When changing the website
   origin, update **Render → Environment** and redeploy. Do not add a
   trailing slash, page path, or wildcard.
   If you later add a custom domain, add that exact HTTPS origin too,
   separated by a comma.
2. Open [the Vercel website](https://personal-options-trader.vercel.app).
   Enter the private **SETUP_TOKEN** and create an owner
   password of at least 12 characters. Keep it in a password manager.
   Optional `OWNER_TOTP_SECRET` enables an authenticator factor; this is
   independent of Kotak's broker login factor.
3. After owner setup, sign out and sign in again. The app uses expiring,
   HttpOnly secure cookies; a session is not a broker token.
4. On the laptop create a new paper trade in Practice data. On Android, turn
   off Wi-Fi, open the same Vercel address using mobile data and sign in with
   the same password. The same trade must appear.
5. Close part of the trade on Android. Verify the remaining lots and P&L on
   the laptop. Restart Render deliberately, sign in again if necessary, and
   verify that Neon retained the trade and export status.
6. In Android Chrome use **Add to Home screen / Install app** if offered.
   The installed app opens the same website and records. It cannot record new
   broker quotes while the backend is stopped; reconnect before trading.

## 7. Configure read-only broker prices

1. Follow `docs/BROKERS.md` for each broker independently. Obtain the account's
   actual data access and inspect any subscription charge before purchasing.
2. Enter broker variables only in **Render → Environment**. For Kotak, change
   the build command to include its optional current SDK when you choose to
   enable that broker:

   ```text
   pip install -r requirements.txt -r requirements-brokers.txt
   ```

3. Import current master CSVs on the backend, restrict `FEED_INSTRUMENT_IDS`
   to an explicit watchlist, and reload/restart. Never copy sample contract
   IDs into a live watchlist. With a free Render instance lacking shell/disk,
   perform the import on Windows and intentionally include a sanitized
   current master as deployment data, or use an always-on service with an
   appropriate durable workflow. Refresh before each trading day; the current
   manual ingestion commands are documented in `BROKERS.md`.
4. Turn off Practice data only after source labels, current quote times,
   stale-feed warnings and per-broker matching contracts have been verified.
5. Keep the watchlist small during first checks. A full exchange master can
   exceed broker limits and create far more journal data than a free database
   can hold. A connection without fresh prices is not a working live feed.

## 8. Pair the laptop save companion

Pairing is already configured for this laptop. **Personal Options Trader
Online** or **Open-Online-Trading-App.cmd** opens the hosted website and
starts its separate companion; **Stop-Online-Trading-App.cmd** stops that
companion only. It polls outward over HTTPS and opens no public laptop port.
The initial authenticated jobs request succeeded. The first workbook save
still needs the checks below after hosted owner creation.

The online launcher reads these private files, which are excluded from Git
and the source release:

- Root `.env.online`: `PUBLIC_WEBSITE_URL` points to the Vercel HTTPS origin.
- `companion/.env.online`: `BACKEND_URL` points to the Render HTTPS origin;
  `COMPANION_TOKEN` is the private online pairing token; `TRADES_FOLDER` is
  the full path to `Documents/PaperTrading/OnlineTrades`; `POLL_SECONDS` is
  the polling interval.

Keep the raw token only on the laptop, with its SHA256 hash in Render's
`COMPANION_TOKEN_SHA256`. The retained local companion uses different `.env`
files and `Documents/PaperTrading/Trades`; do not repurpose its manifest or
overwrite its connection to configure the online companion.

To verify online saving:

1. Complete a practice trade after finishing hosted owner setup.
2. Watch **Excel reports** for **Pending**, then **Saved locally** when the
   companion receives it. Open the workbook in
   **Documents/PaperTrading/OnlineTrades** and check Summary, Timeline,
   Events, Legs, Market Updates and timestamps.
3. Stop the online companion with **Stop-Online-Trading-App.cmd**, then
   complete another practice trade and verify that its job stays online.
   Restart **Open-Online-Trading-App.cmd** and check that it saves one
   verified workbook and acknowledges it. Repeat the failure/restart tests
   in `EXPORTS.md` before considering optional online archiving.

The laptop does not need the website open for saving. Its companion must be
running and the cloud backend must be awake. When the laptop is off, the
export payload and workbook/job remain in Neon. With the website closed,
unattended work requires a running backend; free-tier sleep still applies.

## 9. Back up and operate the app

1. Keep **Delete after verified local save** disabled initially. A generated
   file alone is not permission to delete the source. The companion verifies
   checksum, nonzero size and workbook validity before acknowledgment.
2. Back up Neon independently using its supported backup/export tools, and
   back up the laptop's completed workbooks to another location you control.
   Excel reports are not a complete backup of login/settings/active trades.
3. Review the daily reconciliation view for pending or failed jobs. Check
   provider usage and disk/database capacity, token expiry and stale feeds.
4. Keep broker credentials out of GitHub commits and logs. Rotate any leaked
   secret and update the backend environment. Retest after SDK/protocol changes.
5. Do not run a local backend and a cloud backend against the same live Neon
   database at the same time. This version supports one process owner for the
   journal/feed. The laptop companion alone may run alongside the cloud backend.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Blank page | Vercel root is `frontend`; build output is `dist`; inspect its build log. |
| API returns HTML or 404 | Fix the `/api` Render destination before the SPA fallback. |
| First load takes about a minute | The free Render service may be waking up. |
| Login repeatedly disappears | Use Vercel `/api` proxy, unset VITE_API_BASE_URL, and use Secure/Lax cookies over HTTPS. |
| Origin rejected | Render ALLOWED_ORIGINS must exactly match the current stable Vercel/custom origin. |
| First setup rejected | Use Render's actual SETUP_TOKEN. Production refuses setup without it. |
| WebSocket reconnecting | Correct VITE_WS_URL to `wss://ACTUAL-RENDER-HOST/ws`, redeploy frontend, check backend awake and allowed origin. |
| Database connection fails | Use actual Neon DATABASE_URL on Render with its TLS query parameters intact. |
| Records vanish on restart | Cloud DATABASE_URL is missing or points to SQLite; configure Neon before recording more trades. |
| Live contract unavailable | Refresh/import the matching broker master, verify watchlist and monetary multiplier for MCX. |
| Companion says HTTPS required | Use Render HTTPS URL and the Render-specific proxy command from Step 4. |
| Excel job remains pending | Check companion token/hash, laptop online, destination writable, backend awake, and job error details. |

The remaining hosted-app and real-broker checks in this guide require the
actual services and accounts. They remain **not yet verified**; the local
automated tests cannot establish phone-outside-network or real feed behavior.
