# Kotak Neo market-data connection

**Kotak Neo is the only enabled broker for live market data and new paper trades.** 5paisa and Dhan integrations are retired. Saved trades, daily reports and archives from those brokers remain historical records; preserving them does not enable a feed or permit a new execution.

The application is paper-trading only. No actual broker order-placement endpoint is called. Broker market-data permissions, supported contracts and session availability still apply.

## Current SFeed protocol

The adapter uses the official asynchronous SFeed implementation from the [Kotak Neo Python SDK](https://github.com/Kotak-Neo/kotak-neo-python), with `SFeedWebSocket` and depth subscriptions. The deprecated HS market feed and broker order feed are not used. See the [official WebSocket guide](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/websocket.md).

Authentication and subscription failures produce sanitized status messages. Transport reconnects use bounded backoff, reconnect counts and resubscription to the selected contracts. The application enforces the SFeed subscription budget; it does not subscribe to the entire imported master.

An untraded strike may contain a zero, blank or missing LTP. It produces no new usable quote and does not reset the previous price's timestamp or stop other valid subscriptions. Malformed or negative/nonfinite prices remain explicit failures. Missing fields are never borrowed from another broker or replaced with invented live values.

## Owner-only private setup

Open **Broker connections** on the website and choose **Set up Kotak Neo**. Create the Trade API application and complete TOTP registration in Kotak Neo if needed. The private form accepts:

- Application/consumer key
- Registered mobile number
- Client code (UCC)
- MPIN
- Base32 authenticator setup key

These values are encrypted in private server storage with the backend-only `BROKER_CREDENTIAL_KEY`. Status APIs return only field-presence information. Existing secret values are never displayed back in the form. Preserve the encryption key securely for recovery; replacing it without a migration makes saved credentials unreadable.

Enter keys, MPINs and authenticator values only in the private app or official broker pages. Never put them in chat, source files, frontend `VITE_` settings, logs or GitHub. Saving setup does not prove authentication or quote reception worked. The read-only collector attempts authentication when permitted contracts are selected during an exchange session.

For an operator-managed installation, the backend can use either:

- A currently authenticated `KOTAK_UCC` and `KOTAK_SID` session.
- `KOTAK_CONSUMER_KEY`, `KOTAK_MOBILE_NUMBER`, `KOTAK_UCC`, `KOTAK_MPIN` and a fresh `KOTAK_TOTP`. A protected `KOTAK_TOTP_SECRET` permits server-side generation instead of a pasted temporary code.

Session expiry requires Kotak's supported authentication flow again. A pasted TOTP cannot remain valid indefinitely. SDK request traces are disabled so authentication response bodies do not enter app logs.

## Import current derivative contracts

Use **Refresh contracts** in Broker connections. The backend discovers official master URLs and validates the NSE, BSE and MCX derivative segments before replacing the cache. A consumer key is required to discover these URLs independently of successful feed login.

For operators, run the following from `backend` with the configured Python environment:

```powershell
python -m pip install -r requirements-brokers.txt
python -m app.instruments kotak --download --segment nse_fo
python -m app.instruments kotak --download --segment bse_fo
python -m app.instruments kotak --download --segment mcx_fo
```

The [official master implementation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/services/scrip_master.py) resolves URLs. Native expiry interpretation follows the [instrument documentation](https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/market-data-apis/instruments.md). Current native `lPrecision=2` strike and tick fields are converted from paise once, following the [SDK strike-search implementation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/services/scrip_search.py). Explicit normalized fields remain in human units. Unsupported precision fails validation; contract quantity is not guessed from a trading symbol.

Refresh the master each trading day and after exchange contract changes. The current refresh action is explicit; automatic daily master refresh has not been accepted as verified functionality. Failed downloads or parsing retain the previous valid cache.

## Option chain and strategy builder

Select exchange, underlying and expiry from Kotak's imported master. The available universe includes supported NSE/BSE stock and index options and permitted MCX contracts. Futures appear in the strategy catalog, not as CE/PE chain rows.

Browsing a chain creates a temporary subscription to the selected expiry. The builder requests only its selected legs. Active and pending paper positions retain their own subscriptions independently of open website tabs. All selections use exact Kotak contract identities and original quote timestamps. The engine checks freshness and contract metadata again at paper entry and exit.

Kotak depth messages do not supply all requested analytics. IV, Greeks, spot, previous OI/volume and buildup remain unavailable when not actually provided. No fabricated production values or secondary broker substitutes are used.

The displayed margin, hedge benefit and charges are labelled estimates. This release does not claim verified live broker margin or account balances.

## MCX monetary conversion

Crude Oil, Crude Oil Mini, Natural Gas and Natural Gas Mini require verified physical contract quantity and pricing metadata. Apply the physical quantity exactly once when computing monetary P&L. Kotak's quantity, units and quotation fields must match the maintained official MCX specifications. Unknown conversions and old cached rows without verified provenance may be viewed but cannot authorize a new paper fill.

The actual broker mini symbol `NATGASMINI` uses the maintained energy session. [MCX conversion evidence and historical parser notes](MCX-CONTRACT-UNITS.md) retain the provenance used by earlier releases; they do not enable retired brokers.

## Watchlists and storage

`INSTRUMENT_MASTER_PATH` selects the validated JSON cache; keep it under persistent `/app/data` on RamNaym. `FEED_INSTRUMENT_IDS` is an optional comma-separated Kotak watchlist. Use exact IDs from the imported master, for example:

```text
kotak:nse_fo:<actual-token>
```

An empty configured watchlist does not subscribe to the whole market. The active union contains watched contracts, recently viewed selections and active/pending paper positions. The bounded contract API loads selected pages rather than repeatedly downloading the complete master to a phone.

Permanent trading records are in the configured SQLite database at `/app/data/trading.db`. Instrument cache files do not replace trade history. The old Neon database is preserved separately and is not used by the current deployment.

The server's dated exchange calendar controls recording sessions, including holidays and commodity timing. Unknown or expired calendars fail closed rather than claiming the exchange is open. The backend can remain online outside the session without manufacturing market updates.

## Status meanings

| Status | Meaning/action |
| --- | --- |
| Not configured | Complete the private Kotak setup. |
| Awaiting instruments | Refresh the current master and select actual contracts. |
| Authentication required | Repeat Kotak's supported session/TOTP authentication. |
| Reconnecting | A transport interruption is being retried with bounded backoff. |
| Error | Review master, permissions or subscription budget; no prices are substituted. |
| Connected but stale | Check exchange session, original quote time and broker permissions. |
| Disconnected | The connection stopped; last-known marks retain their timestamps. |

## Live acceptance checklist

During a real exchange session on server **1606**, verify Kotak authentication, quote reception and timestamps, session expiry, transport reconnection, paper entry/partial exit/full exit, one-second recording with website tabs closed, and restart resubscription. Check the exact recording gaps, exports, backups and measured CPU/RAM/latency.

Offline parser and accounting tests do not prove an authenticated broker connection. Earlier 5paisa login, master or socket observations are historical evidence only. They are not evidence that the current Kotak-only deployment has passed live acceptance. No real broker order may be sent during testing.
