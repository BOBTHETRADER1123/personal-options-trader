"""Validated, read-only official Kotak Scrip Master downloads.

No login, order, or portfolio API is called. SDK payload/credential logging is
kept disabled; diagnostics contain fixed codes and public file metadata only.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import httpx

from .instruments import IST, normalize_kotak_stock, normalize_row

log = logging.getLogger('personal_options_trader.master')
log.setLevel(logging.INFO)  # Only app-authored, credential-free diagnostics below.
SEGMENTS = ('nse_fo', 'bse_fo', 'mcx_fo')
STOCK_SEGMENT = 'nse_cm'
MAX_BYTES = 100 * 1024 * 1024
MAX_STOCK_BYTES = 12 * 1024 * 1024
DERIVATIVE_REFRESH_SECONDS = 240
STOCK_REFRESH_SECONDS = 80
# Official troubleshooting describes 424 as a temporary dependency failure.
# Bounded retries are safe here because this endpoint only reads the master.
RETRY_STATUSES = {408, 424, 429, 500, 502, 503, 504}
REQUIRED = {
    'token': {'pSymbol', 'instrument_token', 'token'},
    'symbol': {'pTrdSymbol', 'trading_symbol', 'symbol'},
    'underlying': {'pSymbolName', 'pUnderlying', 'underlying'},
    'expiry': {'expiry', 'pExpiryDate', 'lExpiryDate'},
    'option_type': {'pOptionType', 'option_type'},
    'lot_size': {'lLotSize', 'lot_size'},
    'strike': {'dStrikePrice', 'strike'},
    'segment': {'pExchSeg', 'exchange_segment', 'segment'},
    'tick_size': {'dTickSize', 'tick_size'},
}
STOCK_REQUIRED = {name: {name} for name in (
    'pSymbol', 'pGroup', 'pExchSeg', 'pExchange', 'pSegment', 'pSymbolName',
    'pTrdSymbol', 'pISIN', 'pDesc', 'lLotSize', 'lPrecision', 'dTickSize',
)}


class MasterRefreshError(ValueError):
    def __init__(self, code, stage, *, segment=None, row_number=None,
                 http_status=None, retryable=False, missing_columns=None,
                 endpoint=None, method=None, authorization_present=None,
                 content_type=None, response_shape=None):
        super().__init__(code)  # Codes are app constants, never broker text.
        self.details = {k: v for k, v in {
            'code': code, 'stage': stage, 'segment': segment,
            'row_number': row_number, 'http_status': http_status,
            'missing_columns': missing_columns, 'endpoint': endpoint, 'method': method,
            'authorization_present': authorization_present, 'content_type': content_type,
            'response_shape': response_shape,
        }.items() if v is not None}
        self.retryable = retryable


def response_diagnostics(response):
    """Only fixed MIME names and JSON-shape booleans; never body/error text."""
    mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    safe_mimes = {'application/json', 'application/problem+json', 'text/plain',
                  'text/html', 'text/csv', 'application/octet-stream'}
    content_type = mime if mime in safe_mimes else 'missing' if not mime else 'other'
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    is_object = isinstance(payload, dict)
    data = payload.get('data') if is_object else None
    data_object = isinstance(data, dict)
    return {'content_type': content_type, 'response_shape': {
        'json_object': is_object,
        'data_object': data_object,
        'filesPaths_list': data_object and isinstance(data.get('filesPaths'), list),
        'error_field': is_object and 'error' in payload,
        'fault_field': is_object and 'fault' in payload,
        'message_field': is_object and 'message' in payload,
    }}


class MasterBatch(list):
    def __init__(self, rows, metadata):
        super().__init__(rows)
        self.metadata = metadata


def public_url(url):
    """Reject credentials/query strings; only broker-owned HTTPS locations."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in {None, 443} or parsed.query or parsed.fragment
                or not parsed.hostname or not (parsed.hostname == 'kotaksecurities.com'
                    or parsed.hostname.endswith('.kotaksecurities.com'))):
            raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise MasterRefreshError('untrusted_official_url', 'file_paths') from None


