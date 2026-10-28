"""Unattended positions stay subscribed without expanding to the whole catalog."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import main
from app.brokers import FeedManager
from app.database import Base
from app.models import Trade


@pytest.mark.parametrize('watchlist', ['kotak:watched', ''])
def test_active_legs_survive_watchlist_filter_without_subscribing_unselected_catalog(monkeypatch, watchlist):
    monkeypatch.setenv('FEED_INSTRUMENT_IDS', watchlist)
    contracts = [{'id': 'kotak:' + name, 'broker': 'kotak', 'demo': False}
                 for name in ('watched', 'active', 'pending', 'closed', 'closed-leg', 'unused')]
    lookups=[]
    def selected_rows(identifiers, brokers):
        lookups.append(set(identifiers))
        return [row for row in contracts if row['id'] in identifiers and row['broker'] in brokers]
    monkeypatch.setattr(main, 'catalog', SimpleNamespace(select_live=selected_rows))
    database = create_engine('sqlite://')
    Base.metadata.create_all(database)
    try:
        with Session(database) as db:
            for identifier, status, leg_status in [('active', 'open', 'open'), ('pending', 'pending', 'pending'),
                                                   ('closed', 'closed', 'open'), ('closed-leg', 'open', 'closed')]:
                db.add(Trade(id=identifier, broker='kotak', status=status,
                             data={'demo': False, 'legs': [{'instrument_id': 'kotak:' + identifier, 'status': leg_status}]}))
            db.commit()
            selected = main.selected_feed_contracts(db)
        expected = {'kotak:active', 'kotak:pending'} | ({watchlist} if watchlist else set())
        assert {row['id'] for row in selected} == expected
        assert lookups==[expected]  # Only demanded IDs, never a full-catalog copy.

        async def verify():
            received = []
            started = asyncio.Event()

            class Adapter:
                broker = 'kotak'

                def configured(self):
                    return True

                async def stream(self, instruments):
                    received.extend(instruments)
                    started.set()
                    await asyncio.Event().wait()

            async def on_quote(quote):
                raise AssertionError('This isolated subscription test must not receive external quotes.')

            manager = FeedManager(on_quote)
            manager._adapters = [Adapter()]
            try:
                await manager.start(selected)
                await asyncio.wait_for(started.wait(), timeout=1)
                assert {row['id'] for row in received} == expected
            finally:
                await manager.stop()

        asyncio.run(verify())
    finally:
        database.dispose()
