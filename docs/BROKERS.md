# Read-only broker data setup

The application records paper orders in its own database. The adapters in
`backend/app/brokers/` contain market-data code only. Connecting a broker does
not turn on real order submission. Credentials are read from the backend
environment; never add them to Vercel frontend variables, spreadsheets, GitHub,
screenshots, or browser storage.

The adapter protocols were checked against the official sources linked below
on 20 October 2026. Account authentication, feed entitlements, real quotes,
subscription limits for your account, and reconnection need testing with your
own broker accounts during their actual market sessions. No account credentials
were provided during implementation, so the offline tests do not certify that
your account is connected.

## First try the sample feed

1. Start the app and sign in.
2. Leave **Practice data** enabled in Settings.
3. Record a paper trade, add lots, and close it. Every sample quote and contract
   is labeled synthetic. The example expiry dates, lot quantities, prices,
   tokens, and underlying values are practice fixtures, not exchange-valid
   contract information.
4. Configure live feeds only after the basic app and Excel workflow work.

## Dhan

1. Open the official [DhanHQ documentation](https://dhanhq.co/docs/v2/) and your
   Dhan developer dashboard. Activate the required derivatives/MCX segments
   and obtain market-data access. A Dhan trading account alone must not be
   treated as proof of Data API access.
2. Create a client ID/access token in the official account flow. Put
   `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in the backend environment.
3. From the `backend` folder, with the project's Python environment activated,
   import the current public master:

   ```powershell
   python -m app.instruments dhan --download
   ```

4. Choose a small watchlist from that master (see **Watchlists** below), then
   restart the backend or use the app's instrument reload control.
5. Disable Practice data in Settings. Check that quotes say **Dhan**, have
   current receive times, and become stale when the feed is stopped.

The [Dhan v2 feed documentation](https://dhanhq.co/docs/v2/live-market-feed/)
defines a binary WebSocket. This adapter requests Full packets, parses LTP,
best bid/ask, volume and open interest, and batches subscriptions in groups of
100. It uses one connection, with the documented 5,000-contract limit.
Permission/token failures stop retries and appear in feed status. The current
[instrument master](https://dhanhq.co/docs/v2/instruments/) supplies tokens,
expiries, strikes and lot sizes. No real order API is called.

## Kotak Neo — current SFeed

1. In Kotak Neo, find **More → Trade API**, create the application and obtain
   its consumer key. Register TOTP through the broker's API dashboard.
2. Install the current official SDK from the `backend` folder:

   ```powershell
   python -m pip install -r requirements-brokers.txt
   ```

3. Set `KOTAK_CONSUMER_KEY`, `KOTAK_MOBILE_NUMBER` (with country code),
   `KOTAK_UCC` and `KOTAK_MPIN` on the backend. Set `KOTAK_TOTP` to a fresh
   authenticator code immediately before starting. The code expires quickly;
   update it if authentication fails. An optional `KOTAK_TOTP_SECRET` can
   generate codes on the backend; protect it as a second-factor secret.
4. An already authenticated SFeed session can instead use `KOTAK_UCC` and
   `KOTAK_SID`. The SID is sensitive and expires; renew it through the broker's
   authentication flow. Never paste it into chat or the app's browser console.
5. Import each required current segment, using the official SDK to resolve
   today's CSV URL:

   ```powershell
   python -m app.instruments kotak --download --segment nse_fo
   python -m app.instruments kotak --download --segment bse_fo
   python -m app.instruments kotak --download --segment mcx_fo
   ```

6. Set the watchlist, restart/reload, and check current **Kotak Neo** quotes.

The official [kotak-neo-python repository](https://github.com/Kotak-Neo/kotak-neo-python)
is the maintained SDK. This project pins `kotakneoapi==3.0.9` and uses async
SFeed `subscribe_depth`; the obsolete callback subscription API is absent.
The [SFeed guide](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/websocket.md)
documents 3,000 tokens and SDK reconnect/resubscribe. `ack_wait_timeout=0`
avoids intentionally coalescing price events while waiting for symbol
acknowledgments; symbols come from the imported master.

Kotak's [official instrument documentation](https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/market-data-apis/instruments.md)
specifies that NSE/CDE `lExpiryDate` requires a 315511200-second adjustment,
whereas BSE/MCX uses Unix epoch directly. The importer follows that rule. It
does not guess expiry dates from a trading-symbol string. CSV schema changes
fail validation and retain the existing master.

## 5paisa Xstream

1. Use the official [Xstream integration flow](https://xstream.5paisa.com/dev-docs/docFundamentals/integration-flow)
   to authorize the account and obtain the current access token and client
   code. Tokens are session secrets and must be renewed when they expire.
2. Put `FIVEPAISA_CLIENT_CODE` and `FIVEPAISA_ACCESS_TOKEN` on the backend.
3. From `backend`, import the official current master:

   ```powershell
   python -m app.instruments fivepaisa --download
   ```

4. Configure the watchlist, restart/reload, turn off Practice data, and verify
   source labels and timestamps.

The adapter uses the documented [MarketFeedV3 WebSocket](https://xstream.5paisa.com/dev-docs/market-data-system/web-socket)
and its exchange/segment/scrip-code subscriptions. Missing bid, ask or exchange
timestamps remain missing. An invalid sentinel date is never presented as a
fresh exchange timestamp. The [scrip-master documentation](https://xstream.5paisa.com/dev-docs/docFundamentals/scrip-master)
defines NSE, BSE and MCX records. The app does not call order-confirmation or
order-management services.

## Watchlists and master refresh

`INSTRUMENT_MASTER_PATH` optionally selects an absolute path for
`instruments.json`. Without it, the backend uses `backend/data/instruments.json`.
The import command merges the selected broker/segments without overwriting
unrelated brokers. A malformed import retains the previous file.

Set `FEED_INSTRUMENT_IDS` to comma-separated **IDs copied from that file**:

```text
FEED_INSTRUMENT_IDS=dhan:NSE_FNO:<actual-token>,kotak:nse_fo:<actual-token>,fivepaisa:D:<actual-token>
```

Those are structural examples, not usable token values. Include every contract
that you intend to paper trade and every source you intend to compare. A full
master can exceed subscription limits. The app reports that error; it does not
silently subscribe only the first few instruments. With no explicit watchlist,
all imported contracts are requested and provider limits still apply.

Refresh masters before each trading day and after contract changes. Reload the
catalog/feed after importing. On Render, use a durable disk path or reproduce
the fresh master on startup before enabling live quotes. A free ephemeral
filesystem can disappear after a restart. SQL trade records belong in Neon;
do not rely on the frontend to keep a feed alive.

If a broker changes its CSV columns, supply an explicitly normalized CSV:

```csv
token,symbol,underlying,exchange,segment,expiry,strike,option_type,lot_size,tick_size
<actual-token>,<actual-trading-symbol>,NIFTY,NSE,nse_fo,YYYY-MM-DD,<actual-strike>,CE,<actual-lot>,0.05
```

The normalized segment must be `NSE_FNO`/`BSE_FNO`/`MCX_COMM` for Dhan,
`nse_fo`/`bse_fo`/`mcx_fo` for Kotak, or `D` for 5paisa options. Use each
broker's native schema where supported. Do not import made-up token
values or copy expiry/lot rules from the sample catalog. MCX quote-unit and
contract-multiplier conventions must be verified against the broker master;
do not assume every commodity uses the same quantity convention. The importer
preserves a native `Multiplier`/`lMultiplier`/`MULTIPLIER` or explicit normalized
`price_multiplier`. An absent MCX multiplier remains unknown, and the paper
engine refuses that live contract until its monetary conversion is supplied.
For example, a broker lot size of 1 with a verified multiplier of 100 has a
monetary sensitivity of 100 per one-point price move per lot. NSE/BSE default
to a multiplier of 1. Demo MCX uses a separately labeled fictional convention.

## MCX sessions and holidays

MCX market hours differ from equity hours and can have separate morning and
evening holiday sessions. Its published [trading hours](https://beta.mcxindia.com/market-operations/trading-surveillance)
show a 09:00 IST opening; non-agri commodities generally close at 23:30 or 23:55
depending on the announced seasonal schedule. Agri categories have different
closes. The [official holiday calendar](https://www.mcxindia.com/market-operations/trading-surveillance/trading-holidays)
must be consulted for each day, including special sessions.

The helper `market_session()` intentionally returns **unknown** until an
operator supplies a dated exchange calendar. It never labels a weekday as
definitely open. Set `MARKET_CALENDAR_PATH` to a JSON file, for example:

```json
{
  "MCX:non_agri": {
    "2026-10-20": [["17:00", "23:30"]],
    "2026-12-25": []
  },
  "NSE": {
    "2026-10-20": []
  }
}
```

Update the file from exchange circulars. Empty intervals mean closed; an
absent date means unknown. A calendar indicates the scheduled session, not
an assurance of a particular instrument's availability, trading halt, or feed
freshness. Real quote receive times and the stale-feed indicator remain
separate from the schedule.

## Permissions, costs and limits

No paid subscription or trial has been purchased. Check each broker's current
developer portal for market-data charges, enabled segments, allowed usage,
subscription caps, concurrent-session rules, data redistribution restrictions,
and any static-IP or regulatory requirements for your account. A read-only app
still needs genuine data entitlements. Do not enter payment details until the
broker displays its applicable charge and you choose to proceed.

## Troubleshooting

| Status | What to check |
| --- | --- |
| Not configured | Required backend environment variables are missing. |
| Awaiting instruments | Import the current master and select matching watchlist IDs. |
| Authentication required | Renew the token/SID/TOTP and check account data access. |
| Error, limit exceeded | Reduce the explicit watchlist; do not invent extra connections. |
| Reconnecting | Network disconnected; retries back off and resubscribe. |
| Connected but stale | Market may be closed, contract inactive, token invalid, or feed stalled. Check actual exchange session and broker portal. |
| No matching contract | One broker's master lacks that exact underlying/expiry/strike/type. The paper action reports that broker's failure independently. |

Protocol errors are visible, not replaced with generated live prices. Switching
to Practice data is an explicit separate action. Offline parser tests use
synthetic packets. They cover timestamps, source labels, full-depth fields,
duplicate-price event preservation, malformed packet rejection, token expiry,
master validation and demo separation. Run real-account checks in a separate
session before relying on unattended market recording.
