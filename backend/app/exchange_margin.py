"""Official risk-file intake and fail-closed exchange-margin provenance.

This is NOT a replacement implementation of CME SPAN. No complete exchange
margin is emitted until a supported model has been reconciled against an
independent official reference. Scenario scanning alone is not SPAN margin.
No broker account, funds, orders or credentials are accessed here.
"""
from __future__ import annotations

import hashlib
import io
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SOURCES = {
    "NSE": {
        "page": "https://www.nseindia.com/static/products-services/equity-derivatives-span-risk-parameter-files",
        "methodology": "https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span",
        "hosts": {"nsearchives.nseindia.com", "archives.nseindia.com", "www.nseindia.com", "www.nseclearing.in"},
    },
    "BSE": {
        "page": "https://www.icclindia.com/riskmgmt/RiskparameterNEW.aspx",
        "methodology": "https://www.icclindia.com/riskmgmt/RiskManagement.aspx",
        "hosts": {"www.icclindia.com", "icclindia.com", "www.bseindia.com"},
    },
    "MCX": {
        "page": "https://www.mcxccl.com/risk-management/daily-span-risk-parameter-file",
        "methodology": "https://www.mcxccl.com/risk-management/faq",
        "hosts": {"www.mcxccl.com", "mcxccl.com", "www.mcxindia.com"},
    },
}
MISSING_MODEL_COMPONENTS = (
    "validated risk-array contract mapping and INR scaling",
    "short-option minimum charge and spread rules",
    "net option valuation and premium settlement treatment",
    "ELM, additional, special and concentration margins",
    "expiry-day and MCX devolvement obligations",
    "official portfolio reference reconciliation",
)
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 32


