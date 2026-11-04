# Calculation audit: expiry payoff and margin provenance

Updated 4 November 2026. The saved-contract and official-source observations below were made on 30 October 2026; they are not fresh 4 November market quotes or parameter-file checks. This report covers the existing Kotak-only, paper-trading application. No real orders were placed, modified or cancelled. The website layout is preserved.

## Finding and preservation of the actual saved contract

The requested five-leg regression uses a **BUY 22,950 CE**. The inspected saved trade instead contains **BUY 22,950 PE**, symbol `NIFTY26N0322950PE`, broker token `53871`. Replacing PE with CE would change the actual recorded transaction and its expiry payoff. The fix preserves the stored contract, original fills, timestamps and history, and tests both portfolios independently.

For the actual PE portfolio, the displayed maximum profit **INR 1,471,609.81** and breakeven **22,745.08** are reproduced analytically. The maximum occurs at a nonnegative settlement of zero: the extra long put produces substantial downside intrinsic value. Those two results are not evidence of a chart-range calculation bug for that saved portfolio. They are inconsistent with the separately specified CE portfolio because the contract types differ.

The margin **INR 20,140.19** and hedge benefit **INR 603,502.31** have a different cause: the former was derived by capping a heuristic margin at expiry loss, and the latter by subtracting that cap from an arbitrary standalone estimate. Neither number was a verified broker basket-margin response.

## Exact expiry-payoff formula

For each remaining leg i:

- `q_i = open_lots_i * contract_lot_size_i * verified_monetary_multiplier_i`.
- `d_i = +1` for BUY and `-1` for SELL.
- Call intrinsic: `I_i(S) = max(S - strike_i, 0)`.
- Put intrinsic: `I_i(S) = max(strike_i - S, 0)`.
- Futures terminal value: `I_i(S) = S`, with the futures entry price subtracted below.

For one broker, exchange, underlying and expiry, and settlement `S >= 0`:

```text
P_gross(S) = realized_PnL + sum[d_i * q_i * (I_i(S) - entry_price_i)]
P_net(S)   = P_gross(S) - supplied_estimated_charges
```

Charges are subtracted once. Realized P&L from already closed quantity is an offset; only remaining quantity contributes future intrinsic payoff. Future exit charges are included only when supplied by the caller. An estimated charge is not presented as an official broker charge.

The payoff is linear between zero and each distinct option strike. Evaluate the knots and solve every linear interval crossing. If an entire interval has zero payoff, return that interval rather than inventing one breakeven. Beyond the highest strike:

```text
upper_tail_slope = sum[d_i * q_i for CE and FUT legs]
```

A positive slope means unlimited theoretical profit; a negative slope means unlimited theoretical loss. Zero slope has a finite terminal plateau. Decimal arithmetic calculates extrema and roots; display amounts are rounded only at the output boundary. The chart samples illustrate this analytical result and do not determine maximum profit, maximum loss or breakevens.

Mixed expiries, underlyings, exchanges or brokers are returned as separate analytical groups. There is no invented single settlement or cross-group hedge credit. Shared realized P&L and charges are explicitly unallocated in the group breakdown. A fully closed trade has a constant realized result after supplied costs, with no open-position payoff graph.

## Requested NIFTY CE regression

All five legs have expiry **3 November 2026**, one lot of **65 units**:

| Direction | Strike | Type | Entry premium |
|---|---:|---|---:|
| SELL | 22,700 | CE | 185.10 |
| SELL | 22,700 | PE | 146.50 |
| BUY | 22,850 | CE | 111.35 |
| BUY | 22,950 | CE | 286.20 |
| BUY | 22,550 | PE | 91.95 |

```text
Net premium = 65 * (185.10 + 146.50 - 111.35 - 286.20 - 91.95)
            = -10,263.50   (debit)

Gross maximum loss = 10,263.50 + 150 * 65 = 20,013.50
Net maximum loss   = 20,013.50 + 126.69 = 20,140.19
Upper breakeven    = 22,950 + 20,140.19 / 65
                   = 23,259.8490769... (23,259.85 displayed)
Maximum profit    = Unlimited
```

