# Kotak Neo read-only margin diagnostics

Inspected on 30 October 2026. These checks do not place, modify or cancel real orders.

## Supported official interface

The application pins `kotakneoapi==3.0.9` in `backend/requirements-brokers.txt`. The downloaded official 3.0.9 wheel was inspected directly: `NeoAPI.margin_required()` accepts one exchange segment, instrument token, direction, product, quantity, price and order type. It invokes `MarginAPI.margin_init()`, which sends the margin check to the SDK-configured margin route. There is no list of hypothetical option legs in this request.

The user's older v2 repository is archived. Its scalar request matches the same limitation. The current SDK documentation also describes a single-order check for a real client account:

- [Current official margin_required documentation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/portfolio/margin_required.md)
- [Current official implementation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/services/margin.py)
- [Archived v2 documentation supplied by the user](https://github.com/Kotak-Neo/Kotak-neo-api-v2/blob/main/docs/Margin_Required.md)

No officially documented complete isolated hypothetical basket-margin API was verified in these sources. The [official website calculator](https://www.kotakneo.com/calculator/margin-calculator/) offers a basket UI; that does not establish a public supported API contract for its internal requests. This implementation does not reverse engineer or call an undocumented calculator endpoint.

## Implementation

`backend/app/kotak_margin.py` implements:

- `single_order_request()`: validate a genuine server-resolved Kotak contract, unexpired ISO date, supported derivatives segment, positive integral lots and broker lot size, direction, finite price and NRML/MIS product. The broker quantity is `lots × lot_size`, without applying a P&L monetary multiplier a second time.
- `ReadOnlyKotakMargin.check()`: invoke only `client.margin_required(**request)`, using the already authenticated feed's REST client. A separate login is never triggered by a margin diagnostic. Requests run outside the event loop, one at a time, with a five-second cooldown and the existing SDK REST timeout.
- `safe_single_order_audit()`: return an allowlisted structured event with source, SDK version, request fingerprint, UTC/IST timestamp and the recognized numeric response values. No account IDs, tokens, arbitrary SDK messages, headers or real funds balances are retained.

`KotakFeed.margin_required()` and `FeedManager.margin_required()` delegate to that service. The authenticated client is retained only for the feed lifetime and cleared when the stream ends. A websocket-only `KOTAK_SID` configuration has no authenticated REST client and therefore returns an explicit unavailable error instead of logging in automatically.

The protected route must resolve the supplied instrument ID against its own catalog, require owner authentication and CSRF, apply rate limits, and call the method only for an explicit diagnostic. Never call it for every quote or every one-second P&L snapshot.

## Meaning of returned values

`order_margin` is the SDK response's `ordMrgn`; `required_margin` is `reqdMrgn`. They are **single-order real-account diagnostics**, which may be affected by existing real positions and account state. A recognized successful response has `status=single_order_response` and `verified=true` only in that narrow scope. It always has `paper_portfolio_verified=false`.

The following portfolio fields remain null: SPAN, ELM, additional margin, total basket margin, hedge benefit and final capital required. Do not sum diagnostics across paper legs. Do not use maximum expiry loss as margin. Do not derive hedge benefit from these results. Do not label the result as an isolated paper basket or exchange-SPAN calculation.

Failures, malformed/negative/nonfinite amounts, expired authorization and undocumented response shapes fail closed with a fixed safe message. No raw exception or broker response string is exposed.

## Verification

Offline regression tests exercise exact SDK arguments, 65-unit quantities, MCX quantity conversion once, non-portfolio scope, secret redaction, invalid contracts/premiums/quantities, broker failures, cooldown, in-flight exclusion and reuse of the existing authenticated client. All broker HTTP is mocked. These tests do not certify a live authenticated margin response or establish exchange-basket parity.


## Owner controls and screenshot correction — 14 November 2026

The existing trade Risk tab and paper-trade review now provide an explicit **Check single order** action. Select one genuine unexpired derivative leg and NRML or MIS. The app shows the exact side, lots, broker quantity and reference price being checked. This reuses the current authenticated Kotak REST client; it never logs in or reconnects the collector for a margin request. If the feed has ended or only a websocket session exists, it reports unavailable. No request is automatic, no leg results are summed, and no real order is submitted.

The response displays `ordMrgn` and `reqdMrgn` under their exact API field names. They remain account-aware diagnostics, not verified isolated basket margin, exchange SPAN/ELM, hedge benefit or final capital. A response is bound to its inputs, hidden after exposure/product/input changes, and expires from the current display after at most 60 seconds. A 30-second browser timeout and existing backend/SDK bounds prevent an indefinite wait. Stale, inactive, invalid-expiry or expired master selections are rejected by the server before broker HTTP. Only allowlisted numeric response values, source, timestamp and request fingerprint enter the protected audit event; secrets and arbitrary SDK errors never do.

Option premium paid, received and signed net premium are calculated separately using Decimal from the **remaining open option quantities at entry prices**, excluding charges. They are not the complete historical cash ledger after adds/exits, nor are premium credits buying power. Futures do not contribute option premiums. Unknown MCX monetary units remain unavailable. Current master evidence stores native `lLotSize=1250` for NATURALGAS and `100` for CRUDEOIL; the margin request uses lots times that field once, without multiplying by monetary conversion again.

Closed trades previously relabelled the constant remaining-position payoff as maximum expiry profit and zero as maximum loss. Closed/cancelled trades now show recorded gross realized P&L, charges and final net P&L, with **Not applicable — no remaining exposure** for expiry forecasts. Original entry risk is not reconstructed from closed weighted-average legs. Owner records and portfolio screenshots remain private; public regression cases use synthetic amounts.

The public [Kotak calculator](https://www.kotakneo.com/calculator/margin-calculator/) currently exposes NSE and BSE in its exchange selector, so an MCX NATURALGAS numerical comparison could not be completed there. No undocumented calculator endpoint was used. Kotak also [explains that calculator and blocked margins may differ as intraday risk files change](https://www.kotakneo.com/support/why-is-the-margin-blocked-different-from-what-i-see-on-the-margin-calculator/). A same-time, same-contract, same-product official MCX basket reference is still needed before any basket adapter can be certified. This release does not claim live authenticated broker-margin parity.

Local release validation: 953 backend/frontend regression tests passed, TypeScript passed, production Vite build passed and the allowlisted RamNaym ZIP/checksum manifest passed. One existing Starlette/httpx deprecation warning remains. Authenticated Kotak responses and numerical basket comparison were not live tested.