def source_date(url):
    parsed = public_url(url)
    matches = re.findall(r'/(\d{4}-\d{2}-\d{2})/', parsed.path)
    if len(matches) != 1:
        raise MasterRefreshError('missing_file_date', 'file_paths')
    try:
        return date.fromisoformat(matches[0]).isoformat()
    except ValueError:
        raise MasterRefreshError('invalid_file_date', 'file_paths') from None


def resolve_paths(payload, today, segments=SEGMENTS):
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise MasterRefreshError('invalid_data_object', 'response_json')
    paths = payload['data'].get('filesPaths')
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise MasterRefreshError('invalid_filesPaths', 'file_paths')
    result = {}
    for segment in segments:
        matches = []
        for url in paths:
            # Derivative and cash refreshes request only their own segment files.
            basename = urlsplit(url).path.rsplit('/', 1)[-1].lower()
            if basename not in {segment + '.csv', segment + '-v1.csv'}:
                continue
            public_url(url)
            matches.append((source_date(url), url))
        if not matches:
            raise MasterRefreshError('segment_file_missing', 'file_paths', segment=segment)
        newest = max(d for d, _ in matches)
        chosen = sorted({url for d, url in matches if d == newest})
        if segment == STOCK_SEGMENT:
            versioned = [url for url in chosen if urlsplit(url).path.lower().endswith('/nse_cm-v1.csv')]
            if versioned:
                chosen = versioned
        if len(chosen) != 1:
            raise MasterRefreshError('ambiguous_segment_files', 'file_paths', segment=segment)
        if newest != today:
            raise MasterRefreshError('source_file_not_current', 'file_paths', segment=segment)
        result[segment] = chosen[0]
    return result


def _transient(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError)):
            return True
        error = error.__cause__ or error.__context__
    return False


def retry(operation, *, stage, segment=None, deadline, clock=time.monotonic, sleep=time.sleep):
    for attempt in range(3):
        if clock() >= deadline:
            raise MasterRefreshError('refresh_deadline_exceeded', stage, segment=segment)
        try:
            return operation()
        except Exception as exc:
            temporary = exc.retryable if isinstance(exc, MasterRefreshError) else _transient(exc)
            if not temporary or attempt == 2:
                if isinstance(exc, MasterRefreshError):
                    raise
                raise MasterRefreshError('transport_failure' if temporary else 'request_failed',
                                         stage, segment=segment) from exc
            delay = 2 ** attempt
            if clock() + delay >= deadline:
                raise MasterRefreshError('refresh_deadline_exceeded', stage, segment=segment) from exc
            log.warning('Kotak master temporary failure stage=%s segment=%s attempt=%s retry_in_seconds=%s',
                        stage, segment, attempt + 1, delay)
            sleep(delay)