The upper tail has one net long call-equivalent, or INR 65 of additional payoff per one-point rise. The high-strike CE entry premium 286.20 exceeds the lower-strike CE entry premium 111.35. The app warns about this inconsistency and asks the owner to compare original fill times and bid/ask. Different-time fills do not establish simultaneous arbitrage, and the provided prices are not silently changed.

The saved **PE** version has the same net premium and maximum loss but a different payoff:

| Metric | Requested 22,950 CE | Actual saved 22,950 PE |
|---|---:|---:|
| Premium debit | 10,263.50 | 10,263.50 |
| Gross maximum loss | 20,013.50 | 20,013.50 |
| Maximum loss with 126.69 costs | 20,140.19 | 20,140.19 |
| Maximum theoretical profit after costs | Unlimited | 1,471,609.81 |
| Breakeven after costs | 23,259.85 | 22,745.08 |

## Current mark-to-market is a separate calculation

```text
MTM_gross = realized_PnL + sum[d_i * remaining_quantity_i * (latest_mark_i - entry_price_i)]
MTM_net   = MTM_gross - accumulated_estimated_charges
```

The screenshot's known marks, in the table's leg order, are **329.95, 49.55, 217.90, 123.75 and 27.95**. Applying these supplied marks gives gross **INR -10,907.00** and net **INR -11,033.69** after INR 126.69 rounded costs. The fourth observed mark belonged to the actual **PE** contract. Reusing 123.75 in the hypothetical CE regression demonstrates arithmetic only; it does not establish that the CE had that market quote.

Original quote timestamps are retained. A one-second calculation does not certify a fresh quote; stale, reused, invalid or future-dated observations must remain identified. This audit does not claim those last-known marks are current market prices.

## NATURALGAS regression and old margin reproduction

Expiry **20 November 2026**, **1,250 monetary units** each: SELL 300 PE at 24.05 and BUY 290 PE at 18.15.

```text
Credit                  = (24.05 - 18.15) * 1250 = 7,375.00
Gross maximum profit    = 7,375.00
Gross maximum loss      = (300 - 290) * 1250 - 7375 = 5,125.00
Estimated supplied costs= 66.38
Net maximum profit      = 7,308.62
Net maximum loss        = 5,191.38
Net breakeven           = 300 - (7375 - 66.38) / 1250 = 294.153104
```

The old model approximated standalone requirement using long premium costs and an arbitrary 20% spot notional for shorts/futures. It then used:

```text
old_margin = min(standalone_heuristic, maximum_expiry_loss + max(net_credit, 0))
old_hedge_benefit = standalone_heuristic - old_margin
```

At reference spot **295**, this exactly reproduces the reported NATURALGAS numbers:

```text
Long premium cost       = 18.15 * 1250 = 22,687.50
Short heuristic         = 295 * 20% * 1250 = 73,750.00
Standalone heuristic    = 96,437.50
Old capped margin       = 5,191.38 + 7,375 = 12,566.38
Old claimed hedge benefit = 96,437.50 - 12,566.38 = 83,871.12
```

This is a reproduction at the stated spot, not proof that 295 was the exact timestamped production margin input. For NIFTY, **623,642.50 - 20,140.19 = 603,502.31** reproduces the old hedge-benefit output. Neither subtraction verifies a hedged portfolio margin. The new implementation removes these heuristics and returns unavailable values.

## Kotak margin implementation and validation boundary

The previous adapter did not implement `margin_required()`. The current change adds a read-only adapter for the official pinned **kotakneoapi 3.0.9** scalar method, exposed only through an authenticated, CSRF-protected owner diagnostic. It uses the already authenticated REST client, verified server-side contract identity and `lots * broker_lot_size`; it does not initiate a separate login or apply the monetary price multiplier twice. Calls run outside the event loop, are serialized and cooled down, and are never made for each quote or one-second P&L tick.

The official public request accepts one order. A documented complete, isolated hypothetical multi-leg Kotak basket endpoint was not verified. Its `ordMrgn` and `reqdMrgn` values are single-order real-account diagnostics and may depend on actual account state. They are never summed or published as paper portfolio SPAN, ELM or hedge benefit. A successful scalar response has verification only within that explicitly narrow scope.

The app displays **Broker margin unavailable** for the paper basket until a supported complete portfolio calculation is verified. Separate nullable fields cover SPAN, ELM, additional margins, basket total, hedge benefit and final capital. Net premium and expiry risk remain available independently. Simulated capital checks cover known premium cash costs and paid charges, explicitly disclose that broker margin is unavailable, and do not label unknown margin as zero.