def _instant(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Risk-file timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


def _number(value, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{name} must be finite.") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be finite.")
    return result


def validate_source_url(exchange: str, url: str) -> str:
    """Only public HTTPS downloads on exact exchange domains; no redirects."""
    if exchange not in SOURCES:
        raise ValueError("Unsupported exchange risk-file source.")
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid exchange download URL.") from None
    if (parsed.scheme != "https" or parsed.hostname not in SOURCES[exchange]["hosts"]
            or parsed.username or parsed.password or port not in (None, 443)
            or parsed.fragment or parsed.query or "\\" in parsed.path
            or not parsed.path.lower().endswith((".zip", ".spn", ".xml", ".pa2"))):
        raise ValueError("Use a direct public HTTPS risk-file URL from the selected exchange.")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Risk-file redirects require a separately validated source URL.")


def inspect_risk_file(data: bytes, *, exchange: str, source_url: str,
                      expected_date: date, downloaded_at: datetime | None = None) -> dict:
    """Validate transport/archive safety; never claim semantic SPAN validation.

    The date in a filename is only a label. It does not establish the internal
    point-in-time/version, effective date or completeness of the risk model.
    Public files remain quarantined for an independently reconciled parser.
    """
    source_url = validate_source_url(exchange, source_url)
    if not isinstance(expected_date, date) or isinstance(expected_date, datetime):
        raise ValueError("An explicit expected trading date is required.")
    now = _instant(downloaded_at)
    if expected_date > now.astimezone(IST).date():
        raise ValueError("Risk-file trading date cannot be in the future.")
    if not isinstance(data, bytes) or not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("Risk file is empty or exceeds the download limit.")
    if data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("The exchange returned an HTML page, not a risk file.")
    members = []
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("Unexpected risk archive member count.")
            if sum(info.file_size for info in infos) > MAX_EXPANDED_BYTES:
                raise ValueError("Expanded risk archive exceeds the memory limit.")
            names = set()
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                mode = info.external_attr >> 16
                if (name in names or "\\" in name or ":" in name or path.is_absolute()
                        or ".." in path.parts or stat.S_ISLNK(mode) or info.flag_bits & 1):
                    raise ValueError("Unsafe or duplicate risk archive member.")
                names.add(name)
                if info.is_dir():
                    continue
                if path.suffix.lower() not in {".spn", ".xml", ".pa2", ".txt", ".md5", ".sha256"}:
                    raise ValueError("Unrecognized risk archive member type.")
                with archive.open(info) as stream:
                    payload = stream.read(MAX_EXPANDED_BYTES + 1)
                if len(payload) > MAX_EXPANDED_BYTES or len(payload) != info.file_size:
                    raise ValueError("Invalid expanded risk file size.")
                members.append(_inspect_member(name, payload))
    else:
        members.append(_inspect_member(PurePosixPath(urllib.parse.urlsplit(source_url).path).name, data))
    embedded_dates = {day for member in members for day in member.get("embedded_point_in_time_dates", [])}
    if embedded_dates and embedded_dates != {expected_date.isoformat()}:
        raise ValueError("Embedded risk-file point-in-time date differs from the requested trading date.")
    if not members or not any(row["name"].lower().endswith((".spn", ".xml", ".pa2")) for row in members):
        raise ValueError("No recognized SPAN data member is present.")
    filename = urllib.parse.unquote(PurePosixPath(urllib.parse.urlsplit(source_url).path).name)
    compact = expected_date.strftime("%Y%m%d")
    date_labels = re.findall(r"(?<![0-9])(20[0-9]{6})(?![0-9])", filename)
    if date_labels and compact not in date_labels:
        raise ValueError("Risk-file filename differs from the requested trading date.")
    return {
        "exchange": exchange, "source_url": source_url, "source_page": SOURCES[exchange]["page"],
        "downloaded_at": now.isoformat(), "downloaded_at_ist": now.astimezone(IST).isoformat(),
        "expected_trading_date": expected_date.isoformat(), "file_version": filename,
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "members": members,
        "transport_validated": True, "model_validated": False, "effective_at": None,
        "risk_parameter_version_verified": False, "internal_date_matches_expected": bool(embedded_dates),
        "freshness": "unverified", "status": "quarantined",
        "reason": "Archive integrity inspected; contract units, effective timestamp and complete SPAN methodology are not validated.",
    }


def _inspect_member(name: str, payload: bytes) -> dict:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DTD/entity declarations are prohibited in risk XML.")
    is_xml = payload.lstrip().startswith(b"<")
    root_name = None
    stack = []
    embedded_dates = set()
    if is_xml:
        try:
            # Pull/clear rather than retaining a full 100MB exchange XML tree.
            for event, elem in ElementTree.iterparse(io.BytesIO(payload), events=("start", "end")):
                tag = elem.tag.rsplit("}", 1)[-1]
                if root_name is None:
                    root_name = tag
                if event == "start":
                    stack.append(tag.lower())
                else:
                    if (tag.lower() == "pointintime" or (tag.lower() == "date" and "pointintime" in stack)) and elem.text:
                        text = elem.text.strip()
                        if re.fullmatch(r"20[0-9]{6}", text):
                            embedded_dates.add(datetime.strptime(text, "%Y%m%d").date().isoformat())
                        elif re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", text):
                            embedded_dates.add(date.fromisoformat(text).isoformat())
                    stack.pop()
                    elem.clear()
        except (ElementTree.ParseError, ValueError):
            raise ValueError("Malformed risk XML.") from None
        if root_name and root_name.lower() in {"html", "error"}:
            raise ValueError("The exchange returned an error document.")
    return {"name": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "format": "XML" if is_xml else "unvalidated text", "root_element": root_name,
            "embedded_point_in_time_dates": sorted(embedded_dates)}


def download_risk_file(exchange: str, url: str, expected_date: date, *, now: datetime | None = None) -> dict:
    """One bounded read-only download. No recurring retries, file writes or secrets.

    Failures return a safe diagnostic; the caller owns retry/backoff and durable
    storage. The application does not infer a new version by guessing filenames.
    """
    url = validate_source_url(exchange, url)
    instant = _instant(now)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/zip, application/xml, application/octet-stream"})
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError("Unexpected risk-file HTTP status.")
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_DOWNLOAD_BYTES:
                raise ValueError("Risk-file response exceeds the download limit.")
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
        return inspect_risk_file(data, exchange=exchange, source_url=url,
                                 expected_date=expected_date, downloaded_at=instant)
    except urllib.error.HTTPError as exc:
        reason = f"Official exchange download returned HTTP {exc.code}."
    except (urllib.error.URLError, TimeoutError, OSError):
        reason = "Official exchange download could not be reached within its timeout."
    except (ValueError, zipfile.BadZipFile, RuntimeError):
        reason = "Official exchange risk file failed bounded archive or format validation."
    return {"exchange": exchange, "source_url": url, "source_page": SOURCES[exchange]["page"],
            "status": "unavailable", "model_validated": False, "freshness": "unverified",
            "downloaded_at": instant.isoformat(), "reason": reason}


def portfolio_scan_diagnostic(rows: Iterable[Mapping]) -> dict:
    """Decimal aggregation of 16 *already scaled* long-position loss arrays.

    Only a diagnostic scanning component. Inputs must all refer to the same
    exchange, combined commodity, official file version and INR-per-contract
    units. BUY quantity is positive; SELL is negative. No NOV/premium is applied
    here. This result MUST NOT be displayed as complete exchange/broker margin.
    """
    grouped = list(rows)
    if not grouped:
        raise ValueError("A scanning diagnostic needs at least one contract.")
    total = [Decimal(0)] * 16
    identity = None
    contracts = set()
    for row in grouped:
        key = tuple(row.get(field) for field in ("exchange", "combined_commodity", "file_version", "effective_at"))
        if any(not item for item in key) or key[0] not in SOURCES:
            raise ValueError("Risk arrays require exchange, commodity, version and timestamp.")
        if identity is not None and key != identity:
            raise ValueError("Risk scenarios from different groups or file versions cannot be offset.")
        identity = key
        try:
            stamp = datetime.fromisoformat(str(key[3]).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Risk-array effective timestamp must be ISO 8601.") from None
        _instant(stamp)
        if row.get("units") != "INR_per_contract":
            raise ValueError("Risk-array scaling must be explicitly established in INR per contract.")
        token = row.get("contract_id")
        if not isinstance(token, str) or not token or token in contracts:
            raise ValueError("Risk diagnostic requires unique exact contract identifiers.")
        contracts.add(token)
        quantity = _number(row.get("signed_contracts"), "Signed contract quantity")
        if quantity != quantity.to_integral_value() or quantity == 0:
            raise ValueError("Risk contract quantities must be nonzero integers.")
        array = row.get("risk_array")
        if not isinstance(array, (list, tuple)) or len(array) != 16:
            raise ValueError("A standard diagnostic needs exactly 16 aligned risk scenarios.")
        for index, value in enumerate(array):
            total[index] += quantity * _number(value, "Scenario loss")
    return {"status": "diagnostic_only", "verified": False, "currency": "INR",
            "scanning_risk": str(max(Decimal(0), *total)),
            "scenario_losses": [str(value) for value in total],
            "span_margin": None, "total_margin": None, "hedge_benefit": None,
            "reason": "Scanning component only; SOMM, spread, NOV and other margin components remain unvalidated."}


def exchange_margin_status(legs: Iterable[Mapping], *, risk_files: Mapping | None = None,
                           now: datetime | None = None) -> dict:
    """Public provenance, separate from broker margin and expiry payoff.

    Even a caller-supplied model_validated flag cannot turn a quarantined file
    into a verified calculator: no production SPAN adapter is registered yet.
    """
    instant = _instant(now)
    exchanges = set()
    identity_missing = False
    for leg in legs:
        if leg.get("enabled", True) is False:
            continue
        lots = _number(leg.get("open_lots", leg.get("lots", 1)), "Remaining lots")
        if lots < 0 or lots != lots.to_integral_value():
            raise ValueError("Remaining lots must be nonnegative integers.")
        if lots:
            exchange = str(leg.get("exchange", "")).upper()
            if exchange not in SOURCES:
                identity_missing = True
            else:
                exchanges.add(exchange)
    data = []
    for exchange in sorted(exchanges):
        item = (risk_files or {}).get(exchange) or {}
        data.append({"exchange": exchange, "source_url": SOURCES[exchange]["page"],
                     "methodology_url": SOURCES[exchange]["methodology"],
                     "risk_parameter_version": item.get("file_version"),
                     "risk_parameter_sha256": item.get("sha256"),
                     "freshness": "unverified", "model_validated": False,
                     "reason": item.get("reason") or "No validated applicable official risk file and portfolio model are loaded."})
    return {"status": "unavailable", "label": "Margin unavailable", "source": "exchange SPAN calculation unavailable",
            "verified": False, "currency": "INR", "as_of": instant.isoformat(),
            "as_of_ist": instant.astimezone(IST).isoformat(), "span_margin": None, "elm": None,
            "additional_margin": None, "total_exchange_margin": None, "total_margin": None, "hedge_benefit": None,
            "final_capital_required": None, "risk_parameter_version": None, "parameter_version": None, "freshness": "unverified",
            "sources": data, "missing_components": list(MISSING_MODEL_COMPONENTS),
            "reason": ("Exact exchange contract identity is missing or unsupported. " if identity_missing else "") + "Official risk files and the complete portfolio model require independent validation; no expiry-loss or percentage substitute is used."}


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Inspect one official risk file; does not calculate or enable complete margin.")
    parser.add_argument("--exchange", required=True, choices=sorted(SOURCES))
    parser.add_argument("--url", required=True, help="Direct public HTTPS file link copied from the official exchange page")
    parser.add_argument("--trading-date", required=True, type=date.fromisoformat)
    arguments = parser.parse_args()
    result = download_risk_file(arguments.exchange, arguments.url, arguments.trading_date)
    print(json.dumps(result, indent=2, ensure_ascii=False))