def parse_csv(raw, segment, *, today, stamp):
    is_stock = segment == STOCK_SEGMENT
    required = STOCK_REQUIRED if is_stock else REQUIRED
    if not raw or len(raw) > (MAX_STOCK_BYTES if is_stock else MAX_BYTES):
        raise MasterRefreshError('empty_or_oversized_csv', 'csv_download', segment=segment)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise MasterRefreshError('invalid_csv_encoding', 'csv_contents', segment=segment) from exc
    if text.lstrip().startswith(('<', '{', '[')):
        raise MasterRefreshError('non_csv_response', 'csv_contents', segment=segment)
    reader = csv.DictReader(io.StringIO(text), strict=True)
    try:
        headers = reader.fieldnames or []
    except csv.Error as exc:
        raise MasterRefreshError('invalid_csv_header', 'csv_columns', segment=segment) from exc
    normalized = [name.strip().rstrip(';') for name in headers]
    if not normalized or len(normalized) != len(set(normalized)):
        raise MasterRefreshError('empty_or_duplicate_headers', 'csv_columns', segment=segment)
    missing = sorted(name for name, aliases in required.items() if not set(normalized) & aliases)
    if missing:
        raise MasterRefreshError('required_columns_missing', 'csv_columns', segment=segment, missing_columns=missing)
    rows, seen, signatures, row_count, expired = [], set(), set(), 0, 0
    try:
        for row in reader:
            row_count += 1
            row_number = reader.line_num
            if None in row or any(value is None for value in row.values()):
                raise MasterRefreshError('invalid_column_count', 'csv_row', segment=segment, row_number=row_number)
            row = {key.strip().rstrip(';'): value for key, value in row.items()}
            supplied = next((row[k].strip().lower() for k in ('pExchSeg','exchange_segment','segment') if row.get(k)), segment)
            if supplied != segment:
                raise MasterRefreshError('segment_mismatch', 'csv_row', segment=segment, row_number=row_number)
            if is_stock:
                try:
                    stock = normalize_kotak_stock(row)
                except (ValueError, TypeError, OverflowError) as exc:
                    raise MasterRefreshError('invalid_stock_fields', 'csv_row', segment=segment, row_number=row_number) from exc
                if stock is None:
                    continue
                if stock['token'] in seen or stock['isin'] in signatures:
                    raise MasterRefreshError('duplicate_stock_identity', 'csv_row', segment=segment, row_number=row_number)
                seen.add(stock['token'])
                signatures.add(stock['isin'])
                stock.update(master_active=True, master_refresh_at=stamp, master_source_date=today)
                rows.append(stock)
                continue
            if any(row.get(k) for k in ('dStrikePrice', 'dTickSize')) and not row.get('lPrecision'):
                raise MasterRefreshError('native_price_precision_missing', 'csv_row', segment=segment, row_number=row_number)
            kind = next((row[k].strip().upper() for k in ('pOptionType','option_type') if row.get(k)), '')
            if kind not in {'CE', 'PE', 'XX', 'FUT'}:
                continue  # Other published product types are not paper derivatives.
            try:
                contract = normalize_row('kotak', row, segment)
            except (ValueError, TypeError, OverflowError, OSError) as exc:
                raise MasterRefreshError('invalid_contract_fields', 'csv_row', segment=segment, row_number=row_number) from exc
            if contract['id'] in seen:
                raise MasterRefreshError('duplicate_contract_id', 'csv_row', segment=segment, row_number=row_number)
            seen.add(contract['id'])
            signature = tuple(contract[k] for k in ('exchange','underlying','expiry','strike','option_type'))
            if signature in signatures:
                raise MasterRefreshError('duplicate_contract_identity', 'csv_row', segment=segment, row_number=row_number)
            signatures.add(signature)
            if contract['expiry'] < today:
                expired += 1
                continue
            contract.update(master_active=True, master_refresh_at=stamp, master_source_date=today)
            rows.append(contract)
    except csv.Error as exc:
        raise MasterRefreshError('malformed_csv', 'csv_row', segment=segment, row_number=reader.line_num) from exc
    if not rows:
        raise MasterRefreshError('no_current_stocks' if is_stock else 'no_current_derivatives', 'csv_validation', segment=segment)
    return rows, {'row_count': row_count, 'active_count': len(rows), 'expired_count': expired,
                  'required_columns_verified': sorted(required), 'segment_verified': True,
                  'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def download(*, client=None, now=None, clock=time.monotonic, sleep=time.sleep, segments=SEGMENTS):
    owned_client = client is None
    moment = now or datetime.now(timezone.utc)
    today, stamp = moment.astimezone(IST).date().isoformat(), moment.isoformat()
    deadline = clock() + (STOCK_REFRESH_SECONDS if segments == (STOCK_SEGMENT,) else DERIVATIVE_REFRESH_SECONDS)
    max_bytes = MAX_STOCK_BYTES if segments == (STOCK_SEGMENT,) else MAX_BYTES
    if client is None:
        os.environ['NEO_LOG_LEVEL'] = 'NOLOG'
        os.environ['NEO_LOG_FILE_ENABLED'] = 'false'
        from neo_api_client import NeoAPI
        from neo_api_client.logger import setup_logging
        setup_logging(level='NOLOG', file_enabled=False)
        key = os.getenv('KOTAK_CONSUMER_KEY')
        if not key:
            raise MasterRefreshError('consumer_key_missing', 'configuration')
        client = NeoAPI(consumer_key=key, environment='prod', timeout=12)
    try:
        api = client.api_client
        endpoint = api.configuration.get_url_details('scrip_master')
        public_url(endpoint)
        headers = {'Authorization': api.configuration.consumer_key,
                   'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}

        def paths_request():
            response = api.rest_client.request(url=endpoint, method='GET', headers=headers,
                                               timeout=min(12, max(.1, deadline - clock())))
            status = response.status_code
            public_request = {'endpoint': endpoint, 'method': 'GET',
                              'authorization_present': bool(headers['Authorization'])}
            diagnostics = response_diagnostics(response)
            log.info('Kotak master request=%s http_status=%s response=%s', public_request, status, diagnostics)
            if status != 200:
                raise MasterRefreshError('scrip_master_http_error', 'response_status', http_status=status,
                                         retryable=status in RETRY_STATUSES, **public_request, **diagnostics)
            try:
                payload = response.json()
            except (ValueError, TypeError) as exc:
                raise MasterRefreshError('invalid_json', 'response_json', http_status=status) from exc
            return resolve_paths(payload, today, segments)

        paths = retry(paths_request, stage='scrip_master_request', deadline=deadline, clock=clock, sleep=sleep)
        for segment, url in paths.items():
            log.info('Kotak master resolved segment=%s source_date=%s public_csv_url=%s', segment, source_date(url), url)
        result, metadata, total = [], {}, 0
        # CSV requests deliberately have NO broker Authorization header.
        with httpx.Client(follow_redirects=False, timeout=12) as transport:
            for segment, url in paths.items():
                def fetch():
                    current = url
                    for _ in range(5):
                        public_url(current)
                        with transport.stream('GET', current, timeout=min(12, max(.1, deadline-clock()))) as response:
                            if response.status_code in {301,302,303,307,308}:
                                from urllib.parse import urljoin
                                location = response.headers.get('location')
                                if not location:
                                    raise MasterRefreshError('redirect_location_missing', 'csv_download', segment=segment)
                                redirected = urljoin(current, location)
                                target = public_url(redirected)
                                expected = public_url(url)
                                if (source_date(redirected) != source_date(url)
                                        or target.path.rsplit('/', 1)[-1] != expected.path.rsplit('/', 1)[-1]):
                                    raise MasterRefreshError('redirect_source_changed', 'csv_download', segment=segment)
                                current = redirected
                                continue
                            if response.status_code != 200:
                                raise MasterRefreshError('csv_http_error', 'csv_download', segment=segment,
                                    http_status=response.status_code, retryable=response.status_code in RETRY_STATUSES)
                            content_type = response.headers.get('content-type', '').split(';',1)[0].strip().lower()
                            if content_type not in {'text/csv','application/csv','text/plain','application/octet-stream','binary/octet-stream','application/vnd.ms-excel'}:
                                raise MasterRefreshError('unexpected_csv_content_type', 'csv_contents', segment=segment)
                            chunks, size = [], 0
                            for chunk in response.iter_bytes():
                                size += len(chunk)
                                if size + total > max_bytes:
                                    raise MasterRefreshError('csv_size_limit', 'csv_download', segment=segment)
                                if clock() >= deadline:
                                    raise MasterRefreshError('refresh_deadline_exceeded', 'csv_download', segment=segment)
                                chunks.append(chunk)
                            return b''.join(chunks)
                    raise MasterRefreshError('too_many_redirects', 'csv_download', segment=segment)
                raw = retry(fetch, stage='csv_download', segment=segment, deadline=deadline, clock=clock, sleep=sleep)
                total += len(raw)
                rows, details = parse_csv(raw, segment, today=today, stamp=stamp)
                if clock() >= deadline:
                    raise MasterRefreshError('refresh_deadline_exceeded', 'csv_validation', segment=segment)
                result.extend(rows)
                details.update(source_url=url, source_date=source_date(url))
                metadata[segment] = details
                log.info('Kotak master validated segment=%s source_date=%s http_status=200 bytes=%s rows=%s active=%s sha256=%s',
                         segment, today, len(raw), details['row_count'], len(rows), details['sha256'])
        return MasterBatch(result, {'last_successful_refresh_at': stamp, 'segments': metadata})
    finally:
        if owned_client:
            try:
                client.api_client.rest_client.close()
            except Exception:
                log.warning('Kotak master transport close failed; credential details omitted')


def download_stock_master(*, client=None, now=None, clock=time.monotonic, sleep=time.sleep):
    """Download only the official NSE cash master via the read-only endpoint."""
    return download(client=client, now=now, clock=clock, sleep=sleep, segments=(STOCK_SEGMENT,))