Safe diagnostics retain source, SDK version, exposure/request fingerprint, UTC/IST timestamps and recognized numeric response fields. They exclude secrets, authorization headers, account identifiers, raw arbitrary messages and funds balances. No real order API is invoked.

See [Kotak single-order implementation and tests](KOTAK-SINGLE-ORDER-MARGIN.md) and [broker research](MARGIN-CALCULATIONS.md).

## Exchange SPAN work and outstanding evidence

The exchange module implements bounded official HTTPS downloads, archive integrity and metadata checks, date/version provenance, quarantine status and a Decimal aggregation diagnostic for 16 aligned scenario arrays. This diagnostic is not a complete production SPAN engine. No caller-supplied flag can activate an unvalidated model.

A complete calculation still needs verified current exact-contract risk arrays, scaling, spread/combined-commodity rules, short-option minimum, exchange net-option valuation and premium settlement, ELM, additional/special/expiry/devolvement obligations, broker additions and independent reference reconciliation. Expiry spread width or received premium cannot substitute for those rules.

Observed **30 October 2026** access results:

- MCXCCL's listed 28 October ZIP returned HTTP 403. The newer official MCX mirror's `mcxrpf-20261029-1900-07-i.zip` also returned HTTP 403 at 08:12 IST. Neither was parsed or used.
- The retrieved NSE explanatory page did not supply an applicable current downloadable file.
- The ICCL parameter page timed out.
- Kotak's official calculator was opened, but contract selection/search did not produce a verified numeric result; NATURALGAS search displayed “No results found” while required selection was incomplete. This is not evidence that Kotak does not support the contract. No basket-parity comparison passed.

Therefore **genuine exchange SPAN, verified Kotak basket margin and numerical calculator parity remain untested/unavailable**. The change deliberately withholds amounts instead of publishing plausible-looking estimates. No anti-bot bypass, undocumented calculator endpoint or paid data service was used. Details and acceptance conditions are in [exchange margin data](EXCHANGE-MARGIN-DATA.md).

The earlier 5paisa research documents an official combined multi-order margin interface, but 5paisa stays disabled. It was not authenticated or tested as part of this Kotak-only change.

## Regression coverage and release checks

Automated fixtures cover the requested CE portfolio, actual saved PE interpretation, NATURALGAS spread, finite and unlimited tails, all roots/zero intervals, mixed settlement groups, lot/quantity changes, partial closes and realized offsets, stale and future timestamps, malformed financial inputs, safe margin display, scalar diagnostic scope and official-file quarantine boundaries. Broker responses and exchange fixtures in these automated tests are mocked or synthetic.

Final local release checks on 4 November: 734 backend tests passed, 0 failed, with one test-library deprecation warning. TypeScript and Vite production builds passed. The RamNaym runtime ZIP passed its file allowlist and checksum manifest checks. GitHub and production deployment status are recorded separately in FEED-CONNECTION-UPDATE-2026-11-04.md. No live authenticated broker-margin response, validated full exchange-SPAN result or official numerical calculator comparison has passed in this audit.

## Official sources consulted on 30 October

- [Current Kotak margin method](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/portfolio/margin_required.md)
- [Current Kotak SDK](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/neo_api.py)
- [Archived v2 margin documentation supplied by the owner](https://github.com/Kotak-Neo/Kotak-neo-api-v2/blob/main/docs/Margin_Required.md)
- [Kotak official margin calculator](https://www.kotakneo.com/calculator/margin-calculator/)
- [NSE equity derivatives risk files](https://www.nseindia.com/static/products-services/equity-derivatives-span-risk-parameter-files)
- [NCL SPAN methodology](https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span)
- [MCXCCL risk files](https://www.mcxccl.com/risk-management/daily-span-risk-parameter-file)
- [MCXCCL risk and devolvement FAQ](https://www.mcxccl.com/risk-management/faq)
- [ICCL risk parameter page](https://www.icclindia.com/riskmgmt/RiskparameterNEW.aspx)
- [5paisa research-only multi-order margin documentation](https://xstream.5paisa.com/dev-docs/funds-management-system/Multi%20order%20margin)
