"""Owner/CSRF boundaries and callback responsiveness, using no real broker."""
import asyncio
import secrets
import threading
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import main
from app.database import Base,configure_sqlite,get_db
from app.models import UserSession
from app.security import COOKIE,digest

@pytest.fixture
def client(tmp_path,monkeypatch):
    sql=create_engine(f'sqlite:///{tmp_path / "private.db"}',connect_args={'check_same_thread':False})
    configure_sqlite(sql,journal_mode='DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False)
    token=secrets.token_urlsafe(48)
    with factory() as db:
        db.add(UserSession(token_hash=digest(token),csrf_token='test-csrf',expires_at=time.time()+600));db.commit()
    def db_override():
        with factory() as db:yield db
    main.app.dependency_overrides[get_db]=db_override
    monkeypatch.setattr(main,'database_ready',True)
    monkeypatch.setattr(main,'write_lock',asyncio.Lock())
    # Do not enter the lifespan: these routes use only the isolated journal.
    c=TestClient(main.app)
    yield c,token
    c.close();main.app.dependency_overrides.clear();sql.dispose()


def test_broker_setup_requires_owner_and_csrf(client,monkeypatch):
    c,token=client;calls=[]
    monkeypatch.setattr(main,'kotak_setup',SimpleNamespace(status=lambda db:{'stored':False},save=lambda values:calls.append(values)))
    assert c.get('/api/brokers/kotak/setup').status_code==401
    assert c.post('/api/brokers/kotak/setup',json={}).status_code==401
    c.cookies.set(COOKIE,token)
    assert c.get('/api/brokers/kotak/setup').json()=={'stored':False}
    assert c.post('/api/brokers/kotak/setup',json={}).status_code==403
    assert not calls


def test_invalid_private_fields_are_never_echoed(client):
    c,token=client;c.cookies.set(COOKIE,token)
    private='private-rejected-input'
    r=c.post('/api/brokers/kotak/setup',headers={'x-csrf-token':'test-csrf'},json={'consumer_key':{'secret':private},'unexpected':private})
    assert r.status_code==422 and private not in r.text
    assert 'input' not in r.text


def test_authorization_redirect_is_owner_bound_and_no_key_in_begin(client,monkeypatch):
    c,token=client;c.cookies.set(COOKIE,token);seen=[]
    class Fake:
        def begin(self,session):seen.append(session);return 'state-value-that-is-long-enough'
        def authorization_url(self,state,session):
            assert state=='state-value-that-is-long-enough' and session==digest(token)
            return 'https://dev-openapi.5paisa.com/WebVendorLogin/VLogin/Index?VendorKey=private-key'
    monkeypatch.setattr(main,'fivepaisa_auth',Fake())
    r=c.post('/api/brokers/fivepaisa/auth/begin',headers={'x-csrf-token':'test-csrf'})
    assert r.status_code==200 and seen==[digest(token)]
    assert 'private-key' not in r.text
    r=c.get(r.json()['url'],follow_redirects=False)
    assert r.status_code==303 and r.headers['location'].startswith('https://dev-openapi.5paisa.com/')
    assert r.headers['referrer-policy']=='no-referrer'


def test_callback_sanitizes_broker_failure_and_accepts_state_case(client,monkeypatch):
    c,token=client;c.cookies.set(COOKIE,token);seen=[]
    def complete(request_token,state,session):
        seen.append((request_token,state,session));raise main.broker_auth.BrokerAuthError('Fixed failure')
    monkeypatch.setattr(main,'fivepaisa_auth',SimpleNamespace(complete=complete))
    r=c.get('/api/brokers/fivepaisa/callback',params={'RequestToken':'private-request','State':'state-value-that-is-long-enough'},follow_redirects=False)
    assert r.status_code==303 and r.headers['location']=='/?broker=fivepaisa_failed'
    assert seen==[('private-request','state-value-that-is-long-enough',digest(token))]
    assert 'private-request' not in r.text


def test_slow_broker_callback_does_not_hold_recorder_lock(monkeypatch):
    async def run():
        started=threading.Event();release=threading.Event()
        def complete(*args):started.set();assert release.wait(3)
        monkeypatch.setattr(main,'fivepaisa_auth',SimpleNamespace(complete=complete))
        monkeypatch.setattr(main,'write_lock',asyncio.Lock())
        request=SimpleNamespace(query_params={'RequestToken':'private','state':'state-value-that-is-long-enough'})
        task=asyncio.create_task(main.callback_fivepaisa(request,SimpleNamespace(token_hash='session'),SimpleNamespace(rollback=lambda:None)))
        try:
            for _ in range(100):
                if started.is_set():break
                await asyncio.sleep(.005)
            assert started.is_set() and not task.done()
            async with asyncio.timeout(.2):
                async with main.write_lock:assert main.process_health()['ok']
        finally:release.set();await asyncio.wait_for(task,3)
    asyncio.run(run())
