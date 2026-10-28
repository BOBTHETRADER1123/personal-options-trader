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


@pytest.mark.parametrize('method,path',[
    ('GET','/api/brokers/fivepaisa/auth/status'),
    ('POST','/api/brokers/fivepaisa/auth/begin'),
    ('GET','/api/brokers/fivepaisa/authorize'),
    ('GET','/api/brokers/fivepaisa/callback'),
])
def test_retired_authorization_routes_are_absent(client,method,path):
    c,token=client;c.cookies.set(COOKIE,token)
    response=c.request(method,path,headers={'x-csrf-token':'test-csrf'},follow_redirects=False)
    assert response.status_code in (404,405) and 'location' not in response.headers
    assert response.headers['referrer-policy']=='no-referrer'


def test_retired_callback_cannot_restart_or_restore_authorization():
    assert not hasattr(main,'fivepaisa_auth')
    assert not hasattr(main,'callback_fivepaisa')
    assert not any(getattr(route,'path','').startswith('/api/brokers/fivepaisa') for route in main.app.routes)
