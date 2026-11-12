# Kotak Scrip Master investigation — 4–5 November 2026

This report records the original implementation at commit 1805992, the pinned official `kotakneoapi==3.0.9` wheel, and the current official 3.0.13 source/changelog read on 4 November 2026. The investigation combines captured production master diagnostics with in-memory replays of official contract files. Broker WebSocket delivery requires separate owner verification.

## Confirmed origin of the displayed message

The original `backend/app/option_chain.py:250` catches every exception from master download, normalization or save and returns only `type(exc).__name__`. Its displayed `Official kotak master unavailable (ValueError); existing contracts retained.` loses the exception message, traceback, segment and row. This identifies an import/refresh failure, but does not prove that a daily broker login was rejected. The original handler did not log the underlying traceback, so it cannot be recovered from that historical message alone.

There are multiple possible ValueError sites: missing consumer key, a non-URL SDK response, oversized download, invalid native contract fields/expiry, no normalized contracts, or an oversized saved cache. The production triggering branch must be established from new stage-aware diagnostics and a reproduced refresh. A synthetic example is not proof of the production branch.

## Official pinned SDK request contract

Inspection of `neo_api_client/services/scrip_master.py`, `settings.py`, `utils/urls.py`, and `utils/neo_utility.py` in the existing official 3.0.9 wheel confirms:

- Method: `GET`.
- Default production endpoint: `https://mis.kotaksecurities.com/script-details/1.0/masterscrip/file-paths`, constructed by the SDK configuration rather than a hardcoded dated CSV location.
- SDK request headers: `Authorization` is the configured consumer key; `Content-Type` is `application/x-www-form-urlencoded`. Logging must record header names/presence only, never their values.
- Scrip Master needs the consumer key; a completed TOTP/MPIN session is not required.
- For HTTP 200 the service reads the JSON object's `data`, then its `filesPaths` list. Each chosen URL is downloaded separately. For a supplied exchange segment, the SDK returns the matching URL string. With no segment it returns the unwrapped metadata object.
- For non-200 responses the service returns parsed JSON rather than a URL. The public `NeoAPI.scrip_master()` wrapper also catches exceptions and substitutes a generic error object. The app's URL-type check then raises its own ValueError, hiding the original HTTP/JSON problem.

Therefore the diagnostic implementation should use the verified official SDK REST transport and configured Scrip Master endpoint, validate status/JSON structure explicitly, and retain a safe failure stage. It must not infer HTTP success from a truthy dictionary or print an error dictionary that might contain private values.

The application requests the officially supported segments `nse_fo`, `bse_fo`, and `mcx_fo`. Their current CSV URLs must come from the actual response's `filesPaths`, with exact segment/filename matching and HTTPS/official-host checks. Dated example URLs in documentation are examples, not current file locations.

## Expiry parsing confirmed from the 5 November 2026 files

The SDK pinned by this deployment is `kotakneoapi==3.0.9`. The newer 3.0.13 changelog describes direct epoch decoding for `nse_fo`, but the three official files returned on 5 November establish the schema that the app had to parse. The NSE file contained equal `pExpiryDate` and `lExpiryDate` fields encoded in the legacy NSE epoch. Commit `a0353ba` selected `pExpiryDate` and applied direct Unix decoding, which moved all 86,055 rows back ten years and failed validation with `no_current_derivatives`.

The corrected parser requires both fields to agree when both are present, uses the legacy NSE conversion for the paired schema, and verifies the decoded date against the explicit NSE `pScripRefKey` when provided. BSE and MCX use direct UTC dates. The parser preserves explicit normalized dates and fails closed on schema disagreement. It does not adjust dates based on their age.

| Captured segment | Verified derivative contracts | Result |
| --- | ---: | --- |
| NSE | 86,055 | Accepted with legacy decoding and reference cross-check |
| BSE | 39,575 | Accepted using direct UTC decoding |
| MCX | 16,402 | Accepted using direct UTC decoding, with 40 non-derivative rows ignored |

The exact files were replayed locally. A separate production attempt on 4 November returned HTTP 424 at `response_status`, before CSV download. It is handled as a temporary dependency failure with bounded retry. The replay verifies parsing, not a broker WebSocket connection.

## Verified SDK transport and CSV schema details

In pinned 3.0.9, `RESTClientObject.request(method, url, query_params=None, headers=None, body=None, timeout=None)` returns an `httpx.Response`. `raise_on_error` is an instance option, defaulting to `False`; it is not a per-request keyword. Consequently non-200 status and JSON structure must be checked explicitly. Timeout/connect/HTTP failures are wrapped as `ApiException(status=0)` with the original exception as its cause. The HTTP client follows redirects; its standard httpx construction uses environment proxy defaults unless a custom transport is provided. The SDK's own response/body logging must remain disabled because an arbitrary broker response or request trace may contain private values.

