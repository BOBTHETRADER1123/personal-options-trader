# Watchlists and quick paper trading

## Open your workspace

Open https://personal-options-trader.vercel.app/ and sign in with your app password. Watchlist is a trading tool. The three trading categories are **Stocks**, **F&O** and **Commodities**. Total P&L combines these categories; a trade is counted once.

## Build a list

1. Open **Watchlist**. Choose Watchlist 1, Watchlist 2 or Favourites, or create a named list.
2. Select **Add instrument** and choose Stocks, F&O or Commodities. Search the company or underlying symbol. For derivatives, include the expiry, strike and CE/PE/FUT when choosing the exact contract.
3. If Stocks reports that listings have not loaded, choose **Refresh stock listings** once. This requests the existing official Kotak cash master without opening another feed.
4. Add a current Kotak contract. Expired or unavailable entries remain visible with a warning so the app can retain their identity and your trade history.
5. Use manual order to drag rows, or use the up/down controls on a phone. Sorting by price change, percentage, volume or symbol changes the view; manual reordering changes the saved list.

Lists are stored in your private application database and shared by phone and desktop. The last selected list and appearance preferences are device preferences. Simultaneous edits use revision checks rather than silently overwrite another device's changes. If an edit conflicts, reload the list and apply your change again.

Limits keep the one-worker server bounded: ten lists, fifty instruments per list and one hundred unique saved instruments. Only the displayed list requests watchlist subscriptions. Open positions continue to retain their independent recorder subscriptions. No socket is opened for an individual row or a separate list.

## BUY, SELL and existing positions

- **BUY / SELL** opens a new paper-order panel for the exact instrument. Stocks use share quantity; derivatives use lots and the official contract lot size. Choose market or limit entry and review the estimated fill, trade value and charges.
- **Confirm Paper Trade** rechecks the quote and settings, then uses the existing paper execution engine. The backend remains responsible for the current contract, session, quote freshness, buying power and final fill. Significant price changes require another review.
- A limit that has not crossed remains pending; it is not shown as an executed market fill.
- The existing-position badge opens management controls. Use these to add quantity, partially exit or close an existing position. A new BUY/SELL order remains a separately identified paper trade.
- If a submission result is uncertain, retry keeps the same request identifier. Check Positions before creating a second order.

New trades are classified on the server. Cash shares belong to Stocks, exchange equity/index derivatives to F&O, and MCX derivatives to Commodities. Mixed-category new baskets are rejected. Watchlist removal does not close a trade or delete its journal, P&L history or export.

## Long-term equity paper holdings

Stocks means cash equity shares, including currently listed companies from the official Kotak cash master. Stock contracts have no expiry or six-month holding limit. Open holdings remain in the durable database until you explicitly sell/close them; daily exports and archiving must retain the active position. Use Manage existing position to partially sell or close an existing holding. A new SELL order is a separately identified paper position.

The app does not currently apply automatic verified dividends, stock splits or other corporate actions. Long-term results must be interpreted with that limitation until verified corporate-action handling is added. Future session calendars still require verified official data; an unknown calendar must not erase a holding or fabricate a fill.

## Read prices honestly

Quotes retain their own received and exchange timestamps. Disconnected or stale prices remain labelled as last-known data. The app never substitutes another broker or fills a genuine paper market order from a stale quote.

LTP, previous close, day open/high/low, volume and bid/ask are displayed when the official feed supplies them. Missing fields appear as `—`. Day prices use the official SFeed `open_price`, `high_price`, `low_price` and `close_price` fields; they are not reconstructed from the latest price. See [Kotak's official WebSocket guide](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/websocket.md).

Charges are estimates under your configured paper model. Unverified basket margin appears as **Broker margin unavailable**. Expiry loss is not used as a substitute for broker margin.

**CHART** opens a larger view of received price observations collected while this Watchlist is open. It keeps a bounded set of up to 300 observations per instrument. It is not an exchange historical-data or candlestick service, and missing periods are never backfilled with invented prices.

## Stable option chain

Open Option chain from the F&O or Commodities page, or workspace search. Choose Classic, Terminal or Aurora. These styles use the same Kotak-only source and preserve the same trading rules. The Sample chain switch is removed.

The strike column stays centred within the scrollable table. The ±5/±10 window stays anchored while quotes change. Choose **Go to ATM** to explicitly move it to the latest available ATM reference, or choose All strikes. Changing the instrument or expiry centres its initial view. Table widths and row identities remain stable during quote refreshes.

Basket payoff is indicative and based on the displayed premiums before charges. The exact existing payoff engine determines theoretical maximum profit/loss and breakevens. A bounded chart range never defines an unlimited theoretical result. Missing quantities, prices or unsupported mixed expiries show unavailable.

## Recording, exports and deployment

Confirmed paper orders use the same database journal, one-second recorder, exchange calendars, daily Excel generation and verified backup services as existing orders. Watchlist polling does not create P&L observations or alter market sessions.

The existing SQLite rollback journal (`DELETE`) and one application worker remain required. No database reset or data import is needed for this feature: a private settings row stores the watchlist definitions. Always retain `/app/data` when deploying. A backend deployment briefly restarts the single worker; any recording interruption must remain visible as a gap rather than be backfilled.

Automated tests use isolated fake market observations. Authenticated Kotak quotes, actual exchange-session continuity and production CPU/RAM require observation in a real session; a passing local test is not a live-feed certification.
