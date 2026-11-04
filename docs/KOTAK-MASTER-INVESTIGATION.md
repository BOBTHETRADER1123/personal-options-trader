# Kotak Scrip Master investigation — 4 November 2026

This report records the original implementation at commit 1805992, the pinned official `kotakneoapi==3.0.9` wheel, and the current official 3.0.13 source/changelog read on 4 November 2026. It does not claim that a live authenticated master request, downloaded current CSV, or broker-feed connection has been tested. No credentials or real orders were used in this investigation.

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

## Expiry parsing: current source differs from the pinned SDK

The pinned 3.0.9 `services/scrip_search.py` adds 315,511,200 seconds to NSE `pExpiryDate`, while parsing BSE/MCX directly. **This is no longer the current official rule.** The official 3.0.13 changelog, dated 28 October 2026, explicitly states that its approximately ten-year adjustment is specific to `nse_com`, not `nse_fo`. The current official `scrip_search.py` converts `pExpiryDate` as direct epoch seconds for all supported `*_fo` segments. Its UTC date must not be shifted into the next day by an IST conversion.

The fresh public source fetch identified `__version__ = "3.0.13"`; SHA-256 of its `services/scrip_search.py` was `b3f033dc5c5024d7caa3ff3857231c1f58e1bcd09900edd9b518ef9cc81d1028`. The original app instead prefers `lExpiryDate` over `pExpiryDate` and gives NSE's former legacy `lExpiryDate` a segment-specific offset. Current official `pExpiryDate` therefore needs explicit precedence and direct UTC conversion. Historical imports that use `lExpiryDate` need their own documented compatibility rule; the old SDK's `pExpiryDate` conversion must not be blindly applied to today's data. The `transformed` versus `transformed-v1` directory name does not, by itself, establish an epoch conversion rule.

A read-only synthetic reproduction executed the original parser helpers without importing app settings or making a broker request:

| Synthetic native input | Original parser result | What this proves |
|---|---|---|
| NSE `pExpiryDate = 1478188800` | 2016-11-03 | Direct epoch result; only the legacy pinned 3.0.9 source adds ten years. This does not establish a current parser bug. |
| NSE `lExpiryDate = 1478188800` | 2026-11-03 | Original app's legacy-field conversion. |
| NSE `lExpiryDate = 1478188800.0` | ValueError: unrecognized expiry format | The original numeric test rejects an integral decimal representation. |

The decimal-epoch example is a deterministic parser failure on synthetic input. It has not been observed in the production CSV, so it must not be reported as the exact production root cause. No actual current CSV URL, column values, HTTP status or root exception has been captured in this source-only investigation. The owner must reproduce a contract refresh using the new safe stage/row diagnostics before any specific branch is described as the observed server cause.

Normalized ISO expiry strings must retain their supplied dates. Native epoch aliases require finite integral values and explicit schema-specific conversion, not a guessed ten-year correction chosen to make an expired date look current. Unexpected historical dates must fail current-contract eligibility while the previous validated catalog remains available for existing positions.

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

The pinned wheel documents the installed SDK; the fresh official source and changelog document the newer published conversion rules. Their difference is material and must remain explicit. Production HTTP/CSV/root-cause and authenticated feed verification remain separate from these source and synthetic-parser findings.
