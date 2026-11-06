"""Owner-triggered feed reconciliation against offline collectors and a SQLite journal."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import get_db
from app.models import UserSession
from app.security import COOKIE, digest
from test_chain_delete_journal import journal


class FakeFeed:
    def __init__(self):
        self.state = {'status': 'disconnected'}
        self.calls = []

    def statuses(self):
        return {'kotak': self.state}

    async def reconfigure(self, rows):
        self.calls.append(('reconfigure', [row['id'] for row in rows]))
        self.state = {'status': 'connecting'} if rows else {'status': 'disconnected'}

    async def stop(self):
        self.calls.append(('stop', []))
        self.state = {'status': 'disconnected'}

    async def start(self, rows):
        self.calls.append(('start', [row['id'] for row in rows]))
        self.state = {'status': 'connecting'}

    async def mark_market_closed(self):
        self.state = {'status': 'market_closed'}


@pytest.fixture
def feed(journal, monkeypatch):
    manager = FakeFeed()
    contract = {'id': 'kotak:offline-future', 'broker': 'kotak', 'source': 'broker_master', 'master_active': True}
    session = {'open': True, 'known': True, 'reason': 'session_open'}
    lease = SimpleNamespace(token='offline-lease')
    monkeypatch.setattr(main, 'feed_manager', manager)
    monkeypatch.setattr(main, 'database_ready', True)
    monkeypatch.setattr(main, 'collector_lease', lease)
    monkeypatch.setattr(main, 'exchange_calendar', SimpleNamespace(state=lambda row, now: session))
    monkeypatch.setattr(main.option_chain, 'configured', lambda broker: True)
    monkeypatch.setattr(main.option_chain, 'live_universe', lambda broker: {
        'NSE:NIFTY': {'reference_futures': [{**contract, 'id': 'unverified', 'master_active': False}, contract]}
    })
    monkeypatch.setattr(main, 'selected_feed_contracts', lambda db: [])
    monkeypatch.setattr(main, '_feed_refresh_lock', asyncio.Lock())
    monkeypatch.setattr(main, '_feed_contract_ids', None)
    monkeypatch.setattr(main, '_feed_auto_requested', False)
    monkeypatch.setattr(main, '_last_manual_reconnect', None)
    monkeypatch.setattr(main, '_runtime_settings', {'demo_mode': False, 'stale_after_seconds': 10})
    monkeypatch.setattr(main, '_activity_until', 0)
    monkeypatch.setattr(main, '_viewers', 1)
    return SimpleNamespace(manager=manager, contract=contract, session=session, lease=lease, db=journal)


def refresh(feed, owner_request=None):
    with feed.db() as db:
        return asyncio.run(main.refresh_selected_feeds(db, owner_request=owner_request))


def test_preopen_requests_survive_until_session_opens(feed):
    feed.session.update(open=False, reason='exchange_closed')
    assert refresh(feed, 'automatic')['status'] == 'market_closed'
    assert feed.manager.calls == [('reconfigure', [])]
    feed.session.update(open=True, reason='session_open')
    refresh(feed)
    assert feed.manager.calls[-1] == ('reconfigure', [feed.contract['id']])


def test_authenticated_viewer_recovers_reference_demand_after_backend_restart(feed, monkeypatch):
    monkeypatch.setattr(main, '_viewers', 0)
    refresh(feed)
    assert feed.manager.calls == [('reconfigure', [])]

    # An accepted authenticated dashboard socket re-establishes viewer demand.
    monkeypatch.setattr(main, '_viewers', 1)
    assert main._feed_auto_requested is False
    refresh(feed)
    assert feed.manager.calls[-1] == ('reconfigure', [feed.contract['id']])


def test_expired_selected_contract_without_references_requests_fresh_instruments(feed, monkeypatch):
    monkeypatch.setattr(main, 'selected_feed_contracts', lambda db: [feed.contract])
    monkeypatch.setattr(main.option_chain, 'live_universe', lambda broker: {})
    feed.session.update(open=False, reason='contract_expired_settlement_required')
    assert refresh(feed, 'automatic')['status'] == 'awaiting_instruments'
    assert feed.manager.calls == [('reconfigure', [])]


def test_connect_keeps_waiting_socket_and_recovers_disconnected_socket(feed):
    assert refresh(feed, 'automatic')['status'] == 'connecting'
    feed.manager.state = {'status': 'awaiting_quotes', 'awaiting_since': datetime.now(timezone.utc).isoformat()}
    for request in ('automatic', 'manual'):
        result = refresh(feed, request)
        assert result['ok'] and result['status'] == 'awaiting_quotes'
    assert len(feed.manager.calls) == 1
    feed.manager.state = {'status': 'disconnected'}
    assert refresh(feed, 'automatic')['status'] == 'connecting'
    assert feed.manager.calls == [('reconfigure', [feed.contract['id']])] * 2


def test_manual_restart_after_waiting_grace_has_cooldown(feed):
    refresh(feed, 'automatic')
    stale = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    feed.manager.state = {'status': 'awaiting_quotes', 'awaiting_since': stale}
    assert refresh(feed, 'manual')['status'] == 'connecting'
    assert feed.manager.calls[-2:] == [('stop', []), ('start', [feed.contract['id']])]
    feed.manager.state = {'status': 'disconnected'}
    result = refresh(feed, 'manual')
    assert result['status'] == 'rate_limited' and result['retry_after_seconds'] > 0
    assert len(feed.manager.calls) == 3


def test_automatic_request_cannot_loop_on_rejected_credentials(feed):
    refresh(feed, 'automatic')
    feed.manager.state = {'status': 'authentication_required'}
    assert refresh(feed, 'automatic')['status'] == 'authentication_required'
    refresh(feed)
    assert len(feed.manager.calls) == 1


def test_collector_handover_keeps_demand_without_starting_duplicate_socket(feed):
    feed.lease.token = None
    result = refresh(feed, 'automatic')
    assert result['status'] == 'rate_limited' and not feed.manager.calls
    feed.lease.token = 'offline-lease'
    refresh(feed)
    assert feed.manager.calls == [('reconfigure', [feed.contract['id']])]


@pytest.mark.parametrize('endpoint', ['connect', 'reconnect'])
def test_market_connection_routes_require_session_and_csrf(feed, monkeypatch, endpoint):
    token = 'offline-owner-session'
    with feed.db() as db:
        db.add(UserSession(token_hash=digest(token), csrf_token='offline-csrf', expires_at=time.time() + 600))
        db.commit()

    def override():
        with feed.db() as db:
            yield db

    monkeypatch.setitem(main.app.dependency_overrides, get_db, override)
    client = TestClient(main.app)
    try:
        url = f'/api/brokers/kotak/{endpoint}'
        assert client.post(url).status_code == 401
        client.cookies.set(COOKIE, token)
        assert client.post(url).status_code == 403
        assert not feed.manager.calls
        response = client.post(url, headers={'X-CSRF-Token': 'offline-csrf'})
        assert response.status_code == 200
        assert response.json()['status'] == 'connecting'
        assert feed.manager.calls[-1][1] == [feed.contract['id']]
    finally:
        client.close()
