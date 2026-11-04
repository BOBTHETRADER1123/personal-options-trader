# Exchange margin data and validation boundary

Updated 30 October 2026. All trading remains simulated. This component never
places, modifies or cancels broker orders and never queries actual account funds
or positions.

## What is implemented

`backend/app/exchange_margin.py` provides:

- `exchange_margin_status(legs, risk_files=None, now=None)`: separate exchange
  margin provenance, UTC/IST assessment time, official source URLs, missing
  components and nullable amounts. Missing exact exchange identity is reported;
  the exchange is never guessed from an underlying name.
- `download_risk_file(exchange, url, expected_date)`: a single read-only, bounded
  download from an exact allowlisted public exchange HTTPS domain. Redirects,
  credentials in URLs, query strings, untrusted hosts, oversized files, archive
  traversal/symlinks/duplicates, XML entities and malformed XML are rejected.
  Failures expose safe diagnostics, without response bodies or credentials.
- `inspect_risk_file(...)`: SHA-256 for the archive and each member, file version
  label, requested date, optional internal point-in-time date comparison and
  transport/archive checks. **Quarantined does not mean SPAN validated.**
- `portfolio_scan_diagnostic(rows)`: exact Decimal aggregation of 16 aligned,
  already scaled INR-per-contract long-position loss arrays. BUY contract count
  is positive; SELL is negative. It rejects differing versions, timestamps,
  exchanges and combined commodities. This is an internal scanning diagnostic,
  **not SPAN, total exchange margin, broker margin or hedge benefit**.

No complete production exchange SPAN adapter has yet been validated. The public
assessment consequently returns `Margin unavailable`; SPAN, ELM, additional
margin, total margin, hedge benefit and final capital are null. A caller-supplied
`model_validated` flag cannot activate a calculator. No fixed percentage,
spread-width cap, maximum expiry loss or sum of single-order broker quotes is
substituted. The scan diagnostic is never returned as the final total.

File download is an explicit operator action, not a quote-tick or one-second P&L
operation. This avoids extra CPU, network traffic or changes to recording cadence.
For a one-off inspection, with `backend` on `PYTHONPATH`:

```text
python -m app.exchange_margin --exchange MCX --url DIRECT_OFFICIAL_FILE_URL --trading-date 2026-10-29
```

The command prints safe metadata and does not replace active parameters or write
trading state. A downloaded file requires independent semantic/units validation,
current effective-version checks, complete margin rules and reference comparison
before it may supply public figures.

## Official data sources checked

- NSE/NCL equity derivatives risk files:
  https://www.nseindia.com/static/products-services/equity-derivatives-span-risk-parameter-files
- NCL methodology:
  https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span
- NCL current parameter explanations:
  https://www.nseclearing.in/risk-management/equity-derivatives/span-risk-parameters
- ICCL/BSE risk parameter page (SENSEX):
  https://www.icclindia.com/riskmgmt/RiskparameterNEW.aspx
- MCXCCL risk files:
  https://www.mcxccl.com/risk-management/daily-span-risk-parameter-file
- MCXCCL methodology and devolvement FAQ:
  https://www.mcxccl.com/risk-management/faq
- Official NCL instructions for independent PC-SPAN validation:
  https://www.nseindia.com/static/products-services/equity-derivatives-pc-span

## Actual access results, not a live margin test

During this audit, the public MCXCCL page supplied 28 October file links. A direct
read-only HTTPS request to its listed
`mcxrpf-20261028-1500-05-i.zip` returned **HTTP 403**. Web retrieval of that same
file could not retrieve a cached ZIP. It was not downloaded, parsed or used as a
30 October applicable parameter set. No anti-bot/authentication bypass was used.
The MCX India mirror also listed the newer `mcxrpf-20261029-1900-07-i.zip`;
a bounded read-only request to that exact official link returned **HTTP 403** at
30 October 2026 08:12 IST. It was not parsed or used.
The NSE static explanatory page was readable, but did not expose an applicable
current downloadable F&O file in the retrieved page. The ICCL parameter page
timed out. Therefore no verified live file for the requested NATURALGAS contracts
was loaded and no genuine portfolio SPAN amount is reported as tested.

The current MCXCCL Daily Margin page lists NATURALGAS futures for 24 November 2026;
that does **not** establish the identity or expiry of the 20 November option.
Options and underlying futures must be mapped through verified exact contract
metadata, not matched by assuming identical expiry dates.

## What must be validated before a genuine result

NCL describes scanning 16 scenarios, intra-commodity/calendar spread risk and NOV.
MCXCCL describes comparing scanning plus intra-commodity risk with the short
option minimum, subtracting net option value and accounting for net buy premium.
It also describes additional ELM and option devolvement obligations. The precise
sign, scaling and settlement treatment must match the applicable exchange files
and rules, not entry premiums guessed from a screenshot.

A production model needs all of the following:

1. Exact token/symbol, exchange, option expiry, underlying futures identity,
   lot/price multiplier and risk-array scaling, with a current effective file.
2. Correct combined-commodity groups and permitted portfolio offsets, SOMM,
   delta-based inter-month charges and any allowed cross-commodity credits.
3. Exchange-valued NOV and the correct premium payable/receivable settlement
   treatment, without double counting entry premium cash flows.
4. ELM, current additional/special/concentration rates, and expiry/devolvement
   schedules plus verified broker additions or an explicit unknown status.
5. Like-for-like hedged/unhedged comparison using the same timestamp, rules,
   isolated account assumptions and product; never arbitrary order summation.
6. Independent reconciliation of identical portfolios with official PC-SPAN or
   another officially supported engine and Kotak's calculator, including dated
   inputs, components, differences and explanation of any account effects.

The official NCL PC-SPAN workflow requires a CME account/download and desktop
software. It is not silently treated as a Linux-compatible cloud engine. No
software licence was accepted and no paid service was enabled in this work.

## NATURALGAS payoff is independently known; margin is not

SELL 300 PE at 24.05 and BUY 290 PE at 18.15, 1,250 units each:

- Premium credit: `(24.05 - 18.15) * 1250 = INR 7,375`.
- Gross maximum expiry profit: INR 7,375.
- Gross maximum expiry loss: `(300 - 290) * 1250 - 7375 = INR 5,125`.
- With total estimated charges INR 66.38: net profit cap INR 7,308.62;
  net maximum loss INR 5,191.38; breakeven `300 - (7375 - 66.38)/1250`
  = INR 294.153104 (approximately 294.15).

These are expiry payoff results, not SPAN margin. The historical INR 12,566.38
margin and INR 83,871.12 hedge benefit must not be marked verified. Genuine
exchange margin and like-for-like hedge benefit remain unavailable until the
above data and reference checks pass. The new status does not alter saved option
contracts, entry/exit prices, fills, P&L history or existing exports.

## Tests

`backend/tests/test_exchange_margin.py` uses synthetic, explicitly unverified
fixtures for safety and Decimal arithmetic. It checks source restrictions,
archive/size/XML validation, date mismatches, version/unit consistency, missing
exchange identity, partial/closed leg coverage and safe HTTP failure handling.
Passing these tests establishes the implementation boundary; it does not
establish live broker/exchange margin accuracy.
