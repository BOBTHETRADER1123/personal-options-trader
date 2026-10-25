"""Verified archival tests use tiny synthetic rows and mocked Google metadata."""
import asyncio
import copy
import hashlib
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine,func,select
from sqlalchemy.orm import sessionmaker
from app.database import Base,configure_sqlite
from app.models import PnlSnapshot,Trade,Owner
from app.cloud_backup import CloudDriveCredential,BackupError
from app.daily_reports import DailyReport,DailyReportPart,DailyReportService,_canonical
from app.sheets_sync import SheetsSyncService,SheetsConfig,SheetsProgress,DailyCloudCopy
from app.archival import AutomaticArchiver,ArchiveConfig,ArchiveReceipt,ArchiveState,ArchiveError,review_day,source_matches
from test_daily_reports import insert_trade,calendar as calendar_fixture,DAY,AFTER

NOW=datetime(2026,10,28,12,tzinfo=timezone.utc)
GIB=1024**3

@pytest.fixture
def prepared(tmp_path,request):
    sql=create_engine(f'sqlite:///{tmp_path / "archive.db"}')
    configure_sqlite(sql,journal_mode='DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False)
    from app.scheduler import ExchangeCalendar
    calendar=ExchangeCalendar({'rules':[{'exchange':'NSE','segments':['nse_fo'],'valid_from':'2026-01-01','valid_through':'2026-12-31','sources':['https://exchange.example/test'],'verified_at':'2026-10-24','sessions':[['09:15','15:40']],'weekdays':[0,1,2,3,4],'holidays':[]}]})
    with factory() as db:
        trade=insert_trade(db,seconds=getattr(request,'param',3))
        db.add(Owner(id=1,password_hash='SYNTHETIC TEST ONLY'))
        db.add(CloudDriveCredential(id=1,encrypted_refresh_token='TEST ENCRYPTED PLACEHOLDER',auth_required=False))
        db.add(SheetsProgress(trade_id=trade['id'],last_second=1792994102,acknowledged_rows=6))
        # Use the actual recorded final second, independently of hardcoded UTC.
        db.get(SheetsProgress,trade['id']).last_second=db.scalar(select(func.max(PnlSnapshot.snapshot_second)))
        db.commit()
    daily=DailyReportService(factory,calendar);daily.generate_day(DAY,AFTER)
    yield factory,daily,trade
    sql.dispose()

class Drive:
    def __init__(self):self.files={};self.uploads=0;self.corrupt=False;self.fail=False
    def close(self):pass
    def find(self,parent,key):return [x for x in self.files.values() if x['appProperties']['pot_backup_key']==key]
    def generate_id(self):return 'mock-'+str(len(self.files)+1)
    def metadata(self,identifier):
        result=copy.deepcopy(self.files.get(identifier))
        if result and self.corrupt:result['md5Checksum']='bad-checksum'
        return result
    def upload(self,identifier,name,parent,mime,key,content):
        if self.fail:raise BackupError('Mock Drive quota failure; source retained.')
        self.uploads+=1
        self.files[identifier]={'id':identifier,'name':name,'parents':[parent],'mimeType':mime,'size':str(len(content)),
            'md5Checksum':hashlib.md5(content).hexdigest(),'sha256Checksum':hashlib.sha256(content).hexdigest(),
            'appProperties':{'pot_backup_key':key},'trashed':False}
        return self.metadata(identifier)


def build(prepared,physical=13*GIB,enabled=True):
    factory,daily,_=prepared;drive=Drive()
    cloud=SimpleNamespace(config=SimpleNamespace(validate=lambda:None,max_file_bytes=64*1024**2),access_token=lambda:'MOCK TOKEN',_folder=lambda *args:'parent')
    sheets=SheetsSyncService(factory,cloud,SheetsConfig(enabled=True),drive_factory=lambda token:drive)
    service=AutomaticArchiver(cloud,sheets,daily,lambda:{'storage':{'application_bytes':physical}},asyncio.Lock(),factory,ArchiveConfig(enabled=enabled),now=lambda:NOW)
    return service,drive


def upload_all(service):
    assert service.prepare_once() is None
    assert service.prepare_once() is None
    return service.prepare_once()


def total(factory):
    with factory() as db:return db.scalar(select(func.count()).select_from(PnlSnapshot))


def test_disabled_is_idle_without_database_or_google():
    def forbidden():raise AssertionError('Disabled worker must remain idle')
    service=AutomaticArchiver(None,None,None,forbidden,asyncio.Lock(),forbidden,ArchiveConfig(enabled=False))
    assert service.prepare_once() is None


def test_below_threshold_does_not_upload_or_delete(prepared):
    service,drive=build(prepared,physical=GIB)
    assert service.prepare_once() is None
    assert drive.uploads==0 and total(prepared[0])==3
    assert service.status()['threshold_bytes']==12*GIB


def test_verified_upload_prunes_only_detail_and_retains_continuity(prepared):
    service,drive=build(prepared);plan,proof=upload_all(service)
    assert drive.uploads==2
    original=copy.deepcopy(prepared[2])
    assert service._prune(plan,proof)==2
    assert total(prepared[0])==1
    with prepared[0]() as db:
        assert db.get(Trade,original['id']).data==original
        assert db.get(Owner,1).password_hash=='SYNTHETIC TEST ONLY'
        receipt=db.get(ArchiveReceipt,DAY)
        assert receipt.snapshot_count==3 and receipt.retained_count==1 and len(receipt.evidence['google_files'])==2
        assert source_matches(db,db.get(DailyReport,DAY),1)
        assert db.scalar(select(func.count()).select_from(DailyReportPart))==2
    assert service._prune(plan,proof)==2
    assert total(prepared[0])==1


def test_remote_checksum_corruption_never_deletes(prepared):
    service,drive=build(prepared)
    service.prepare_once();service.prepare_once();drive.corrupt=True
    with pytest.raises(BackupError,match='checksum'):service.prepare_once()
    assert total(prepared[0])==3


def test_google_failure_preserves_source_and_persists_backoff(prepared):
    service,drive=build(prepared);drive.fail=True
    asyncio.run(service.cycle())
    assert total(prepared[0])==3 and service.status()['status']=='blocked'
    with prepared[0]() as db:assert db.get(ArchiveState,1).payload['next_attempt']>0
    replacement,_=build(prepared)
    assert replacement.prepare_once() is None
    assert total(prepared[0])==3


def test_unsynchronized_sheets_block_even_after_verified_drive_upload(prepared):
    service,_=build(prepared)
    with prepared[0]() as db:db.get(SheetsProgress,prepared[2]['id']).last_second=0;db.commit()
    service.prepare_once();service.prepare_once()
    with pytest.raises(ArchiveError,match='unsynchronized'):service.prepare_once()
    assert total(prepared[0])==3


def test_current_day_is_always_protected(prepared):
    with prepared[0]() as db:
        with pytest.raises(ArchiveError,match='Current-day'):review_day(db,DAY,AFTER)


def test_missing_csv_cannot_authorize_cleanup(prepared):
    with prepared[0]() as db:
        report=db.get(DailyReport,DAY);manifest=copy.deepcopy(report.manifest)
        manifest['artifacts']=[a for a in manifest['artifacts'] if a['kind']!='csv_gz']
        report.manifest=manifest;report.manifest_checksum=hashlib.sha256(_canonical(manifest)).hexdigest();db.commit()
        with pytest.raises(ArchiveError,match='Both Excel'):review_day(db,DAY,NOW)
    assert total(prepared[0])==3


def test_source_changed_after_google_verification_rolls_back(prepared):
    service,_=build(prepared);plan,proof=upload_all(service)
    with prepared[0]() as db:
        row=db.scalars(select(PnlSnapshot).order_by(PnlSnapshot.snapshot_second.desc())).first()
        db.add(PnlSnapshot(trade_id=row.trade_id,snapshot_second=row.snapshot_second+1,timestamp=row.timestamp,data=row.data));db.commit()
    with pytest.raises(ArchiveError,match='coverage'):service._prune(plan,proof)
    assert total(prepared[0])==4


def test_active_recording_defers_large_prune(prepared):
    service,_=build(prepared);service.prepare_once();service.prepare_once();service.can_prune=lambda:False
    asyncio.run(service.cycle())
    assert total(prepared[0])==3
    assert service.status()['status']=='waiting_for_market_close'


def test_local_acknowledgement_alone_never_qualifies_for_automatic_prune(prepared):
    service,_=build(prepared)
    with prepared[0]() as db:
        report=db.get(DailyReport,DAY);report.local_ack_checksum=report.manifest_checksum;report.local_ack_at='mock-ack';db.commit()
    plan=service._review(DAY)
    with pytest.raises(ArchiveError,match='Google Drive'):service._verify_google(plan,'MOCK')
    assert total(prepared[0])==3


@pytest.mark.parametrize('prepared',[452],indirect=True)
def test_bounded_prune_resumes_after_restart_with_exact_counts(prepared):
    service,drive=build(prepared);plan,proof=upload_all(service)
    with prepared[0]() as db:original=db.get(Trade,prepared[2]['id']).data
    assert service._prune(plan,proof)==200
    assert total(prepared[0])==252
    with prepared[0]() as db:
        receipt=db.get(ArchiveReceipt,DAY)
        assert receipt.status=='pruning' and receipt.deleted_count==200 and receipt.retained_count==1
        assert not source_matches(db,db.get(DailyReport,DAY),252)
        resumed=review_day(db,DAY,NOW)
        assert resumed['snapshot_count']==452 and resumed['remaining_count']==252
    # Release the original process's fenced lease, then restore a new worker.
    service.lease.release()
    replacement,_=build(prepared,physical=GIB)
    replacement.sheets=service.sheets
    # Even below target, finish an interrupted day so its cleanup receipt is
    # never left ambiguous. All original bytes remain in both verified files.
    resumed_plan,resumed_proof=replacement.prepare_once()
    assert resumed_plan['snapshot_count']==452
    assert replacement._prune(resumed_plan,resumed_proof)==400
    assert total(prepared[0])==52
    assert replacement._prune(resumed_plan,resumed_proof)==451
    assert total(prepared[0])==1 and drive.uploads==2
    with prepared[0]() as db:
        receipt=db.get(ArchiveReceipt,DAY)
        assert receipt.status=='completed' and receipt.deleted_count+receipt.retained_count==452
        assert source_matches(db,db.get(DailyReport,DAY),1)
        assert db.get(Trade,prepared[2]['id']).data==original


def test_remote_trashed_after_prepare_is_refreshed_before_delete(prepared):
    service,drive=build(prepared);plan,proof=upload_all(service)
    drive.files[proof[0]['drive_id']]['trashed']=True
    with pytest.raises(BackupError):service._prune(plan,proof)
    assert total(prepared[0])==3
    with prepared[0]() as db:assert db.get(ArchiveReceipt,DAY) is None


def test_local_copy_status_changed_after_prepare_blocks_prune(prepared):
    service,_=build(prepared);plan,proof=upload_all(service)
    with prepared[0]() as db:
        row=db.scalars(select(DailyCloudCopy)).first();row.status='trashed';db.commit()
    with pytest.raises(ArchiveError,match='status changed'):service._prune(plan,proof)
    assert total(prepared[0])==3


def test_low_plan_headroom_never_starts_delete(prepared):
    service,_=build(prepared);plan,proof=upload_all(service)
    service.sample_provider=lambda:{'storage':{'application_bytes':15*GIB-1024,'plan_limit_bytes':15*GIB}}
    with pytest.raises(ArchiveError,match='headroom'):service._prune(plan,proof)
    assert total(prepared[0])==3
    with prepared[0]() as db:assert db.get(ArchiveReceipt,DAY) is None


def test_low_filesystem_headroom_never_starts_delete(prepared,monkeypatch):
    service,_=build(prepared);plan,proof=upload_all(service)
    monkeypatch.setattr('app.archival.shutil.disk_usage',lambda path:SimpleNamespace(free=1024))
    with pytest.raises(ArchiveError,match='headroom'):service._prune(plan,proof)
    assert total(prepared[0])==3


def test_recording_start_during_fresh_google_check_prevents_delete(prepared):
    service,_=build(prepared);plan,proof=upload_all(service)
    verify=service._verify_google
    def market_opened(*args):
        result=verify(*args);service.can_prune=lambda:False;return result
    service._verify_google=market_opened
    with pytest.raises(ArchiveError,match='Recording resumed'):service._prune(plan,proof)
    assert total(prepared[0])==3


@pytest.mark.parametrize('prepared',[252],indirect=True)
def test_receipt_mismatch_after_partial_prune_blocks_more_deletes(prepared):
    service,_=build(prepared);plan,proof=upload_all(service)
    assert service._prune(plan,proof)==200
    with prepared[0]() as db:
        receipt=db.get(ArchiveReceipt,DAY);receipt.deleted_count=199;db.commit()
    with pytest.raises(ArchiveError,match='coverage'):service._prune(plan,proof)
    assert total(prepared[0])==52


def test_account_identity_change_after_remote_proof_blocks_delete(prepared):
    from app.google_accounts import GoogleAccountBinding
    service,_=build(prepared);plan,proof=upload_all(service)
    verify=service._verify_google
    def account_changed(*args):
        result=verify(*args)
        with prepared[0]() as db:
            db.add(GoogleAccountBinding(id=1,account_key='OTHER_ACCOUNT_HASH',email='new-account@example.test',connected_at='synthetic-test'))
            db.commit()
        return result
    service._verify_google=account_changed
    with pytest.raises(ArchiveError,match='Source changed'):service._prune(plan,proof)
    assert total(prepared[0])==3


def test_pruning_receipt_never_allows_cloud_cleanup_even_before_first_delete(prepared):
    service,_=build(prepared);plan,_=upload_all(service)
    with prepared[0]() as db:
        db.add(ArchiveReceipt(trading_date=DAY,manifest_checksum=plan['manifest_checksum'],snapshot_count=3,record_count=6,
            deleted_count=0,retained_count=1,status='pruning',evidence={'anchors':plan['anchors']}));db.commit()
        assert not source_matches(db,db.get(DailyReport,DAY),3)



def test_google_wait_never_holds_capture_lock_and_market_open_stops_delete(prepared):
    import threading
    service,_=build(prepared);plan,proof=upload_all(service)
    service.prepare_once=lambda:(plan,proof)
    entered,release=threading.Event(),threading.Event()
    original=service._verify_google
    def delayed_verify(*args):
        entered.set()
        assert release.wait(5),'test did not release Google wait'
        return original(*args)
    service._verify_google=delayed_verify
    async def scenario():
        task=asyncio.create_task(service.cycle())
        try:
            assert await asyncio.to_thread(entered.wait,2)
            assert not service.writer_lock.locked()
            await asyncio.wait_for(service.writer_lock.acquire(),0.3)
            service.writer_lock.release()
            service.can_prune=lambda:False
        finally:release.set()
        await asyncio.wait_for(task,3)
        assert not service.writer_lock.locked()
    asyncio.run(scenario())
    assert total(prepared[0])==3
    assert service.status()['status']=='blocked'
    assert 'Recording resumed' in service.status()['message']


def test_capture_lock_covers_only_local_archival_transaction(prepared,monkeypatch):
    import app.archival as module
    service,_=build(prepared);plan,proof=upload_all(service)
    original_verify=service._verify_google
    def verify(*args):
        assert not service.writer_lock.locked()
        return original_verify(*args)
    service._verify_google=verify
    original_transaction=module.run_transaction
    observed=[]
    def transaction(operation,*args,**kwargs):
        if operation.__name__=='operation':
            observed.append(service.writer_lock.locked())
        return original_transaction(operation,*args,**kwargs)
    monkeypatch.setattr(module,'run_transaction',transaction)
    async def scenario():
        assert await service._prune_batch(plan,proof)==2
        assert not service.writer_lock.locked()
    asyncio.run(scenario())
    assert observed==[True]
    assert total(prepared[0])==1


def test_cancel_while_waiting_for_recorder_never_leaks_capture_lock(prepared):
    import threading
    service,_=build(prepared);plan,proof=upload_all(service)
    entered=threading.Event()
    original_verify=service._verify_google
    def verify(*args):
        result=original_verify(*args);entered.set();return result
    service._verify_google=verify
    async def scenario():
        await service.writer_lock.acquire()
        task=asyncio.create_task(service._prune_batch(plan,proof))
        assert await asyncio.to_thread(entered.wait,2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,2)
        # The original recorder still owns this lock; the cancelled archival
        # holder must disappear from its wait queue rather than release it.
        assert service.writer_lock.locked()
        service.writer_lock.release()
        await asyncio.wait_for(service.writer_lock.acquire(),0.3)
        service.writer_lock.release()
        await asyncio.sleep(0)
        assert not service.writer_lock.locked()
    asyncio.run(scenario())
    assert total(prepared[0])==3
    with prepared[0]() as db:assert db.get(ArchiveReceipt,DAY) is None


def test_writer_gate_timeout_cancels_queued_holder_without_lock_leak():
    from app.archival import _AsyncWriterGate
    async def scenario():
        lock=asyncio.Lock();await lock.acquire()
        gate=_AsyncWriterGate(asyncio.get_running_loop(),lock,timeout=0.05)
        def worker():
            with gate:raise AssertionError('busy recorder must not grant gate')
        with pytest.raises(ArchiveError,match='Recorder is busy'):
            await asyncio.to_thread(worker)
        assert lock.locked()
        lock.release()
        await asyncio.wait_for(lock.acquire(),0.3)
        lock.release()
        await asyncio.sleep(0)
        assert not lock.locked()
    asyncio.run(scenario())


def test_cancel_during_local_transaction_waits_for_rollback_before_unlock(prepared,monkeypatch):
    import threading
    import app.archival as module
    service,_=build(prepared);plan,proof=upload_all(service)
    entered,release=threading.Event(),threading.Event()
    original_transaction=module.run_transaction
    def transaction(operation,*args,**kwargs):
        if operation.__name__=='operation':
            entered.set()
            assert release.wait(5),'test did not release local transaction'
        return original_transaction(operation,*args,**kwargs)
    monkeypatch.setattr(module,'run_transaction',transaction)
    async def scenario():
        task=asyncio.create_task(service._prune_batch(plan,proof))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            assert service.writer_lock.locked()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert service.writer_lock.locked()
            task.cancel()  # Repeated shutdown cancellation must still await rollback.
            await asyncio.sleep(0)
            assert not task.done()
            assert service.writer_lock.locked()
        finally:release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,3)
        assert not service.writer_lock.locked()
    asyncio.run(scenario())
    assert total(prepared[0])==3
    with prepared[0]() as db:assert db.get(ArchiveReceipt,DAY) is None
