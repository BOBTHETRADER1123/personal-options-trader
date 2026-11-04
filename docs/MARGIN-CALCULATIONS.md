# Paper basket margin audit

Updated 4 November 2026; official documentation and calculator observations were made on 30 October 2026. The application remains paper trading only and currently supports Kotak Neo only. No real broker orders or live broker margin requests were submitted for this audit. Automated broker responses are mocked.

## Kotak Neo

The adapter previously implemented authentication and read-only market data without `margin_required()`. This change adds the pinned official 3.0.9 SDK method as an explicit owner-only, read-only single-order diagnostic, using the already authenticated client. It validates server-resolved contracts and exact broker quantities, serializes requests with a cooldown, and does not run on quote or P&L ticks. See [implementation details](KOTAK-SINGLE-ORDER-MARGIN.md). The [official SDK method](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/neo_api.py) and [method documentation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/portfolio/margin_required.md) accept one instrument, transaction direction, quantity, price, product and order type. The public contract does not accept a hypothetical basket or document a portfolio SPAN/ELM/hedge-benefit breakdown. The response documents account/order checks, including `ordMrgn` and `reqdMrgn`. These must not be added and presented as hedged paper portfolio margin; actual account positions may also affect an order check.

No officially documented Kotak hypothetical multi-leg basket API has been verified in the current public SDK. This is a limitation of verified integration evidence, not a claim that Kotak's own trading systems cannot calculate baskets. The app therefore displays **Broker margin unavailable**, with null SPAN, ELM, additional margin, total margin, hedge benefit and capital required. It supplies no invented estimate. Net option premium and theoretical expiry loss remain separate calculations.

## Official calculator comparison

Kotak's [official margin calculator](https://www.kotakneo.com/calculator/margin-calculator/) permits multiple selected F&O contracts and shows SPAN/exposure and basket benefits. This website feature is not proof of a supported public basket API.

For a valid comparison, select the exact NSE NIFTY expiry and verified units for all five requested legs, use the same product and assessment time, then record the displayed basket components and final requirement. The calculator's published market information may be delayed; entry premiums are not substitutes for its current margin inputs. Do not compare against a sum of independent leg quotes, expiry loss, or another product.

**Numerical comparison status:** the official calculator was opened on 30 October, but selection/search did not yield a verified basket result. NATURALGAS search displayed “No results found” before complete exchange/segment selection; this does not establish instrument unavailability. No verified calculator result for the five specified legs has been captured. No numerical broker margin or hedge benefit is claimed to have passed comparison. Record the actual result and timestamp before marking this check complete.

## 5paisa research only

The [official Multi Order Margin documentation](https://xstream.5paisa.com/dev-docs/funds-management-system/Multi%20order%20margin) documents a set of hypothetical orders, combined margin including hedge benefits and a `CoverPositions` flag. The documented response provides `TotalMarginRequired`, not separate SPAN/ELM/additional or a standalone hedge-benefit amount. The [official SDK](https://github.com/OpenApi-5p/py5paisa/blob/master/py5paisa/py5paisa.py) exposes `multi_order_Margin`. Its account-funds `margin()` call is a different operation. 5paisa remains disabled; none of these APIs were connected, authenticated or tested in this Kotak-only application.

## Provenance and safe diagnostics

`backend/app/margin.py` is a pure availability assessment. It performs no network requests, order calls or automatic logging, including during one-second P&L updates. Each response identifies broker, official documentation, assessment time, complete paper portfolio scope and a hash of remaining contract exposures. A change in lots, remaining legs, expiry, strike or broker changes the assessment identity. An assessment timestamp does not imply a verified broker quote.

`safe_margin_audit()` produces optional structured diagnostics for an explicit owner request. Only recognised numeric response amounts are retained, under a diagnostic-only scope. Credentials, headers, account identifiers, arbitrary SDK messages and unknown payloads are omitted. Single-order debug amounts never populate verified basket fields. Keep audit events bounded and do not log a margin event on every P&L tick.

Future verified basket support must establish a documented read-only hypothetical-portfolio contract, resolve actual broker instrument tokens and products, match every remaining leg and quantity, state whether existing actual positions are included, validate response freshness and retain source provenance. Components absent from the official response must remain unavailable. Do not infer final capital by adding premium to a broker total without verifying whether premium is already included.

## Complete audit and exchange work

The [calculation audit](CALCULATION-AUDIT-2026-10-30.md) reproduces the requested CE portfolio, distinguishes the actual saved PE contract, and explains the old expiry-loss margin cap. The [exchange margin boundary](EXCHANGE-MARGIN-DATA.md) records official-file access results and the missing validation required for full SPAN. Download/quarantine metadata and a 16-scenario scanning diagnostic do not constitute a verified complete exchange margin model. Exchange portfolio components remain null until current parameters, all applicable rules and independent numerical reconciliation pass.
