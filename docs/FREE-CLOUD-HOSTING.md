# Persistent VM hosting: operator preparation

**Status: source scaffold only. No Oracle account, VM, network rule, public endpoint or paid resource was created by this work.** The current website remains on Vercel; its Render/Neon connection is unchanged. This Ubuntu deployment can replace the backend and run while the laptop is off. It still needs a provisioned VM, HTTPS validation, account/data selection and a real phone test.

Oracle Always Free is a possible zero-cost compute route when the selected resource explicitly says **Always Free eligible**. Use the home region and review the final resource summary; trial credits and paid shapes are not a permanent free deployment. Capacity may be unavailable, and Oracle can reclaim idle free instances. There is no promise of uninterrupted free 24/7 service. Oracle requires its own account/identity verification. See [Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) and [signup FAQ](https://www.oracle.com/cloud/free/faq/). The same scaffold also works on a deliberately approved paid Ubuntu VM; this guide authorizes no purchase.

## Prepare the selected VM

1. Select Ubuntu **24.04**, persistent storage and sufficient memory for Python plus the imported option-contract catalog. Record the actual public IP. Keep SSH limited to the operator's address. Permit public HTTP/HTTPS ports **80 and 443** in both the provider and Ubuntu firewall only after review; keep **8000 private**. The installer does not change either firewall.
2. Choose an actual DNS hostname resolving to that VM. A free IP-based DNS hostname is possible, subject to the DNS operator and shared certificate rate limits; no hostname is selected by this scaffold. See [sslip.io](https://sslip.io/). Check DNS from outside the VM before activation.
3. Install Python 3.12 with its `venv` support and Caddy using [Caddy's official signed package instructions](https://caddyserver.com/docs/install#debian-ubuntu-raspbian). Review and stop/disable the package's default `caddy.service` on this dedicated new VM; this app has its own proxy service and refuses to replace another web server.
4. Stage the verified source release, including its **already built `frontend/dist`**, in a root-owned directory immediately inside `/opt/personal-options-trader/releases/`. For example, a reviewed release could use `/opt/personal-options-trader/releases/release-20261023`. No Node installation is needed on the VM. Do not upload the working Windows folder, `.env`, `.desktop`, SQLite databases, Google credentials, Git credentials or companion manifests as application source.
5. Run that staged release's `deploy/vm/install.sh` as root with required `--hostname ACTUAL_DNS_NAME` and `--release ABSOLUTE_STAGED_RELEASE_PATH` arguments. It prepares dependencies and configuration. It does **not** start or enable public services unless `--start` is explicitly present. It supports Ubuntu 24.04 only, refuses source outside the dedicated release directory, preserves existing configuration/data and refuses automatic release switching. Repeating the same completed setup reuses its files and dependencies.

The installer creates a dedicated unprivileged application user. Source and Python dependencies are readable by that app's group; the installer retains a restrictive `077` umask. Persistent state belongs in `/var/lib/personal-options-trader`, separate from source. The private `/etc/personal-options-trader/app.env` is root-only mode `0600`; the generated setup token is never printed. Caddy can read its separate hostname/proxy configuration, not that environment file. Never put secrets in `VITE_` settings or GitHub.

## Select the account and records before starting

The installer does not copy or reset any account. A fresh database needs its first owner created in the app with the generated server setup token; the user enters the password directly. Alternatively, a separately reviewed migration can transfer a **consistent, verified laptop SQLite snapshot over SSH** before starting the VM. This preserves that snapshot's owner password hash and trade records; invalidate copied browser sessions and WebSocket tickets before making it public. The laptop account and the old Neon account are separate. Do not silently replace either one.

Neon-only records remain unavailable until Neon access returns. A raw copy of an active `trader.db` can omit WAL changes: use the existing verified SQLite backup mechanism. A full database contains private account/session data and must stay outside GitHub, public folders and the trade-report Drive destination. Preserve the original source database and safety backup.

Review `app.env` privately: production mode, secure/Lax cookies, persistent SQLite URL and exactly the selected HTTPS hostname plus `https://personal-options-trader.vercel.app` are required. Broker tokens and optional companion pairing belong only in private server settings. No live broker credentials are supplied by this scaffold. Real feeds and daily broker authentication still need verification.

## Activate and connect the existing website

After DNS, firewall, data and private settings are reviewed, repeat the same installer command with **`--start`**. This enables startup after a VM reboot for one uvicorn worker, the dedicated Caddy HTTPS proxy and the daily backup timer. Caddy handles HTTPS and WebSockets; uvicorn listens only on `127.0.0.1:8000` and trusts forwarded headers only from that loopback proxy.

Check the actual HTTPS `/api/health`: `database_ready` must be `true`, `mode` must be `PAPER`, and `live_orders_supported` must be `false`. Test sign-in, a practice trade, a server restart, report generation and phone access over mobile data. A listening process alone is insufficient evidence of a working database. This scaffold has not passed those VM checks yet.

To keep the existing Vercel address, change `frontend/vercel.json`'s `/api` destination to the actual VM HTTPS origin and change Vercel's `VITE_WS_URL` to `wss://ACTUAL_DNS_NAME/ws`; redeploy. Leave `VITE_API_BASE_URL` unset. This preserves Vercel-domain session cookies and authenticated short-lived WebSocket tickets. Do not leave either connection pointing to Render. Alternatively, the VM already serves the built frontend itself: a build without either Vite override uses same-origin `/api` and `/api/ws`.

## Reports, backups and laptop-off limits

The running VM continues paper tracking and compact Excel generation with the laptop off. The reports retain final P&L, observed maximum profit/loss and times, and strategy legs. Completed workbooks remain in the persistent database, with convenience copies in its private `reports` directory. Leave record deletion disabled.

The daily VM timer runs around **18:35 IST**, including one catch-up run after a missed start. It calls the existing consistent SQLite backup helper, verifies integrity and publishes a unique private archive only after success. Each run is time-limited. Defaults allow at most **30 files**, **1 GiB of backup files**, and require **2 GiB free disk space after the estimated copy**. These are conservative creation limits, not an automatic cleanup policy. At a limit, new backup creation stops and the service reports failure; every existing archive and source record is retained. Concurrent database growth can exceed the estimate, so review real disk usage and failed timers. Failed/interrupted creation can leave a private `.partial` file for operator review.

These database backups are on the same VM; loss or reclamation of the VM/storage can lose them. Arrange a separately authorized off-site backup and restore check before relying on unattended use. Do not expose the backups directory through the web proxy.

The existing laptop companion and Codex-to-Google-Drive automation **do not run while the laptop is off**. They can collect completed reports and back them up after it returns. Repointing the existing online companion to the VM needs its own reviewed private pairing settings. Continuous VM-to-Drive uploads would require separate Google authorization and implementation; no Google credential is copied to the VM by this scaffold. Excel reports are not full backups of active trades, settings or the owner account.

## Local validation record

`deploy/vm/test_vm_deploy.py` checks hostname/path rejection, private-file staging refusal, non-overwriting configuration, production environment constraints, backup limits, loopback/one-worker settings and explicit activation. It uses only temporary local files. Python checks do not establish Ubuntu package installation, systemd/Caddy operation, certificate issuance, VM memory sufficiency or physical-phone connectivity. Record those results after the real VM exists.
