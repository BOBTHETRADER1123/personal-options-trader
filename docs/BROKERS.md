# Broker market-data connections

Only **Kotak Neo** and **5paisa** are enabled for new paper trades and live
feeds. Their quotes, instrument tokens, authentication, receive times and
paper ledgers remain independent. A strategy submitted to both brokers is
simulated separately against each broker's own available quote. No adapter
exposes real order placement, modification or cancellation.

Existing Dhan trade/history/export records are preserved as legacy data. The
Dhan feed implementation, new-trade selection and option-chain HTTP calls
have been removed. A saved master containing old Dhan entries does not block
loading the two supported brokers.

Official documentation was reviewed on **24 October 2026**. All broker tests
use synthetic messages and fake sockets. No actual credentials were supplied;
account authentication, entitlements, live streaming, reconnect behavior and
market-hours performance still require the two-day server trial.

## Kotak Neo: current 2026 SFeed

The adapter uses the maintained [official Python SDK](https://github.com/Kotak-Neo/kotak-neo-python),
pinned to the [published PyPI release 3.0.9](https://pypi.org/project/kotakneoapi/) in `backend/requirements-brokers.txt`. Its public wheel checksum and required SFeed configuration arguments were verified without connecting a broker. The
[official SFeed guide](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/websocket.md)
describes the asynchronous binary `native_batch` feed. The deprecated HS
callback feed is not used. `subscribe_depth` supplies decoded LTP, bid/ask,
volume and OI; matching `unsubscribe_depth` is attempted during shutdown.
SDK-auth failures become a sanitized **Authentication required** status.
Network reconnects are managed centrally with bounded backoff, reconnect
counts and resubscription to the current contract set. The 3,000-token SFeed
budget is enforced. A full master is never subscribed indiscriminately.

Backend credentials can use either:

- An authenticated session: `KOTAK_UCC` and `KOTAK_SID`.
- The official TOTP/MPIN flow: `KOTAK_CONSUMER_KEY`,
  `KOTAK_MOBILE_NUMBER`, `KOTAK_UCC`, `KOTAK_MPIN`, plus a fresh `KOTAK_TOTP`.
  An optional protected `KOTAK_TOTP_SECRET` permits server generation.

Session expiry requires the broker's actual authentication flow again.
A pasted TOTP cannot remain valid indefinitely. Credentials belong only in
private backend settings; never enter them in frontend variables or chat.
SDK console/file request traces are disabled to prevent authentication
response bodies from entering application logs.

Install the optional SDK, then import each enabled derivative segment:

```powershell
python -m pip install -r requirements-brokers.txt
python -m app.instruments kotak --download --segment nse_fo
python -m app.instruments kotak --download --segment bse_fo
python -m app.instruments kotak --download --segment mcx_fo
```

These commands run from `backend`. The app's **Refresh catalog** action also
fetches the selected broker's three derivative segments. A consumer key is
required to discover official master URLs, independently of feed login.
The [SDK instrument implementation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/services/scrip_master.py)
is used to resolve those URLs. CSVs are bounded and validated before atomic
replacement. Native NSE expiry offsets and direct BSE/MCX Unix expiry values
follow the broker's [instrument documentation](https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/market-data-apis/instruments.md).
No expiry or lot quantity is inferred from a trading symbol.

## 5paisa Xstream

Complete the broker's [OAuth login](https://xstream.5paisa.com/dev-docs/user-authentication-system/oauth-login)
and [access-token exchange](https://xstream.5paisa.com/dev-docs/user-authentication-system/access-token).
Store `FIVEPAISA_CLIENT_CODE` and `FIVEPAISA_ACCESS_TOKEN` only on the backend.
The documented access token expires daily; the app stops on authentication
rejection and requests guided reauthentication. It does not invent a silent
refresh-token mechanism.

The adapter uses the official [MarketFeedV3 WebSocket](https://xstream.5paisa.com/dev-docs/market-data-system/web-socket).
The allowlisted A/B/default feed host routing follows the [official 5paisa SDK](https://github.com/OpenApi-5p/py5paisa/blob/master/py5paisa/py5paisa.py); arbitrary token-supplied hosts are never used. It subscribes to LTP/bid/ask and the separate `GetScripInfoForFuture` OI channel,
and sends matching unsubscriptions when stopping. OI-only messages do not
pretend to be new price quotes: their receipt time is retained separately and
attached to the next actual price message. Missing/sentinel timestamps remain
missing. A configurable `FIVEPAISA_MAX_SUBSCRIPTIONS` budget defaults to 3,000;
this is an application guard, not a promise about an account's broker limit.
Confirm your actual entitlements and concurrent-session limits with 5paisa.

```powershell
python -m app.instruments fivepaisa --download
```

The official [scrip master](https://xstream.5paisa.com/dev-docs/docFundamentals/scrip-master)
supplies exchange, derivative segment, token, CE/PE/XX, expiry, lot size and
available multipliers. XX is treated as FUT only in validated derivative
segments. New IDs include exchange as well as segment and token, preventing
NSE/BSE token collisions. Existing valid saved IDs remain readable.

## Option chains and unavailable capabilities

Select a broker, exchange, underlying and expiry. The live universe contains
that broker's imported NSE/BSE/MCX options, including stocks and indices present
in its current master. Browsing subscribes only the selected expiry, with a
10-minute viewing subscription window. Open/pending trade contracts are
retained separately and continue receiving data when the browser closes.
Prices are read from the same broker's cache; chains do not journal hundreds
of inactive strike histories. A missing or stale row is labelled and retains
its original quote time. The paper fill engine validates each chosen leg
again before allowing a simulated fill.

The [5paisa Greeks documentation](https://xstream.5paisa.com/dev-docs/market-data-system/option-greeks)
describes a separate NSE-only feed, not Greeks within MarketFeedV3. Its public
page lists subscriptions and result fields but does not fully specify binary
wire framing for this implementation to validate. That separate decoder and
real-account entitlement check remain pending. Kotak depth messages likewise
do not provide Greeks. Therefore IV/Greeks remain **unavailable** rather than
using another broker, generated values or claiming an untested integration.
Spot, previous OI/volume and buildup also remain unavailable when not actually
provided. Futures appear in the trading catalog, not as CE/PE chain rows.

Official read-only margin APIs exist, but this release does not yet call them.
Displayed margin and hedge benefit are explicitly estimates, never official
broker funds or a guarantee. MCX contracts with an unknown monetary
multiplier are blocked from live simulated fills until verified.

## Watchlists and persistent cache

`INSTRUMENT_MASTER_PATH` selects the validated JSON cache; use a path under
RamNaym's persistent `/app/data` volume. Downloads are bounded to 100 MiB per
refresh and the combined cache to 256 MiB. Import files are atomically
replaced; failed parsing retains the prior catalog. Refresh each trading day
and after exchange contract changes. Permanent trades remain in PostgreSQL.

`FEED_INSTRUMENT_IDS` is an optional comma-separated watchlist of actual IDs
copied from the validated catalog. The active subscription union contains
that watchlist, active/pending position contracts and the recently viewed
chain. An empty watchlist never means subscribe the complete market.
Current broker tokens and quantities must come from masters, not these
structural examples:

```text
kotak:nse_fo:<actual-token>,fivepaisa:NSE:D:<actual-token>
```

MCX permission, multiplier, holiday and session rules vary by contract. The
server's maintained, dated exchange calendar determines recording sessions.
An absent or expired calendar is **unknown** and fails closed for recording;
weekday assumptions do not override official special sessions or holidays.
`market_session()` uses the same verified calendar unless an explicit legacy
operator-maintained dated calendar is supplied for display purposes.

## Trial checklist and status meanings

During an actual trading session on server #1606, verify both broker logins,
independent quote prices/times, token expiry, a forced transport disconnect,
resubscription after container restart, paper entry/partial exit/full exit,
and one-second capture while all website tabs are closed. Do not call any real
broker order route. Retain measured results and recording gaps in the trial
report; offline parser tests alone do not establish live connectivity.

| Status | Meaning/action |
| --- | --- |
| Not configured | Add the selected broker's required private backend credentials. |
| Awaiting instruments | Import the current master and select actual contracts. |
| Authentication required | Repeat the broker's supported daily login/session flow. |
| Reconnecting | A transport interruption is being retried with bounded backoff. |
| Error | Correct master, permissions or subscription budget; no fake prices are substituted. |
| Connected but stale | Inspect actual exchange session, quote receive time and broker access. |
| Disconnected | The backend stopped the connection; stale marks remain timestamped. |


## Private connection screens (26 October 2026)

Open **Broker connections** on the owner-only website. **Set up Kotak Neo**
accepts the application key, registered mobile, client code, MPIN and
Base32 authenticator setup key. These values are encrypted before database
storage with the backend-only `BROKER_CREDENTIAL_KEY`; they are never
returned by the status API. Preserve that key privately for recovery.
Saving credentials does not prove authentication or a live feed worked.
The existing read-only collector authenticates and subscribes when eligible
contracts are selected during an exchange session.

For **5paisa**, private server settings require `FIVEPAISA_APP_KEY`,
`FIVEPAISA_ENCRYPTION_KEY`, `FIVEPAISA_USER_ID`, `FIVEPAISA_REDIRECT_URI`
and `BROKER_CREDENTIAL_KEY`. Register this callback on the broker application:

`https://personal-options-trader.vercel.app/api/brokers/fivepaisa/callback`

Choose **Sign in to 5paisa**. Enter the broker PIN/OTP only on the official
5paisa page. The backend exchanges the returned request token over verified
HTTPS, encrypts the access token and client code in the private database,
and restores unexpired credentials after restart. The documented expiry is
23:59 IST each day; there is no invented refresh token. State is one-use,
short-lived, owner-session-bound and fenced against overlapping sign-ins.
The authorization redirect includes the application key only when opening
the official broker destination. Callback query strings are redacted in
application logs. Status endpoints contain no submitted secrets.

Refresh that broker's contracts after sign-in, then choose a valid contract
in Market watch or Option chain. An authenticated token alone is not a
verified streaming connection. Actual permissions, socket reception and
market-hours performance still require authenticated account testing.


## Live-feed corrections — 27 October 2026

5paisa zero, blank or absent last prices on mapped contracts produce no usable
quote. They retain the original OI receipt and do not stop other contracts.
Malformed prices and rejected authentication remain explicit failures.

Current Kotak native derivative masters with `lPrecision=2` encode strike and
tick prices in paise. Convert those fields once; explicit normalized prices
remain in human units. The [official SDK search implementation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/services/scrip_search.py)
compares native strike with the requested human strike multiplied by 100.
Unsupported native precision fails validation. Direct MCX/BSE expiry epochs
retain the business date used by that SDK; NSE retains its documented epoch
offset and IST conversion. Native unknown commodity multipliers (-1/0)
retain the contract for viewing, with fills blocked until monetary conversion
is verified. They cannot turn an unverified estimate into a broker value.

On 27 October, the owner completed actual 5paisa OAuth and the server imported
its official master. The socket subscribed to 522 NIFTY contracts, then exposed
the unavailable-price bug corrected above. These observations confirm login,
master access and socket initiation, not sustained quote delivery or load capacity.
