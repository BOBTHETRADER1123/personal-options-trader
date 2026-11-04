"""Temporary DELETE-journal contention and fake sockets; no live broker calls."""
import asyncio
from contextlib import asynccontextmanager
import json
import sqlite3
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from app import main,engine
from app.database import Base,configure_sqlite,bounded_read,get_db
from app.models import UserSession,SocketTicket
from app.security import COOKIE,digest
from app.realtime import TicketRegistry,SharedDashboardPublisher,dashboard_fingerprint

@pytest.fixture
def journal(tmp_path,monkeypatch):
    path=tmp_path/'stream.db'
    sql=create_engine(f'sqlite:///{path.as_posix()}',connect_args={'check_same_thread':False})
    configure_sqlite(sql,'DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False)
    with factory() as db:
        db.add(UserSession(token_hash=digest('test-owner-cookie'),csrf_token='test-csrf',expires_at=time.time()+300));db.commit()
    def dep():
        with factory() as db:yield db
    @asynccontextmanager
    async def isolated_lifespan(_):yield
    monkeypatch.setattr(main.app.router,'lifespan_context',isolated_lifespan)
    main.app.dependency_overrides[get_db]=dep
    monkeypatch.setattr(main,'SessionLocal',factory)
    monkeypatch.setattr(main,'database_ready',True)
    monkeypatch.setattr(main,'_ws_tickets',TicketRegistry())
    monkeypatch.setattr(main,'_dashboard_publisher',SharedDashboardPublisher())
    yield path,factory
    main.app.dependency_overrides.clear();engine.clear_cache();sql.dispose()

def test_ticket_auth_csrf_one_use_and_no_sqlite_writes(journal):
    path,factory=journal
    with TestClient(main.app) as client:
        assert client.post('/api/ws-ticket').status_code==401
        client.cookies.set(COOKIE,'test-owner-cookie')
        assert client.post('/api/ws-ticket').status_code==403
        reply=client.post('/api/ws-ticket',headers={'x-csrf-token':'test-csrf'})
        assert reply.status_code==200
        ticket=reply.json()['ticket']
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(SocketTicket))==0
            assert db.connection().exec_driver_sql('PRAGMA journal_mode').scalar()=='delete'
            assert db.connection().exec_driver_sql('PRAGMA busy_timeout').scalar()==30000
            assert db.connection().exec_driver_sql('PRAGMA query_only').scalar()==0
        assert main.socket_identity(ticket,SimpleNamespace())[1]==digest('test-owner-cookie')
        assert main.socket_identity(ticket,SimpleNamespace()) is None
        assert main.socket_identity('invalid-ticket-12345',SimpleNamespace(cookies={COOKIE:'test-owner-cookie'})) is None

def test_exclusive_sqlite_writer_returns_quick_retry_instead_of_30_second_wait(journal):
    path,factory=journal
    writer=sqlite3.connect(path,isolation_level=None)
    writer.execute('BEGIN EXCLUSIVE')
    try:
        with TestClient(main.app) as client:
            client.cookies.set(COOKIE,'test-owner-cookie')
            before=time.perf_counter()
            reply=client.post('/api/ws-ticket',headers={'x-csrf-token':'test-csrf'})
            elapsed=time.perf_counter()-before
        assert reply.status_code==503 and reply.headers['retry-after']=='1'
        assert elapsed<.8,elapsed
        assert main.database_ready is True
    finally:writer.rollback();writer.close()
    with factory() as db:
        assert db.get(UserSession,digest('test-owner-cookie')) is not None
        assert db.connection().exec_driver_sql('PRAGMA journal_mode').scalar()=='delete'
        assert db.connection().exec_driver_sql('PRAGMA busy_timeout').scalar()==30000
        assert db.connection().exec_driver_sql('PRAGMA query_only').scalar()==0

def test_tickets_expire_and_concurrent_consumption_has_one_winner():
    from concurrent.futures import ThreadPoolExecutor
    now=[100.0];store=TicketRegistry(clock=lambda:now[0],max_items=2)
    ticket=store.issue('owner',200)['ticket']
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(store.consume,[ticket]*8))
    assert sum(result is not None for result in results)==1
    expired=store.issue('owner',200)['ticket'];now[0]+=31
    assert store.consume(expired) is None
    first=store.issue('owner',200)['ticket']
    store.issue('owner',200);store.issue('owner',200)
    assert store.consume(first) is None

def test_ten_viewers_share_one_build_and_unchanged_ages_do_not_resend():
    async def run():
        now=[100.0];publisher=SharedDashboardPublisher(clock=lambda:now[0],wall_clock=lambda:100)
        for i in range(10):publisher.register(str(i),200)
        calls=[]
        def build(hashes):
            calls.append(hashes)
            return {'data':{'server_time':str(now[0]),'quotes':[{'ltp':100,'feed_age':now[0],'stale':False}]},'sessions':dict.fromkeys(hashes,200)}
        results=await asyncio.gather(*(publisher.snapshot(build) for _ in range(10)))
        assert len(calls)==1 and len({id(result) for result in results})==1
        fingerprint=results[0].fingerprint
        now[0]+=1
        assert (await publisher.snapshot(build)).fingerprint==fingerprint
        assert len(calls)==2
        assert dashboard_fingerprint({'ltp':100,'stale':True})!=dashboard_fingerprint({'ltp':100,'stale':False})
        now[0]+=1
        def revoked(hashes):return {'data':{},'sessions':{}}
        assert not (await publisher.snapshot(revoked)).sessions
        publisher.register('0',200)
        assert not (await publisher.snapshot(revoked)).sessions
    asyncio.run(run())

def test_failed_build_is_bounded_across_clients():
    async def run():
        publisher=SharedDashboardPublisher(clock=lambda:100,wall_clock=lambda:100)
        publisher.register('owner',200);calls=[]
        def fail(_):calls.append(1);raise ValueError('offline test')
        for _ in range(10):
            with pytest.raises(ValueError):await publisher.snapshot(fail)
        assert len(calls)==1
    asyncio.run(run())

def test_socket_accept_does_not_wait_for_global_writer(monkeypatch):
    async def run():
        monkeypatch.setattr(main,'write_lock',asyncio.Lock())
        monkeypatch.setattr(main,'_dashboard_publisher',SharedDashboardPublisher())
        monkeypatch.setattr(main,'socket_identity',lambda *args:(time.time()+10,'owner'))
        monkeypatch.setattr(main,'socket_dashboard',lambda hashes:{'data':{},'sessions':{}})
        class Socket:
            headers={};query_params={};accepted=False
            async def accept(self):self.accepted=True
            async def close(self,code):self.code=code
        socket=Socket()
        async with main.write_lock:
            await asyncio.wait_for(main.websocket(socket),.3)
        assert socket.accepted and socket.code==4401
    asyncio.run(run())
