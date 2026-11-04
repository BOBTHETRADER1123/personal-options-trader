"""Slow fake broker subscriptions must not serialize recorder/API writers."""
import asyncio
import time
from types import SimpleNamespace
import pytest
from app import main
from test_chain_delete_journal import journal


def test_recorder_keeps_running_and_writer_is_free_during_subscription_wait(journal,monkeypatch):
    async def run():
        started=asyncio.Event();release=asyncio.Event();captures=[]
        class Manager:
            async def reconfigure(self,contracts):
                assert not main.write_lock.locked()
                started.set();await release.wait()
            async def stop(self):pass
        class Lease:
            token='test-lease'
            def renew(self):return True
            def release(self):self.token=None
        monkeypatch.setattr(main,'write_lock',asyncio.Lock())
        monkeypatch.setattr(main,'_feed_refresh_lock',asyncio.Lock())
        monkeypatch.setattr(main,'stop_event',asyncio.Event())
        monkeypatch.setattr(main,'SessionLocal',journal)
        monkeypatch.setattr(main,'database_ready',True)
        monkeypatch.setattr(main,'collector_lease',Lease())
        monkeypatch.setattr(main,'feed_manager',Manager())
        monkeypatch.setattr(main,'exchange_calendar',None)
        monkeypatch.setattr(main,'_feed_contract_ids',None)
        monkeypatch.setattr(main,'recording_needed',lambda:True)
        monkeypatch.setattr(main,'selected_feed_contracts',lambda db:[{'id':'test:contract','broker':'kotak'}])
        def capture(*_):captures.append(time.monotonic());return {}
        monkeypatch.setattr(main,'capture_once',capture)
        task=asyncio.create_task(main.background_loop())
        try:
            await asyncio.wait_for(started.wait(),1)
            async def acquire():
                async with main.write_lock:return True
            assert await asyncio.wait_for(acquire(),.2)
            before=len(captures)
            for _ in range(30):
                if len(captures)>before:break
                await asyncio.sleep(.05)
            assert len(captures)>before,'Capture stalled while subscription awaited'
        finally:
            release.set();main.stop_event.set();await asyncio.wait_for(task,2)
    asyncio.run(run())


def test_verified_closed_session_reports_market_closed(journal,monkeypatch):
    calls=[]
    class Manager:
        async def reconfigure(self,contracts):calls.append(('contracts',contracts))
        async def mark_market_closed(self):calls.append(('status','market_closed'))
    monkeypatch.setattr(main,'feed_manager',Manager())
    monkeypatch.setattr(main,'collector_lease',None)
    monkeypatch.setattr(main,'exchange_calendar',SimpleNamespace(state=lambda *args:{'open':False}))
    monkeypatch.setattr(main,'selected_feed_contracts',lambda db:[{'id':'test:contract','broker':'kotak'}])
    monkeypatch.setattr(main,'_feed_contract_ids',None)
    async def run():
        monkeypatch.setattr(main,'_feed_refresh_lock',asyncio.Lock())
        with journal() as db:await main.refresh_selected_feeds(db)
    asyncio.run(run())
    assert calls==[('contracts',[]),('status','market_closed')]


@pytest.mark.parametrize('action',[False,True])
def test_paper_routes_release_writer_before_refresh(journal,monkeypatch,action):
    result={'trades':[],'results':[]}
    monkeypatch.setattr(main,'collector_lease',None)
    monkeypatch.setattr(main.engine,'create_trades',lambda *args:result)
    monkeypatch.setattr(main.engine,'apply_action',lambda *args:result)
    monkeypatch.setattr(main,'refresh_demo_for_payload',lambda *args:None)
    monkeypatch.setattr(main,'update_demand',lambda *args:None)
    calls=[]
    async def refresh(db):
        assert not main.write_lock.locked()
        assert not db.in_transaction()
        async with main.write_lock:calls.append(True)
    monkeypatch.setattr(main,'refresh_selected_feeds',refresh)
    payload=SimpleNamespace(idempotency_key='test-route-no-record',model_dump=lambda:{})
    async def run():
        monkeypatch.setattr(main,'write_lock',asyncio.Lock())
        with journal() as db:
            value=await (main.trade_action('nonexistent',payload,db) if action else main.create_trade(payload,db))
            assert value is result
    asyncio.run(run());assert calls==[True]