Current official Scrip Master source uses the same endpoint and header contract as the pinned version. Current official `search_scrip` documentation publishes native names including `pSymbol`, `pExchSeg`, `pInstType`, `pSymbolName`, `pTrdSymbol`, `pOptionType`, `lLotSize`, `lExpiryDate`, `pExpiryDate`, `lPrecision`, `dTickSize`, and `dStrikePrice;`. Published raw examples also contain trailing header whitespace, so trimming is required. These documentation examples are evidence of supported names, not proof that a current derivative download has those exact bytes. Official futures rows use `pOptionType = XX`, which the SDK maps to the friendly `FUT` alias.

The application must retain current official URLs from `data.filesPaths`, including their actual filename/version and date. It must not construct a dated URL from the documentation's example. A refresh needs all three requested derivative segments, validated before publication; a missing segment must not silently replace the catalog with a partial result.

## Validation and data-preservation requirements

Before replacing the usable catalog, validate the response status and `data.filesPaths` structure; current official HTTPS file locations; downloaded byte bounds/content type; UTF-8 BOM; nonempty/unique required header aliases; row shape; known segments; numeric instrument identifiers; option/futures types; lot size; strike/tick units; exact expiry conversion; duplicate identities; and coverage of each requested exchange segment. A failure must retain the previous catalog and show the exact safe stage, segment and row number where available.

Keep only appropriate current-contract rows eligible for new exposures, and retain referenced historical/open-position identities as history-only. Existing trades, recorded fills and one-second P&L history must not be rewritten by a master refresh. Temporary HTTP 429/5xx or transport failures may retry with bounded exponential backoff; malformed schemas or invalid credentials need an explicit error, not endless retries. The last successful validated refresh must remain visible even when a later attempt fails.

Traceback diagnostics must preserve every exception-chain frame (file, line, function and exception type) without capturing locals, credentials, Authorization values, cookies or raw response bodies. Any omitted/redacted exception text should be identified honestly. An unredacted full SDK exception is unsafe because it can include request or response details.

## Official sources

- [Current official Scrip Master documentation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/scrip_master.md)
- [Official Scrip Master service](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/services/scrip_master.py)
- [Official search/expiry conversion](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/services/scrip_search.py)
- [Official SDK wrapper](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/neo_api.py)
- [Official production URL definitions](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/utils/urls.py)
- [Official 3.0.13 changelog](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/CHANGELOG.md)
- [Official native-field examples](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/search_scrip.md)

The pinned wheel documents the installed SDK; the fresh official source and changelog document the newer published conversion rules. Their difference is material and must remain explicit. Production catalog publication and authenticated feed verification are separate checks.


## Contract status timeout reported on 11 November 2026

The browser's status request times out after 10 seconds. The previous refresh POST waited for the entire download, parsing, cache validation and publication before returning. The browser separately waited up to 90 seconds for that POST. Status also called live_universe, potentially constructing every option-chain group solely to answer a metadata question.

The repair admits one server-owned refresh job and returns HTTP 202 immediately. Its own database session is independent of the browser request. Later requests join the running job. The browser polls status and retries temporary status failures with bounded backoff, without repeating an uncertain POST. Status uses cached catalog counts instead of constructing the full option-chain universe. Safe job stages and the completion timestamp are exposed. The CSV date label explicitly refers to the last verified catalog. Existing source-date validation, atomic publication and paper-trade identity checks remain in force.

Public health checks returned HTTP 200 for liveness and database readiness, and an unauthenticated status request returned HTTP 401. Private production logs and the current authenticated Scrip Master response could not be read because the computer-control connection was unavailable. Therefore neither the exact production download failure nor the cause of the provider's 137.66% CPU sample is established. The 9 November dates shown by the owner identify the saved catalog and cannot establish which files a new request returned. The supplied provider screenshot reports a range average of 4.25% and a peak of 137.66%.

Regression checks cover immediate admission while a mocked download is blocked, duplicate requests joining one job, metadata-only status, owner and CSRF checks, browser polling, temporary status failures and an uncertain POST. Full-suite validation also corrects a pre-existing test expectation from HTTP 200 to HTTP 201, matching the trade-creation response deliberately introduced in commit 29439b3. No broker login or live WebSocket acceptance test is part of these checks.


## Production deadline confirmed after browser access returned

RamNaym's error panel recorded the 11 November 2026 failure at 16:33:20 UTC as refresh_deadline_exceeded, stage csv_download, segment mcx_fo. The exception frames lead from option_chain.refresh_master through download_master to kotak_master.download and retry. The same failure recurred at 16:55 and 16:56 UTC. This establishes the immediate failure rather than inferring it from the retained 9 November CSV dates. The single 80-second budget covered all three downloads and the preceding CSV parsing, leaving MCX without sufficient time. It does not establish why a particular CPU sample exceeded the plan limit.

The derivative refresh now has a bounded 240-second aggregate budget, while the smaller, separate stock refresh retains 80 seconds. Per-request timeouts, retry count, byte bounds, official-host checks, current-date requirements and all-segment atomic validation remain unchanged. A deadline reached during parsing is reported at csv_validation before publication. Browser status observation allows six minutes for download, validation and publication, with no second POST. Fake-clock regressions exercise a successful 135-second three-segment run and an over-budget validation failure. These checks are not authenticated live-feed tests.
