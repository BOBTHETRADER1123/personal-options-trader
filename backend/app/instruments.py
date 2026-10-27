"""Current broker contract masters, plus a deliberately synthetic practice catalog.

No real expiry, strike, lot size or token is guessed. Import broker CSV masters
before selecting Live quotes. Demo IDs never resolve to a live instrument.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

BROKERS = ("kotak", "fivepaisa")
LEGACY_BROKERS = ("dhan",)
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "instruments.json"
FIVEPAISA_MASTER_URL = "https://Openapi.5paisa.com/VendorsAPI/Service1.svc/ScripMaster/segment/all"


def _date(value: object) -> str:
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "-1", "0"}:
        raise ValueError("Missing expiry")
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y", "%d-%b-%y", "%Y%m%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date().isoformat()
        except ValueError:
            pass
    dotnet = re.fullmatch(r"/?Date\((-?\d+)(?:[+-]\d+)?\)/?", text)
    if dotnet:
        return datetime.fromtimestamp(int(dotnet[1]) / 1000, IST).date().isoformat()
    # 5paisa currently uses integer YYYYMMDD or epoch milliseconds, depending on dump.
    if re.fullmatch(r"\d{10,13}", text):
        number = int(text)
        return datetime.fromtimestamp(number / (1000 if len(text) > 10 else 1), IST).date().isoformat()
    raise ValueError(f"Unrecognized expiry format: {text!r}; use an ISO expiry in a normalized CSV")


def _positive(value: object, integer: bool = False) -> float | int:
    result = float(str(value).strip())
    if not math.isfinite(result) or result <= 0 or (integer and not result.is_integer()):
        raise ValueError("Contract lot size/strike must be positive")
    return int(result) if integer else result


def _pick(row: dict, *keys: str, default=""):
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _underlying(text: str) -> str:
    text = re.sub(r"\s+", "", text.upper())
    return {"NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY", "BANKNIFTY50": "BANKNIFTY", "S&PBSESENSEX": "SENSEX"}.get(text, text)


def _kotak_native_price(row, native_name, normalized_name, default=""):
    """Current transformed derivative master prices are integer paise.

    The official SDK search_scrip compares dStrikePrice with human strike*100.
    Current native masters identify this schema with lPrecision=2; this is a
    supported schema check, not a general claim that precision is a divider.
    Explicit normalized fields stay in human price units. Older normalized
    inputs without native precision retain their documented human-unit form.
    """
    normalized = _pick(row, normalized_name)
    if normalized:
        return normalized
    raw = _pick(row, native_name)
    if not raw:
        return default
    precision = _pick(row, "lPrecision")
    if precision:
        try:
            if Decimal(precision) != 2:
                raise ValueError("Unsupported Kotak native price precision")
            value = Decimal(raw) / 100
            if not value.is_finite():
                raise ValueError("Invalid Kotak native price")
            return str(value)
        except InvalidOperation:
            raise ValueError("Invalid Kotak native price or precision") from None
    return raw


def normalize_row(broker: str, row: dict, exchange_segment: str | None = None) -> dict:
    """Parse documented native CSV columns or the explicit normalized template.

    Kotak changed its CSV schema; transformed-v1 fields are supported. A schema
    change fails validation rather than inventing missing tokens or lot sizes.
    """
    if broker not in BROKERS:
        raise ValueError("Unknown broker")
    row = {str(key).strip().rstrip(";"): value for key, value in row.items()}
    if broker == "fivepaisa":
        token = _pick(row, "ScripCode", "token")
        exchange = {"N": "NSE", "B": "BSE", "M": "MCX"}.get(_pick(row, "Exch"), _pick(row, "exchange"))
        segment = _pick(row, "ExchType", "segment", default="D")
        symbol = _pick(row, "Name", "ScripData", "symbol")
        underlying = _pick(row, "SymbolRoot", "underlying")
        expiry = _pick(row, "Expiry", "expiry")
        strike = _pick(row, "StrikeRate", "strike")
        option_type = _pick(row, "ScripType", "option_type")
        lot = _pick(row, "LotSize", "lot_size")
        tick = _pick(row, "TickSize", "tick_size", default="0.05")
    else:
        # Documented transformed-v1 names. Native files must include a segment
        # column or the operator must supply --segment, never infer from token.
        token = _pick(row, "pSymbol", "instrument_token", "token")
        segment = _pick(row, "pExchSeg", "exchange_segment", "segment", default=exchange_segment or "")
        exchange = {"nse_fo": "NSE", "bse_fo": "BSE", "mcx_fo": "MCX"}.get(segment.lower(), _pick(row, "exchange"))
        symbol = _pick(row, "pTrdSymbol", "trading_symbol", "symbol")
        underlying = _pick(row, "pSymbolName", "pUnderlying", "underlying")
        expiry = _pick(row, "expiry", "lExpiryDate", "pExpiryDate")
        if "lExpiryDate" in row and "expiry" not in row and str(expiry).isdigit():
            # Official instrument docs specify the NSE/CDE epoch offset. The
            # SDK formats direct BSE/MCX epochs as UTC business dates; their
            # end-of-day timestamps must not roll into tomorrow in IST.
            shifted = segment in {"nse_fo", "cde_fo"}
            seconds = int(expiry) + (315511200 if shifted else 0)
            expiry = datetime.fromtimestamp(seconds, IST if shifted else timezone.utc).date().isoformat()
        strike = _kotak_native_price(row, "dStrikePrice", "strike")
        option_type = _pick(row, "pOptionType", "option_type")
        lot = _pick(row, "lLotSize", "lot_size")
        tick = _kotak_native_price(row, "dTickSize", "tick_size", default="0.05")
    option_type = option_type.upper()
    # Both official masters use XX for futures in derivative segments. An
    # explicit FUT in the normalized template is supported as well; cash XX
    # rows cannot pass the derivative-segment validation below.
    if option_type in {"XX", "FUT"}:
        option_type = "FUT"
    if option_type not in {"CE", "PE", "FUT"}:
        raise ValueError("Not an option contract")
    if not token or not symbol or not underlying or not segment or exchange not in {"NSE", "BSE", "MCX"}:
        raise ValueError("Missing token, symbol, underlying, exchange or segment")
    allowed_segments = ({"nse_fo", "bse_fo", "mcx_fo"} if broker == "kotak" else {"D"})
    if segment not in allowed_segments:
        raise ValueError("Unsupported derivative segment")
    if not str(token).isdigit():
        raise ValueError("Option token must be a numeric broker-master identifier")
    parsed_expiry = _date(expiry)
    parsed_strike = 0.0 if option_type == "FUT" else _positive(strike)
    parsed_lot = _positive(lot, True)
    # Kotak native paise fields were converted only for the supported schema
    # above. Normalized template prices and 5paisa prices are already human units.
    contract_id = f"{broker}:{exchange}:{segment}:{token}" if broker == "fivepaisa" else f"{broker}:{segment}:{token}"
    # A commodity's broker order quantity may differ from the monetary value
    # of one quoted price point. Only explicitly supplied master information
    # can authorize this conversion; no commodity-specific mapping is guessed.
    multiplier_text = _pick(row, "price_multiplier", "Multiplier", "lMultiplier", "MULTIPLIER")
    # Current Kotak commodity masters use -1/0 in lMultiplier when no monetary
    # conversion is supplied. Retain the contract with an unverified multiplier;
    # the paper engine already blocks MCX fills until it can price them safely.
    # Explicit normalized overrides and other broker multipliers remain strict.
    native_unknown = (broker == "kotak" and exchange == "MCX"
                      and not _pick(row, "price_multiplier", "Multiplier", "MULTIPLIER")
                      and str(_pick(row, "lMultiplier")).strip() in {"-1", "-1.0", "0", "0.0"})
    price_multiplier = (None if native_unknown else _positive(multiplier_text) if multiplier_text else None) if exchange == "MCX" else 1.0
    return {"id": contract_id, "instrument_id": contract_id, "broker": broker,
            "token": str(token), "symbol": symbol, "underlying": _underlying(underlying),
            "exchange": exchange, "segment": segment, "expiry": parsed_expiry,
            "strike": parsed_strike, "option_type": option_type, "instrument_type": "future" if option_type == "FUT" else "option", "lot_size": parsed_lot,
            "tick_size": _positive(tick), "price_multiplier": price_multiplier,
            "multiplier_verified": price_multiplier is not None,
            "demo": False, "source": "broker_master",
            "imported_at": datetime.now(timezone.utc).isoformat()}


def demo_contracts(today: date | None = None) -> list[dict]:
    """Synthetic sample contracts: the values below are NOT current market data."""
    today = today or datetime.now(IST).date()
    expiries = [(today + timedelta(days=7)).isoformat(), (today + timedelta(days=14)).isoformat()]
    # Deliberately fictional practice quantities/prices, not live master rules.
    specs = [("NIFTY", "NSE", 24800, 50, 50), ("BANKNIFTY", "NSE", 56000, 100, 25),
             ("SENSEX", "BSE", 81000, 100, 10), ("GOLD", "MCX", 75000, 500, 100),
             ("SILVER", "MCX", 90000, 1000, 30), ("CRUDEOIL", "MCX", 6500, 50, 100),
             ("NATURALGAS", "MCX", 250, 5, 1250)]
    results = []
    for underlying, exchange, spot, step, lot in specs:
        for expiry in expiries:
            for offset in range(-12, 13):
                strike = spot + offset * step
                for option_type in ("CE", "PE"):
                    identifier = f"DEMO:{underlying}:{expiry}:{strike}:{option_type}"
                    intrinsic = max(0, (spot - strike) * (1 if option_type == "CE" else -1))
                    price = round(intrinsic + max(step * 0.2, step * 2.8 - abs(offset) * step * 0.18), 2)
                    for broker in BROKERS:
                        segment = ({"NSE": "nse_fo", "BSE": "bse_fo", "MCX": "mcx_fo"}[exchange]
                                   if broker == "kotak" else "D")
                        results.append({"id": identifier, "instrument_id": identifier, "broker": broker,
                                        "token": "DEMO", "symbol": f"{underlying} {expiry} {strike} {option_type}",
                                        "underlying": underlying, "exchange": exchange, "segment": segment,
                                        "expiry": expiry, "strike": strike, "option_type": option_type,
                                        "lot_size": lot, "tick_size": 0.05, "demo": True,
                                        "source": "synthetic_demo", "spot": spot, "base_price": price,
                                        "price_multiplier": 1.0, "multiplier_verified": True,
                                        "ltp": price})
    for underlying, exchange, spot, step, lot in specs:
        for expiry in expiries:
            for broker in BROKERS:
                identifier = f"DEMO:{underlying}:{expiry}:0:FUT"
                segment = {"NSE": "nse_fo", "BSE": "bse_fo", "MCX": "mcx_fo"}[exchange] if broker == "kotak" else "D"
                results.append({"id": identifier, "instrument_id": identifier, "broker": broker, "token": "DEMO",
                                "symbol": f"{underlying} {expiry} FUT", "underlying": underlying, "exchange": exchange,
                                "segment": segment, "expiry": expiry, "strike": 0.0, "option_type": "FUT", "instrument_type": "future",
                                "lot_size": lot, "tick_size": .05, "demo": True, "source": "synthetic_demo", "spot": spot,
                                "base_price": float(spot), "price_multiplier": 1.0, "multiplier_verified": True, "ltp": float(spot)})
    return results


class ContractCatalog:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("INSTRUMENT_MASTER_PATH", DEFAULT_PATH))
        self.revision = 0
        self._live: list[dict] = []
        self.error: str | None = None
        self._demo = demo_contracts()
        self.reload()

    def reload(self):
        pending = []
        self.error = None
        if not self.path.exists():
            self._live = []
            self.revision += 1
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload.get("instruments", []) if isinstance(payload, dict) else payload
            for row in rows:
                if isinstance(row, dict) and row.get("broker") in LEGACY_BROKERS:
                    continue  # Saved history remains in SQL; legacy feeds cannot be reactivated.
                if not isinstance(row, dict) or row.get("demo") or row.get("broker") not in BROKERS:
                    raise ValueError("Invalid master record")
                for key in ("id", "token", "symbol", "underlying", "exchange", "segment", "expiry", "strike", "option_type", "lot_size"):
                    if key not in row:
                        raise ValueError(f"Master record missing {key}")
                _positive(row["lot_size"], True)
                if row["option_type"] != "FUT":
                    _positive(row["strike"])
                _date(row["expiry"])
                if row["option_type"] not in {"CE", "PE", "FUT"} or row["exchange"] not in {"NSE", "BSE", "MCX"}:
                    raise ValueError("Invalid option type or exchange")
                if str(row["id"]).startswith("DEMO:"):
                    raise ValueError("Live masters cannot contain demo identifiers")
                if row.get("price_multiplier") is not None:
                    _positive(row["price_multiplier"])
                pending.append(row)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.error = f"Instrument master not loaded: {type(exc).__name__}"

        else:
            self._live = pending
        finally:
            self.revision += 1

    def restore_demo_contracts(self, trades: list[dict]) -> int:
        """Retain unexpired sample contracts after the startup date changes.

        Only persisted, explicitly synthetic trade legs are accepted. This
        recovery path cannot populate the live master or turn a broker token
        into a practice quote. Generated contracts always retain precedence.
        """
        present = {(row["id"], row["broker"]) for row in self._demo}
        today = datetime.now(IST).date().isoformat()
        restored = 0
        for trade in trades:
            if not isinstance(trade, dict) or trade.get("demo") is not True or trade.get("broker") not in BROKERS:
                continue
            broker = trade["broker"]
            for leg in trade.get("legs", []):
                if not isinstance(leg, dict) or leg.get("demo") is not True:
                    continue
                identifier = leg.get("instrument_id", "")
                if not isinstance(identifier, str) or not identifier.startswith("DEMO:") or (identifier, broker) in present:
                    continue
                try:
                    prefix, underlying, expiry, id_strike, option_type = identifier.split(":")
                    if expiry != _date(leg["expiry"]) or expiry < today:
                        continue
                    if underlying != leg["underlying"] or option_type != leg["option_type"] or option_type not in {"CE", "PE", "FUT"}:
                        continue
                    strike = 0.0 if option_type == "FUT" and float(leg["strike"]) == 0 else _positive(leg["strike"])
                    if float(id_strike) != strike or leg.get("token", "DEMO") != "DEMO":
                        continue
                    lot_size = _positive(leg["lot_size"], True)
                    exchange = leg["exchange"]
                    segments = ({"NSE": "nse_fo", "BSE": "bse_fo", "MCX": "mcx_fo"} if broker == "kotak" else
                                {"NSE": "D", "BSE": "D", "MCX": "D"})
                    segment = segments[exchange]
                    if leg.get("segment", segment) != segment:
                        continue
                    base_price = _positive(leg.get("base_price") or leg.get("current_price") or leg.get("entry_price"))
                    current = _positive(leg.get("current_price") or base_price)
                    multiplier = _positive(leg.get("price_multiplier") or 1)
                    spot = trade.get("spot")
                    if spot is not None:
                        spot = _positive(spot)
                except (ValueError, TypeError, KeyError):
                    continue
                self._demo.append({"id": identifier, "instrument_id": identifier, "broker": broker,
                                   "token": "DEMO", "symbol": leg.get("symbol") or f"{underlying} {expiry} {strike:g} {option_type}",
                                   "underlying": underlying, "exchange": exchange, "segment": segment,
                                   "expiry": expiry, "strike": strike, "option_type": option_type,
                                   "lot_size": lot_size, "tick_size": 0.05, "demo": True,
                                   "source": "synthetic_demo", "spot": spot, "base_price": base_price,
                                   "price_multiplier": multiplier, "multiplier_verified": True,
                                   "ltp": current, "restored_from_trade": True})
                present.add((identifier, broker))
                restored += 1
        return restored

    def all(self, demo: bool = True) -> list[dict]:
        rows = self._demo if demo else self._live
        today = datetime.now(IST).date().isoformat()
        return [dict(row) for row in rows if row["expiry"] >= today]

    def contracts(self, demo: bool = True):
        return self.all(demo)

    def merge_live_contracts(self, contracts: list[dict]) -> int:
        """Install only selected master rows, never subscribe the full universe."""
        current = {(c['id'], c['broker']): c for c in self._live}
        for contract in contracts:
            if (contract.get('demo') is not False or contract.get('broker') not in BROKERS
                    or not str(contract.get('token', '')).isdigit()
                    or not str(contract.get('id', '')).startswith(contract['broker'] + ':')):
                raise ValueError('Only verified broker-master contracts may enter the live catalog.')
            current[(contract['id'], contract['broker'])] = dict(contract)
        self._live = list(current.values())
        self.revision += 1
        return len(contracts)

    def select_chain_contracts(self, contracts: list[dict], active_ids: set[str]) -> int:
        """Replace browsed chain rows while retaining active and imported contracts.

        Browsing every underlying must not accumulate the full exchange master
        in process memory or subscribe every possible option to market feeds.
        """
        previous = self._live
        try:
            self._live = [c for c in previous if not c.get('chain_key') or c['id'] in active_ids]
            return self.merge_live_contracts(contracts)
        except Exception:
            self._live = previous
            raise

    def restore_live_contracts(self, trades: list[dict]) -> int:
        """Active journal legs retain their original master identity on restart."""
        contracts = []
        required = {'id','broker','token','underlying','exchange','segment','symbol','expiry','strike','option_type','lot_size'}
        for trade in trades:
            if trade.get('broker') not in BROKERS or trade.get('demo') is not False or trade.get('status') not in {'open','pending'}:
                continue
            for leg in trade.get('legs', []):
                if leg.get('status') not in {'open','pending'} or leg.get('demo') is not False:
                    continue
                contract = dict(leg)
                contract['id'] = leg.get('instrument_id')
                if required.issubset(contract):
                    contracts.append(contract)
        return self.merge_live_contracts(contracts)

    def find(self, instrument_id: str, broker: str | None = None) -> dict | None:
        for row in self._demo + self._live:
            if row["id"] == instrument_id and (broker is None or row["broker"] == broker):
                return dict(row)
        return None

    def list_contracts(self, underlying=None, broker=None, demo=True):
        return [r for r in self.all(demo) if (not underlying or r["underlying"] == underlying) and (not broker or r["broker"] == broker)]


_catalog: ContractCatalog | None = None


def list_contracts(underlying=None, broker=None, demo=True):
    global _catalog
    if _catalog is None:
        _catalog = ContractCatalog()
    return _catalog.list_contracts(underlying, broker, demo)


def get_contract(instrument_id, broker=None):
    global _catalog
    if _catalog is None:
        _catalog = ContractCatalog()
    return _catalog.find(instrument_id, broker)


def import_master(broker: str, csv_text: str, destination: Path, segment: str | None = None) -> dict:
    if broker not in BROKERS:
        raise ValueError("Only Kotak Neo and 5paisa masters are supported")
    rows, rejected, non_options = [], [], 0
    for number, raw in enumerate(csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))), 2):
        try:
            rows.append(normalize_row(broker, raw, segment))
        except ValueError as exc:
            if str(exc) in {"Not an option contract", "Unsupported derivative segment"}:
                non_options += 1
            else:
                rejected.append({"line": number, "error": str(exc)})
    if not rows:
        raise ValueError("No valid options parsed. Check the CSV columns and --segment. Existing master retained.")
    if rejected:
        raise ValueError(f"{len(rejected)} option rows failed validation; existing master retained. First errors: {rejected[:5]}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {"instruments": []}
    old_rows = existing.get("instruments", []) if isinstance(existing, dict) else existing
    # Segment-specific imports replace their own scope only.
    replace_segments = {r["segment"] for r in rows}
    retained = [r for r in old_rows if r.get("broker") != broker or r.get("segment") not in replace_segments]
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "instruments": retained + rows}
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return {"imported": len(rows), "ignored_non_options": non_options, "path": str(destination)}


def market_session(exchange: str, at: datetime | None = None, commodity_category: str = "non_agri", *, underlying: str | None = None, segment: str | None = None) -> dict:
    """Session calendar from an operator-maintained exchange-calendar JSON.

    A weekday schedule alone is not claimed to be an exchange-open signal.
    MCX has split holidays and seasonal closes; an unconfigured calendar returns
    unknown, so the real feed timestamp remains the source of freshness truth.
    """
    local = (at or datetime.now(timezone.utc)).astimezone(IST)
    path = os.environ.get("MARKET_CALENDAR_PATH")
    result = {"exchange": exchange, "timezone": "Asia/Kolkata", "date": local.date().isoformat(),
              "status": "unknown", "source": "calendar_not_configured", "intervals": [],
              "note": "Configure exchange holiday/session calendar; feed freshness is measured separately."}
    if not path:
        from .scheduler import ExchangeCalendar
        moment = at or datetime.now(timezone.utc)
        contract = {"exchange": exchange, "segment": segment or {"NSE": "nse_fo", "BSE": "bse_fo", "MCX": "mcx_fo"}.get(exchange, ""),
                    "underlying": underlying or (commodity_category if commodity_category != "non_agri" else "")}
        calendar = ExchangeCalendar()
        state = calendar.state(contract, moment)
        return {**result, "status": "open" if state["known"] and state["open"] else "closed" if state["known"] else "unknown",
                "source": "verified_exchange_calendar" if state["known"] else "calendar_not_configured",
                "note": state["reason"], "sources": state.get("sources", []), "verified_at": state.get("verified_at")}
    try:
        calendar = json.loads(Path(path).read_text(encoding="utf-8"))
        key = f"{exchange}:{commodity_category}" if exchange == "MCX" else exchange
        by_exchange = calendar.get(key, calendar.get(exchange, {}))
        intervals = by_exchange.get(local.date().isoformat())
        if intervals is None:
            return result
        hhmm = local.strftime("%H:%M")
        opened = any(start <= hhmm < end for start, end in intervals)
        return {**result, "status": "open" if opened else "closed", "intervals": intervals,
                "source": "configured_exchange_calendar", "note": "Calendar estimate; broker/exchange feed can report a halt."}
    except (OSError, ValueError, TypeError):
        return {**result, "source": "calendar_error"}


def main():
    parser = argparse.ArgumentParser(description="Import fresh broker option masters; no trading APIs are called.")
    parser.add_argument("broker", choices=BROKERS)
    parser.add_argument("--csv", type=Path, help="Native broker CSV or normalized CSV")
    parser.add_argument("--download", action="store_true", help="Download current official Kotak/5paisa master")
    parser.add_argument("--segment", help="Kotak segment, e.g. nse_fo, bse_fo or mcx_fo")
    parser.add_argument("--output", type=Path, default=Path(os.environ.get("INSTRUMENT_MASTER_PATH", DEFAULT_PATH)))
    args = parser.parse_args()
    if args.csv:
        text = args.csv.read_text(encoding="utf-8-sig")
    elif args.download:
        import urllib.request
        if args.broker == "kotak":
            from neo_api_client import NeoAPI
            if not args.segment:
                parser.error("Kotak requires --segment nse_fo, bse_fo or mcx_fo")
            client = NeoAPI(consumer_key=os.environ["KOTAK_CONSUMER_KEY"], environment="prod")
            url = client.scrip_master(exchange_segment=args.segment)
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("Broker did not return an HTTPS CSV URL")
        else:
            url = FIVEPAISA_MASTER_URL
        with urllib.request.urlopen(url, timeout=90) as response:
            text = response.read(50_000_001).decode("utf-8-sig")
            if len(text) > 50_000_000:
                raise ValueError("Master exceeds 50 MB safety limit")
    else:
        parser.error("Specify --csv or --download")
    print(json.dumps(import_master(args.broker, text, args.output, args.segment), indent=2))


if __name__ == "__main__":
    main()
