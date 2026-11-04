"""Protected margin diagnostics, using isolated SQLite and an SDK fake only."""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.database import Base, configure_sqlite, get_db
from app.models import Event, UserSession, Trade
from app.kotak_margin import ReadOnlyKotakMargin
from app.security import COOKIE, digest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    sql = create_engine(f'sqlite:///{tmp_path / "margin-route.db"}', connect_args={"check_same_thread": False})
    configure_sqlite(sql, journal_mode="DELETE")
    Base.metadata.create_all(sql)
    factory = sessionmaker(sql, expire_on_commit=False)
    with factory() as db:
        db.add(UserSession(token_hash=digest("test-owner"), csrf_token="csrf", expires_at=time.time() + 600))
        db.commit()
    sessions = []
    def dependency():
        with factory() as db:
            sessions.append(db)
            yield db
    main.app.dependency_overrides[get_db] = dependency
    monkeypatch.setattr(main, "database_ready", True)
    monkeypatch.setattr(main, "write_lock", asyncio.Lock())
    monkeypatch.setattr(main, "rate_limit", lambda *args, **kwargs: None)
    row = {"id": "kotak:nse_fo:53782", "token": "53782", "broker": "kotak", "demo": False,
           "segment": "nse_fo", "lot_size": 65, "expiry": "2030-11-03", "symbol": "NIFTY30NOV22700CE"}
    lookup = []
    def find(identifier, broker):
        lookup.append((identifier, broker))
        return row if identifier == row["id"] and broker == "kotak" else None
    monkeypatch.setattr(main, "catalog", SimpleNamespace(find=find))
    calls = []
    class SDK:
        def margin_required(self, **inputs):
            calls.append(inputs)
            return {"data": {"stat": "Ok", "stCode": 200, "ordMrgn": "100.25", "reqdMrgn": "150.75",
                             "avlCash": "987654", "token": "PRIVATE-TOKEN", "account": "PRIVATE-ACCOUNT"}}
        def place_order(self, *args, **kwargs):
            raise AssertionError("No real orders")
        modify_order = cancel_order = positions = limits = place_order
    service = ReadOnlyKotakMargin(lambda: SDK(), now=lambda: datetime(2030, 10, 29, tzinfo=timezone.utc).timestamp(), cooldown_seconds=0)
    class Manager:
        async def margin_required(self, contract, **inputs):
            assert not main.write_lock.locked(), "Network check must not hold writer lock"
            assert not sessions[-1].in_transaction(), "Network check must not hold auth/database read transaction"
            return service.check(contract, **inputs)
    monkeypatch.setattr(main, "feed_manager", Manager())
    client = TestClient(main.app, raise_server_exceptions=False)
    yield client, factory, calls, lookup
    client.close()
    main.app.dependency_overrides.clear()
    sql.dispose()


def body(**changes):
    return {"instrument_id": "kotak:nse_fo:53782", "side": "SELL", "lots": 1, "price": 185.1, **changes}


def authorize(client):
    client.cookies.set(COOKIE, "test-owner")
    return {"x-csrf-token": "csrf"}


def test_requires_owner_csrf_and_current_catalog_identity(isolated):
    client, factory, calls, lookup = isolated
    path = "/api/brokers/kotak/margin-required"
    assert client.post(path, json=body()).status_code == 401
    headers = authorize(client)
    assert client.post(path, json=body()).status_code == 403
    assert client.post(path, headers=headers, json=body(instrument_id="forged:token")).status_code == 422
    assert client.post(path, headers=headers, json=body(lots=1.5)).status_code == 422
    assert client.post(path, headers=headers, json=body(broker="fivepaisa")).status_code == 422
    assert calls == []


def test_single_call_diagnostic_never_becomes_basket_margin_and_audits_safely(isolated):
    client, factory, calls, lookup = isolated
    response = client.post("/api/brokers/kotak/margin-required", headers=authorize(client), json=body())
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["scope"] == "single_order_real_account_diagnostic"
    assert value["paper_portfolio_verified"] is False and value["total_margin"] is None
    assert value["hedge_benefit"] is None and value["final_capital_required"] is None
    assert value["order_margin"] == 100.25 and value["required_margin"] == 150.75
    assert "PRIVATE" not in response.text and "987654" not in response.text
    assert len(calls) == 1 and calls[0]["instrument_token"] == "53782" and calls[0]["quantity"] == "65"
    assert lookup == [("kotak:nse_fo:53782", "kotak")]
    with factory() as db:
        events = list(db.scalars(select(Event)))
        assert len(events) == 1 and events[0].kind == "single_order_margin_diagnostic"
        assert events[0].data["response_values"] == {"ordMrgn": "100.25", "reqdMrgn": "150.75"}
        assert "PRIVATE" not in str(events[0].data)
        assert not list(db.scalars(select(Trade))), "Margin checks must not create trades"


def test_basket_preview_is_unavailable_without_scalar_requests(isolated):
    client, factory, calls, lookup = isolated
    legs = [{"underlying": "NIFTY", "exchange": "NSE", "expiry": "2030-11-03", "option_type": "CE",
             "side": "SELL", "strike": 22700, "lot_size": 65, "lots": 1, "entry_price": 185.1},
            {"underlying": "NIFTY", "exchange": "NSE", "expiry": "2030-11-03", "option_type": "CE",
             "side": "BUY", "strike": 22850, "lot_size": 65, "lots": 1, "entry_price": 111.35}]
    response = client.post("/api/margin/preview", headers=authorize(client), json={"legs": legs})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["verified"] is False and value["total_margin"] is None
    assert value["hedge_benefit"] is None and value["exchange_margin"]["total_margin"] is None
    assert calls == [] and lookup == []


def test_basket_preview_rejects_foreign_broker_instead_of_server_error(isolated):
    client, factory, calls, lookup = isolated
    leg = {"broker": "fivepaisa", "underlying": "NIFTY", "exchange": "NSE", "expiry": "2030-11-03", "option_type": "CE",
           "side": "SELL", "strike": 22700, "lot_size": 65, "lots": 1, "entry_price": 185.1}
    response = client.post("/api/margin/preview", headers=authorize(client), json={"legs": [leg]})
    assert response.status_code == 422, response.text
    assert calls == []
