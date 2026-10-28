"""Real rollback-journal reads end before asynchronous socket changes.

Only a temporary SQLite file and mocked sockets are used. These are not live
broker acceptance results.
"""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app import engine, main, option_chain as chain
from app.database import Base, configure_sqlite
from app.instruments import ContractCatalog
from app.models import Setting


@pytest.fixture
def journal(tmp_path):
    sql=create_engine(f"sqlite:///{(tmp_path/'rollback.db').as_posix()}",
                      connect_args={'check_same_thread':False})
    configure_sqlite(sql,'DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False)
    with factory() as db:
        db.add(Setting(key='probe',value={'writes':0}));db.commit()
        engine.update_settings(db,{'demo_mode':False});db.commit()
    yield factory
    engine.clear_cache();sql.dispose()


def external_write(factory):
    with factory() as writer:
        # Bound failures so a regression reports an assertion rather than
        # waiting production's 30-second SQLite busy timeout.
        writer.connection().exec_driver_sql('PRAGMA busy_timeout=150')
        row=writer.get(Setting,'probe')
        writer.execute(update(Setting).where(Setting.key=='probe').values(value={'writes':row.value['writes']+1}))
        writer.commit()


def test_subscription_readers_are_closed_before_socket_await(journal,monkeypatch):
    selected_thread=[];heartbeat=[]
    def selected(reader):
        selected_thread.append(threading.get_ident())
        reader.get(Setting,'probe')
        time.sleep(.06)  # Simulate a slow DB/catalog lookup off the event loop.
        return [{'id':'kotak:mcx_fo:123','broker':'kotak'}]
    monkeypatch.setattr(main,'selected_feed_contracts',selected)
    monkeypatch.setattr(main,'collector_lease',None)
    monkeypatch.setattr(main,'exchange_calendar',None)
    monkeypatch.setattr(main,'_feed_contract_ids',None)
    monkeypatch.setattr(main,'_feed_refresh_lock',asyncio.Lock())
    caller=journal()
    caller.get(Setting,'probe')  # Explicit BEGIN + shared read lock.
    calls=[]
    class Manager:
        async def stop(self):
            assert not caller.in_transaction()
            await asyncio.to_thread(external_write,journal)
            calls.append('stop')
        async def start(self,contracts):calls.append(('start',contracts[0]['id']))
    monkeypatch.setattr(main,'feed_manager',Manager())
    async def run():
        loop_thread=threading.get_ident()
        async def pulse():
            for _ in range(8):heartbeat.append(time.monotonic());await asyncio.sleep(.01)
        await asyncio.gather(main.refresh_selected_feeds(caller),pulse())
        assert selected_thread and selected_thread[0]!=loop_thread
    try:
        asyncio.run(run())
        assert len(heartbeat)==8 and heartbeat[-1]-heartbeat[0]>=.05
        assert calls==['stop',('start','kotak:mcx_fo:123')]
        assert not caller.in_transaction()
        with journal() as check:assert check.get(Setting,'probe').value=={'writes':1}
    finally:caller.close()


def test_concurrent_subscription_refreshes_do_not_overlap(journal,monkeypatch):
    activity={'inside':0,'peak':0,'starts':0}
    class Manager:
        async def stop(self):
            activity['inside']+=1;activity['peak']=max(activity['peak'],activity['inside'])
            await asyncio.sleep(.025)
        async def start(self,contracts):
            activity['inside']-=1;activity['starts']+=1
    monkeypatch.setattr(main,'feed_manager',Manager())
    monkeypatch.setattr(main,'collector_lease',None)
    monkeypatch.setattr(main,'exchange_calendar',None)
    monkeypatch.setattr(main,'_feed_contract_ids',None)
    monkeypatch.setattr(main,'_feed_refresh_lock',asyncio.Lock())
    monkeypatch.setattr(main,'selected_feed_contracts',lambda db:[])
    async def run():
        with journal() as first,journal() as second:
            await asyncio.gather(main.refresh_selected_feeds(first,True),main.refresh_selected_feeds(second,True))
    asyncio.run(run())
    assert activity=={'inside':0,'peak':1,'starts':2}


def test_option_chain_ends_read_before_feed_and_threads_quote_queries(journal,tmp_path,monkeypatch):
    previous=engine.catalog
    catalog=ContractCatalog(tmp_path/'empty-master.json')
    catalog.merge_live_contracts([{'id':'kotak:nse_fo:123','instrument_id':'kotak:nse_fo:123',
        'broker':'kotak','token':'123','underlying':'NIFTY','exchange':'NSE','segment':'nse_fo',
        'symbol':'OFFLINE NIFTY CE','expiry':'2030-10-31','strike':25000,'option_type':'CE',
        'lot_size':50,'tick_size':.05,'demo':False}])
    engine.set_catalog(catalog);monkeypatch.setattr(chain,'configured',lambda broker:False)
    settings_thread=[];quotes_thread=[]
    get_settings=engine.get_settings;quote_data=chain.cached_live_data
    def settings(db):settings_thread.append(threading.get_ident());return get_settings(db)
    def quotes(*args):quotes_thread.append(threading.get_ident());return quote_data(*args)
    monkeypatch.setattr(engine,'get_settings',settings);monkeypatch.setattr(chain,'cached_live_data',quotes)
    with journal() as db:
        db.get(Setting,'probe')
        async def refresh(incoming):
            assert incoming is db and not incoming.in_transaction()
            await asyncio.to_thread(external_write,journal)
        monkeypatch.setattr(chain,'_refresh_feeds',refresh)
        async def run():
            loop_thread=threading.get_ident()
            result=await chain.option_chain('NSE:NIFTY',mode='live',broker='kotak',db=db)
            assert result['status']=='credentials_required' and not result['quotes']
            assert settings_thread and all(i!=loop_thread for i in settings_thread)
            assert quotes_thread and all(i!=loop_thread for i in quotes_thread)
            assert not db.in_transaction()
        try:asyncio.run(run())
        finally:engine.set_catalog(previous);chain._requested.clear()
    with journal() as check:assert check.get(Setting,'probe').value=={'writes':1}


def test_read_finisher_refuses_uncommitted_orm_changes(journal):
    with journal() as db:
        pending=Setting(key='uncommitted',value={'private':'offline-test'})
        db.add(pending)
        with pytest.raises(RuntimeError,match='Save pending paper state'):
            chain.finish_read_transaction(db)
        assert pending in db.new
        db.rollback()
    with journal() as check:assert check.get(Setting,'uncommitted') is None
