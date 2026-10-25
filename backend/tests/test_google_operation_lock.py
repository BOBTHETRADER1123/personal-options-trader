"""Mocked concurrency checks for account changes and Google acknowledgements."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs,urlparse

import httpx
from sqlalchemy import func,select

from app import cloud_backup as backup
from app.database import iso_now
from app.google_accounts import bind_account,GoogleAccountBinding,PreviousGoogleAccount
from app.models import PnlSnapshot
from app.sheets_sync import SheetsSyncService,SheetsConfig,SheetsProgress,SheetsDocument
from test_cloud_services import factory,config,seed,MockDrive,service


def new_account_client(called):
    def response(request):
        called.set()
        if request.method=='GET':
            return httpx.Response(200,json={'user':{'permissionId':'new-account','emailAddress':'new@example.test'}})
        return httpx.Response(200,json={'access_token':'new-access','refresh_token':'new-refresh','scope':backup.SCOPE})
    return httpx.Client(transport=httpx.MockTransport(response))


def prepare_switch(factory,config):
    with factory() as db:
        bind_account(db,{'permissionId':'old-account','emailAddress':'old@example.test'})
        db.commit()
        return parse_qs(urlparse(backup.begin_authorization(db,'owner-session',config)).query)['state'][0]


def test_account_switch_waits_for_inflight_sheet_ack_then_resets_old_ids(factory,config):
    seed(factory,config)
    state=prepare_switch(factory,config)
    with factory() as db:
        db.add(PnlSnapshot(trade_id='trade-one',snapshot_second=1792994100,timestamp=iso_now(),
            data={'timestamp_utc':iso_now(),'net_pnl':1,'legs':[{'id':'leg-one','open_lots':1,'lot_size':50}]}))
        db.commit()
    entered,release,oauth_called,switch_started=(threading.Event() for _ in range(4))
    class DelayedSheet:
        def quota(self):return {'storageQuota':{'usage':'1','limit':'100000'}}
        def ensure_document(self,doc):return 'old-account-sheet',[]
        def prepare_tabs(self,*args):pass
        def close(self):pass
        def write_verified(self,*args):
            entered.set()
            assert release.wait(5),'test did not release mock Google response'
    cloud=service(factory,config,MockDrive())
    cloud._token,cloud._expires='old-access',time.time()+3600
    cloud.last_success='old-account-backup-success'
    sheets=SheetsSyncService(factory,cloud,SheetsConfig(enabled=True),lambda token:DelayedSheet())
    with new_account_client(oauth_called) as client,ThreadPoolExecutor(max_workers=2) as pool:
        upload=pool.submit(sheets.sync_once)
        assert entered.wait(5)
        def switch():
            switch_started.set()
            return cloud.complete_authorization('owner-session',state,'mock-code',client)
        changed=pool.submit(switch)
        try:
            assert switch_started.wait(5)
            assert not oauth_called.wait(.1),'account reset raced an old upload acknowledgement'
        finally:release.set()
        assert upload.result(timeout=5)['synced_rows']==1
        assert changed.result(timeout=5)['account_changed']
    with factory() as db:
        assert db.get(GoogleAccountBinding,1).email=='new@example.test'
        assert db.scalar(select(func.count()).select_from(SheetsProgress))==0
        assert db.scalar(select(func.count()).select_from(SheetsDocument))==0
        previous=db.scalars(select(PreviousGoogleAccount)).one().metadata_json
        assert previous['sheets_documents'][0]['drive_id']=='old-account-sheet'
        assert previous['sheets_progress'][0]['acknowledged_rows']==1
        assert previous['sheets_batches'][0]['status']=='verified'
        assert db.scalar(select(func.count()).select_from(PnlSnapshot))==1
    assert cloud._token is None and cloud.last_success is None


def test_refresh_rotation_finishes_before_account_switch_and_old_token_is_not_cached(factory,config):
    seed(factory,config)
    state=prepare_switch(factory,config)
    entered,release,oauth_called,switch_started=(threading.Event() for _ in range(4))
    def refresh(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200,json={'access_token':'old-refreshed-access','refresh_token':'old-rotated-refresh','expires_in':3600})
    with httpx.Client(transport=httpx.MockTransport(refresh)) as token_client,new_account_client(oauth_called) as oauth_client,ThreadPoolExecutor(max_workers=2) as pool:
        cloud=backup.CloudBackupService(config,factory,token_client=token_client)
        refreshed=pool.submit(cloud.access_token)
        assert entered.wait(5)
        def switch():
            switch_started.set()
            return cloud.complete_authorization('owner-session',state,'mock-code',oauth_client)
        changed=pool.submit(switch)
        try:
            assert switch_started.wait(5)
            assert not oauth_called.wait(.1)
        finally:release.set()
        assert refreshed.result(timeout=5)=='old-refreshed-access'
        assert changed.result(timeout=5)['account_changed']
    with factory() as db:
        assert config.decrypt(db.get(backup.CloudDriveCredential,1).encrypted_refresh_token)=='new-refresh'
        assert db.get(GoogleAccountBinding,1).email=='new@example.test'
    assert cloud._token is None and cloud._expires==0


def test_oauth_network_wait_does_not_hold_market_capture_lock(monkeypatch):
    from app import main
    entered,release=threading.Event(),threading.Event()
    def authorize(*args):
        entered.set()
        assert release.wait(5)
    cloud=SimpleNamespace(operation_lock=threading.RLock(),complete_authorization=authorize,notify_work=lambda:None)
    monkeypatch.setattr(main,'cloud_service',cloud)
    monkeypatch.setattr(main,'sheets_service',None)
    monkeypatch.setattr(main,'storage_archiver',None)
    async def exercise():
        lock=asyncio.Lock()
        monkeypatch.setattr(main,'write_lock',lock)
        pending=asyncio.create_task(main.drive_callback(state='mock-state-with-length',code='mock-code',current=SimpleNamespace(token_hash='owner'),db=None))
        try:
            assert await asyncio.to_thread(entered.wait,5)
            await asyncio.wait_for(lock.acquire(),timeout=.2)
            lock.release()
        finally:release.set()
        assert (await pending).status_code==303
    asyncio.run(exercise())


def test_oauth_http_requests_leave_sqlite_free_for_recorder_commits(factory,config):
    import sqlite3
    seed(factory,config)
    state=prepare_switch(factory,config)
    writes=[]
    def response(request):
        # Separate connection represents the one-second recording thread. A
        # leaked OAuth read transaction blocks its commit in DELETE mode.
        with sqlite3.connect(factory.kw['bind'].url.database,timeout=.05) as recorder:
            recorder.execute('UPDATE cloud_drive_credentials SET last_error=? WHERE id=1',('recorder probe',))
        writes.append(request.method)
        if request.method=='GET':
            return httpx.Response(200,json={'user':{'permissionId':'new-account','emailAddress':'new@example.test'}})
        return httpx.Response(200,json={'access_token':'new-access','refresh_token':'new-refresh','scope':backup.SCOPE})
    cloud=backup.CloudBackupService(config,factory)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert cloud.complete_authorization('owner-session',state,'mock-code',client)['connected']
    assert writes==['POST','GET']


def test_cloud_cleanup_network_wait_does_not_hold_market_capture_lock(monkeypatch):
    from app import main
    entered,release=threading.Event(),threading.Event()
    def cleanup(*args):
        assert args==('2026-10-26','a'*64,100,True,'DELETE VERIFIED CLOUD COPIES')
        entered.set()
        assert release.wait(5)
        return {'moved_to_trash':2,'source_records_deleted':False}
    monkeypatch.setattr(main,'sheets_service',SimpleNamespace(cleanup_confirm=cleanup))
    async def exercise():
        lock=asyncio.Lock()
        monkeypatch.setattr(main,'write_lock',lock)
        payload=main.LocalBackupInput(sha256='a'*64,records=100,local_backup_confirmed=True,confirmation='DELETE VERIFIED CLOUD COPIES')
        pending=asyncio.create_task(main.daily_cleanup('2026-10-26',payload))
        try:
            assert await asyncio.to_thread(entered.wait,5)
            await asyncio.wait_for(lock.acquire(),timeout=.2)
            lock.release()
        finally:release.set()
        assert (await pending)=={'moved_to_trash':2,'source_records_deleted':False}
    asyncio.run(exercise())
