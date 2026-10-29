"""History removal is owner-only, reversible and never closes an active trade."""
import asyncio
import time
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import main
from app.database import Base, configure_sqlite, get_db
from app.models import UserSession
from app.security import COOKIE, digest
import pytest

@pytest.fixture
def client(tmp_path,monkeypatch):
    sql=create_engine(f'sqlite:///{tmp_path / "history.db"}',connect_args={'check_same_thread':False})
    configure_sqlite(sql,journal_mode='DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False)
    with factory() as db:
        db.add(UserSession(token_hash=digest('owner-session'),csrf_token='csrf',expires_at=time.time()+600));db.commit()
    def override():
        with factory() as db:yield db
    main.app.dependency_overrides[get_db]=override
    monkeypatch.setattr(main,'database_ready',True)
    monkeypatch.setattr(main,'collector_lease',None)
    monkeypatch.setattr(main,'write_lock',asyncio.Lock())
    c=TestClient(main.app)
    yield c
    c.close();main.app.dependency_overrides.clear();sql.dispose()

def test_requires_owner_csrf_and_explicit_confirmation(client,monkeypatch):
    calls=[]
    monkeypatch.setattr(main.engine,'set_trade_history_hidden',lambda db,trade_id,hidden:calls.append((trade_id,hidden)) or {'id':trade_id,'history_hidden':hidden})
    url='/api/trades/closed-trade/history-visibility'
    assert client.post(url,json={'hidden':True,'confirmed':True}).status_code==401
    client.cookies.set(COOKIE,'owner-session')
    assert client.post(url,json={'hidden':True,'confirmed':True}).status_code==403
    headers={'x-csrf-token':'csrf'}
    assert client.post(url,headers=headers,json={'hidden':True}).status_code==422
    assert client.post(url,headers=headers,json={'hidden':'false','confirmed':True}).status_code==422
    assert calls==[]
    assert client.post(url,headers=headers,json={'hidden':True,'confirmed':True}).json()['history_hidden'] is True
    assert client.post(url,headers=headers,json={'hidden':False,'confirmed':True}).json()['history_hidden'] is False
    assert calls==[('closed-trade',True),('closed-trade',False)]
