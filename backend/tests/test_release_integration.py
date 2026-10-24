"""Behavioral release guards; no saved workspace or remote APIs used."""
from datetime import datetime,timezone
import pytest
from sqlalchemy import create_engine
from app.database import configure_sqlite
from app.payoff import strategy_risk
from app.scheduler import ExchangeCalendar
from app.storage import mount_details

def future(side='BUY',expiry='2026-11-24'):
    return dict(underlying='TEST',expiry=expiry,side=side,option_type='FUT',strike=0,lot_size=50,lots=1,entry_price=100)

def test_futures_are_linear_and_have_margin_without_option_premium():
    long=strategy_risk([future()],100);short=strategy_risk([future('SELL')],100)
    assert long['max_loss']==5000 and long['profit_unlimited'] and long['net_premium']==0
    assert short['max_profit']==5000 and short['loss_unlimited']
    assert long['breakevens']==[100] and long['estimated_margin']==1000

def test_future_with_short_call_has_bounded_upside_and_actual_multiplier():
    call={**future('SELL'),'option_type':'CE','strike':110,'entry_price':5}
    risk=strategy_risk([future(),call],100)
    assert risk['max_profit']==750 and risk['max_loss']==4750
    assert risk['net_premium']==250 and risk['breakevens']==[95]
    assert strategy_risk([{**future(),'price_multiplier':10}],100)['max_loss']==50000

def test_calendar_energy_season_and_partial_holidays():
    cal=ExchangeCalendar();leg={'exchange':'MCX','underlying':'NATURALGAS','expiry':'2026-12-28'}
    assert cal.state(leg,datetime(2026,10,26,18,5,tzinfo=timezone.utc))['open'] is False
    assert cal.state(leg,datetime(2026,11,2,18,5,tzinfo=timezone.utc))['open'] is True
    assert cal.state(leg,datetime(2026,11,10,5,tzinfo=timezone.utc))['open'] is False
    assert cal.state(leg,datetime(2026,11,10,13,tzinfo=timezone.utc))['open'] is True
    assert cal.state(leg,datetime(2026,11,8,10,tzinfo=timezone.utc))['open'] is False

def test_network_mount_is_not_treated_as_local(tmp_path):
    info=tmp_path/'mountinfo';info.write_text('1 0 0:1 / / rw - ext4 /dev/x rw\n2 1 0:2 / /app/data rw - nfs server:/data rw\n')
    assert mount_details('/app/data/trading.db',info)['local'] is False

def test_delete_journal_transactions_and_rollback_remain_atomic(tmp_path):
    sql=create_engine(f'sqlite:///{tmp_path / "rollback.db"}');configure_sqlite(sql,journal_mode='DELETE')
    with sql.begin() as c:
        assert c.exec_driver_sql('PRAGMA journal_mode').scalar()=='delete'
        c.exec_driver_sql('CREATE TABLE t(n INTEGER)');c.exec_driver_sql('INSERT INTO t VALUES (1)')
    with sql.connect() as c:
        c.exec_driver_sql('UPDATE t SET n=2');c.rollback()
        assert c.exec_driver_sql('SELECT n FROM t').scalar()==1
    sql.dispose()


def test_host_gateway_runs_workspace_lifespan_http_and_websocket(monkeypatch):
    import importlib.util
    import sys
    import types
    from contextlib import asynccontextmanager
    from pathlib import Path
    from fastapi import FastAPI, WebSocket
    from fastapi.testclient import TestClient
    events=[]
    @asynccontextmanager
    async def lifespan(app):
        events.append('start')
        yield
        events.append('stop')
    workspace=FastAPI(lifespan=lifespan)
    @workspace.get('/health/live')
    def live(): return {'ready':events==['start']}
    @workspace.websocket('/ws')
    async def socket(ws: WebSocket):
        await ws.accept()
        await ws.send_json({'ready':True})
        await ws.close()
    runtime=types.ModuleType('runtime')
    runtime.prepare_environment=lambda root:events.append('guard')
    runtime.install_safe_logging=lambda:None
    monkeypatch.setitem(sys.modules,'runtime',runtime)
    monkeypatch.setitem(sys.modules,'app.main',types.SimpleNamespace(app=workspace))
    entry=Path(__file__).resolve().parents[2]/'deploy/ramnaym/main.py'
    spec=importlib.util.spec_from_file_location('host_gateway_release_test',entry)
    gateway=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    assert events==['guard']
    events.clear()
    with TestClient(gateway.app) as client:
        assert client.get('/health/live').json()=={'ready':True}
        with client.websocket_connect('/ws') as ws:
            assert ws.receive_json()=={'ready':True}
    assert events==['start','stop']
