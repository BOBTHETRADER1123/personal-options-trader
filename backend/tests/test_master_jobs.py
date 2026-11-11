"""Owner-authenticated job tests use a temporary DELETE journal and fake downloads."""
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from app import engine, main, option_chain as chain
from app.database import get_db
from app.instruments import ContractCatalog, IST
from app.kotak_master import MasterBatch, SEGMENTS
from app.models import UserSession
from app.security import COOKIE, digest
from test_chain_delete_journal import journal


@pytest.fixture
def jobs(journal, tmp_path, monkeypatch):
    catalog = ContractCatalog(tmp_path / 'master.json')
    monkeypatch.setattr(engine, 'catalog', catalog)
    monkeypatch.setattr(chain, 'SessionLocal', journal)
    monkeypatch.setattr(chain, '_master_lock', asyncio.Lock())
    monkeypatch.setattr(chain, '_write_lock', asyncio.Lock())
    monkeypatch.setattr(chain, '_master_jobs', {})
    monkeypatch.setattr(chain, '_attempts', {})
    token = 'offline-master-owner'
    with journal() as db:
        db.add(UserSession(token_hash=digest(token), csrf_token='master-csrf', expires_at=time.time() + 600))
        db.commit()

    def override():
        with journal() as db:
            yield db

    @asynccontextmanager
    async def lifespan(app):
        yield
        await chain.shutdown_master_jobs()

    monkeypatch.setitem(main.app.dependency_overrides, get_db, override)
    monkeypatch.setattr(main.app.router, 'lifespan_context', lifespan)
    monkeypatch.setattr(main, 'database_ready', True)
    with TestClient(main.app) as client:
        client.cookies.set(COOKIE, token)
        yield client, catalog


def batch():
    moment = datetime.now(timezone.utc)
    today = moment.astimezone(IST).date().isoformat()
    row = {'id': 'kotak:nse_fo:123', 'instrument_id': 'kotak:nse_fo:123',
           'broker': 'kotak', 'token': '123', 'underlying': 'NIFTY', 'symbol': 'NIFTY TEST CE',
           'exchange': 'NSE', 'segment': 'nse_fo', 'expiry': '2030-12-31', 'strike': 25000,
           'option_type': 'CE', 'lot_size': 50, 'demo': False, 'source': 'broker_master',
           'master_active': True, 'master_refresh_at': moment.isoformat(), 'master_source_date': today}
    metadata = {'last_successful_refresh_at': moment.isoformat(),
                'segments': {segment: {'source_date': today, 'active_count': 1} for segment in SEGMENTS}}
    return MasterBatch([row], metadata)


def test_refresh_returns_before_download_and_joins_one_job(jobs, monkeypatch):
    client, catalog = jobs
    entered, release = threading.Event(), threading.Event()
    calls = []
    def download(broker):
        calls.append(broker)
        entered.set()
        assert release.wait(5)
        return batch()
    monkeypatch.setattr(chain, 'download_master', download)
    headers = {'X-CSRF-Token': 'master-csrf'}
    url = '/api/option-chain/master/refresh'
    try:
        response = client.post(url, headers=headers)
        assert response.status_code == 202
        assert response.json()['accepted']
        assert entered.wait(2)
        assert not catalog.path.exists()
        status = client.get('/api/option-chain/master/status').json()
        assert status['refresh_in_progress'] and status['refresh_stage'] == 'downloading'
        assert client.post(url, headers=headers).status_code == 202
        assert calls == ['kotak']
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = client.get('/api/option-chain/master/status').json()
        if not status['refresh_in_progress']:
            break
        time.sleep(.01)
    assert status['new_trades_allowed'] and status['last_error'] is None
    assert catalog.path.exists()
    assert status['last_completed_at']


def test_refresh_still_requires_owner_and_csrf(jobs, monkeypatch):
    client, _ = jobs
    monkeypatch.setattr(chain, 'download_master', lambda broker: pytest.fail('Unauthorized download'))
    assert client.post('/api/option-chain/master/refresh').status_code == 403
    client.cookies.clear()
    assert client.get('/api/option-chain/master/status').status_code == 401
    assert client.post('/api/option-chain/master/refresh').status_code == 401


def test_status_does_not_build_full_option_chain_universe(jobs, monkeypatch):
    client, catalog = jobs
    chain.save_master('kotak', batch())
    monkeypatch.setattr(chain, 'live_universe', lambda broker: pytest.fail('Heavy chain construction in status'))
    status = client.get('/api/option-chain/master/status').json()
    assert status['new_trades_allowed'] and status['underlying_count'] == 1
    assert client.get('/api/option-chain/master/status').json()['underlying_count'] == 1
    assert catalog._live[0]['token'] == '123'
