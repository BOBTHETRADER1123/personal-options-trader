"""Automatic, verified daily-history retention. Never clears a workspace.

Only immutable, older PnlSnapshot rows are eligible. Accounts, positions,
orders, source quote journals, summaries, daily exports and continuity anchors
are retained. SQLite free pages are reused; no unsafe full-volume VACUUM runs.
"""
from __future__ import annotations
import asyncio
import concurrent.futures
import threading
import copy
from contextlib import nullcontext
import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import time
from zoneinfo import ZoneInfo
from sqlalchemy import Integer, JSON, String, Text, cast, delete, func, select, tuple_
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base, SessionLocal, iso_now, run_transaction
from .models import PnlSnapshot
from .daily_reports import DailyReport, DailyReportPart, bounds, artifact, _canonical
from .sheets_sync import DailyCloudCopy, SheetsBatch, SheetsProgress
from .scheduler import CollectorLease, guard_lease
from .cloud_backup import BackupError, CloudDriveCredential, file_key, verify_remote

IST=ZoneInfo('Asia/Kolkata')

class ArchiveReceipt(Base):
    __tablename__='verified_snapshot_prunes'
    trading_date:Mapped[str]=mapped_column(String(10),primary_key=True)
    manifest_checksum:Mapped[str]=mapped_column(String(64))
    snapshot_count:Mapped[int]=mapped_column(Integer)
    record_count:Mapped[int]=mapped_column(Integer)
    deleted_count:Mapped[int]=mapped_column(Integer)
    retained_count:Mapped[int]=mapped_column(Integer)
    status:Mapped[str]=mapped_column(String(20),default='pruning')
    evidence:Mapped[dict]=mapped_column(JSON)
    pruned_at:Mapped[str]=mapped_column(String(40),default=iso_now)

class ArchiveState(Base):
    __tablename__='automatic_archive_state'
    id:Mapped[int]=mapped_column(Integer,primary_key=True,default=1)
    payload:Mapped[dict]=mapped_column(JSON,default=dict)

class ArchiveError(BackupError):pass


class _AsyncWriterGate:
    """Hold an async lock only for a worker's local transaction.

    The worker retains the Google RLock while waiting. Its holder coroutine
    owns the async lock, so cancellation before acquisition cannot leak it.
    Cancellation of the caller waits for an already-running transaction before
    releasing the lock; Google requests never enter this gate.
    """
    def __init__(self,loop,lock,timeout=30):
        self.loop,self.lock,self.timeout=loop,lock,timeout
        self.acquired=threading.Event();self.cancelled=threading.Event()
        self.release_event=asyncio.Event();self.future=None
    async def _hold(self):
        async with self.lock:
            self.acquired.set()
            await self.release_event.wait()
    def __enter__(self):
        if self.cancelled.is_set():raise ArchiveError('Archival cancelled; source retained.')
        self.future=asyncio.run_coroutine_threadsafe(self._hold(),self.loop)
        deadline=time.monotonic()+self.timeout
        try:
            while not self.acquired.wait(0.02):
                if self.cancelled.is_set():raise ArchiveError('Archival cancelled; source retained.')
                if self.future.done():self.future.result()
                if time.monotonic()>=deadline:raise ArchiveError('Recorder is busy; archival deferred.')
            if self.cancelled.is_set():raise ArchiveError('Archival cancelled; source retained.')
        except BaseException:
            self.future.cancel()
            raise
        return self
    def __exit__(self,*args):
        self.loop.call_soon_threadsafe(self.release_event.set)
        try:self.future.result(timeout=10)
        except (concurrent.futures.CancelledError,concurrent.futures.TimeoutError):
            # The transaction has ended before __exit__. Cancellation of the
            # holder is therefore safe and still executes async-with cleanup.
            self.future.cancel()


@dataclass(frozen=True)
class ArchiveConfig:
    enabled:bool=False
    threshold_bytes:int=12*1024**3
    target_bytes:int=10*1024**3
    interval_seconds:int=60
    @classmethod
    def from_env(cls):
        threshold=int(float(os.getenv('AUTO_ARCHIVE_TRIGGER_GB','12'))*1024**3)
        target=int(float(os.getenv('AUTO_ARCHIVE_TARGET_GB','10'))*1024**3)
        if not 0<target<threshold:raise ValueError('Archive target must be below the trigger.')
        return cls(os.getenv('AUTO_ARCHIVE_ENABLED','false').lower()=='true',threshold,target,
                   max(30,int(os.getenv('AUTO_ARCHIVE_INTERVAL_SECONDS','60'))))

def source_matches(db,report,captured):
    receipt=db.get(ArchiveReceipt,report.trading_date)
    # A partially pruned day is deliberately unavailable for cloud deletion.
    if receipt and receipt.status!='completed':return False
    if captured==report.snapshot_count:return True
    return bool(receipt and receipt.manifest_checksum==report.manifest_checksum
                and receipt.snapshot_count==report.snapshot_count and receipt.record_count==report.record_count
                and receipt.deleted_count+captured==report.snapshot_count and captured==receipt.retained_count)


def review_day(db,day,now=None):
    moment=now or datetime.now(timezone.utc)
    if day>=moment.astimezone(IST).date().isoformat():raise ArchiveError('Current-day history is protected.')
    report=db.get(DailyReport,day)
    if not report or report.status!='finalized':raise ArchiveError('A finalized daily export is required.')
    if hashlib.sha256(_canonical(report.manifest)).hexdigest()!=report.manifest_checksum:
        raise ArchiveError('Daily manifest integrity verification failed; records retained.')
    previous=db.get(ArchiveReceipt,day)
    if previous and (previous.manifest_checksum!=report.manifest_checksum or previous.record_count!=report.record_count):
        raise ArchiveError('Export changed after archival; review required.')
    start,end=bounds(day)
    captured=db.scalar(select(func.count()).select_from(PnlSnapshot).where(PnlSnapshot.snapshot_second>=start,PnlSnapshot.snapshot_second<end)) or 0
    original=report.snapshot_count
    deleted=previous.deleted_count if previous else 0
    if original<1 or captured+deleted!=original or previous and previous.snapshot_count!=original:
        raise ArchiveError('Export coverage no longer matches source snapshots.')
    anchors=dict(db.execute(select(PnlSnapshot.trade_id,func.max(PnlSnapshot.snapshot_second)).where(
        PnlSnapshot.snapshot_second>=start,PnlSnapshot.snapshot_second<end).group_by(PnlSnapshot.trade_id)).all())
    if previous:
        expected_anchors=(previous.evidence or {}).get('anchors')
        if anchors!=expected_anchors or previous.retained_count!=len(anchors):
            raise ArchiveError('Continuity anchors changed during archival; source retained.')
        if previous.status=='completed':
            if captured!=previous.retained_count:raise ArchiveError('Completed archival source count changed.')
            return {'already_pruned':True,'trading_date':day,'deleted_count':previous.deleted_count}
        if previous.status!='pruning':raise ArchiveError('Unknown archival recovery state; source retained.')
    pending=db.scalar(select(func.count()).select_from(SheetsBatch).where(SheetsBatch.trading_date==day,SheetsBatch.status!='verified')) or 0
    unsent=db.scalar(select(func.count()).select_from(PnlSnapshot).outerjoin(SheetsProgress,SheetsProgress.trade_id==PnlSnapshot.trade_id).where(
        PnlSnapshot.snapshot_second>=start,PnlSnapshot.snapshot_second<end,
        PnlSnapshot.snapshot_second>func.coalesce(SheetsProgress.last_second,0))) or 0
    if pending or unsent:raise ArchiveError('Google Sheets still has unsynchronized records; source retained.')
    specs=copy.deepcopy(report.manifest.get('artifacts',[]))
    if not specs or len({(a['kind'],a['part_number']) for a in specs})!=len(specs):raise ArchiveError('Daily export parts are incomplete.')
    pairs={a['part_number'] for a in specs if a['kind']=='xlsx'}
    if not pairs or pairs!={a['part_number'] for a in specs if a['kind']=='csv_gz'}:raise ArchiveError('Both Excel and compressed CSV parts are required.')
    if any(sum(a.get('record_count',0) for a in specs if a['kind']==kind)!=report.record_count for kind in ('xlsx','csv_gz')):
        raise ArchiveError('Daily export record coverage differs from its manifest.')
    coverage=report.manifest.get('coverage',[])
    if sum(a.get('snapshot_count',0) for a in coverage)!=original or sum(a.get('record_count',0) for a in coverage)!=report.record_count:
        raise ArchiveError('Trade worksheet coverage is incomplete.')
    for spec in specs:
        row=db.get(DailyReportPart,f"{day}:{spec['kind']}:{int(spec['part_number']):04d}")
        if not row or any(getattr(row,k)!=spec[k] for k in ('sha256','size','record_count')):
            raise ArchiveError('A retained daily artifact differs from the verified manifest.')
    from .google_accounts import current_account_key
    return {'account_key':current_account_key(db),'trading_date':day,'manifest_checksum':report.manifest_checksum,'snapshot_count':original,
        'record_count':report.record_count,'specs':specs,'anchors':anchors,'already_pruned':False,
        'remaining_count':captured,'deleted_count':deleted}

class AutomaticArchiver:
    def __init__(self,cloud,sheets,daily,sample_provider,writer_lock,session_factory=SessionLocal,config=None,now=None,ready_provider=None,can_prune=None):
        self.can_prune=can_prune or (lambda:True)
        self.ready_provider=ready_provider or (lambda:True)
        self.cloud,self.sheets,self.daily=cloud,sheets,daily
        self.sample_provider,self.writer_lock,self.factory=sample_provider,writer_lock,session_factory
        self.config=config or ArchiveConfig.from_env();self.now=now or (lambda:datetime.now(timezone.utc))
        self.lease=CollectorLease(session_factory,name='verified_storage_archive',ttl_seconds=120)
        self.state={};self._loaded=False;self._wake=None;self._loop=None
    def notify_work(self):
        if self._loop and self._wake:self._loop.call_soon_threadsafe(self._wake.set)
    def status(self):
        return {'enabled':self.config.enabled,'threshold_bytes':self.config.threshold_bytes,'target_bytes':self.config.target_bytes,
            'status':'not_enabled' if not self.config.enabled else self.state.get('status','checking'),
            'message':self.state.get('message','Google authorization and a verified backup are required before cleanup.'),
            'google_required':True,'physical_file_shrink_automatic':False,**{k:v for k,v in self.state.items() if k not in ('status','message')}}
    def _save(self,status,message,**fields):
        payload={**self.state,**fields,'status':status,'message':message}
        if payload==self.state:return
        run_transaction(lambda db:self._store(db,payload),self.factory);self.state=payload
    @staticmethod
    def _store(db,payload):
        row=db.get(ArchiveState,1)
        if row:row.payload=copy.deepcopy(payload)
        else:db.add(ArchiveState(id=1,payload=copy.deepcopy(payload)))
    def _usage(self):
        sample=self.sample_provider() or {};storage=sample.get('storage') or {}
        physical=storage.get('application_bytes')
        if physical is None:raise ArchiveError('Persistent storage measurement is unavailable; cleanup paused.')
        with self.factory() as db:
            if db.get_bind().dialect.name!='sqlite':raise ArchiveError('This archival policy requires the verified SQLite deployment.')
            connection=db.connection()
            reusable=int(connection.exec_driver_sql('PRAGMA freelist_count').scalar())*int(connection.exec_driver_sql('PRAGMA page_size').scalar())
        return int(physical),min(int(physical),reusable)
    def _review(self,day):
        with self.factory() as db:return review_day(db,day,self.now())
    def _verify_google(self,plan,token):
        client=self.sheets.drive_factory(token);proof=[]
        try:
            for spec in plan['specs']:
                with self.factory() as db:
                    item=artifact(db,plan['trading_date'],spec['kind'],spec['part_number'])
                    record=db.get(DailyCloudCopy,file_key(f"daily:{plan['trading_date']}",item))
                    if not record or record.status!='verified' or not record.verified_at:raise ArchiveError('Every required daily file must be verified in Google Drive.')
                    identifier,parent=record.drive_id,(record.metadata_json or {}).get('parent_id')
                if not parent:raise ArchiveError('Google backup folder evidence is incomplete.')
                remote=client.metadata(identifier)
                if not remote or remote.get('appProperties',{}).get('pot_backup_key')!=file_key(f"daily:{plan['trading_date']}",item):
                    raise ArchiveError('Google backup identity changed; source retained.')
                proof.append(verify_remote(remote,item,parent,client))
            return proof
        finally:client.close()
    def _assert_copy_evidence(self,db,plan,proof):
        required={(a['kind'],a['sha256'],a['size']) for a in plan['specs']}
        verified={(a.get('kind'),a.get('sha256'),a.get('size')) for a in proof if a.get('drive_id') and a.get('verified_at') and a.get('parent_id')}
        if required!=verified:raise ArchiveError('Complete verified Google backup evidence is required before pruning.')
        by_spec={(a['kind'],a['sha256'],a['size']):a for a in proof}
        for spec in plan['specs']:
            # Match the freshly read remote identity to the current account's
            # durable copy. A stale proof cannot authorize a trashed copy.
            row=db.get(DailyCloudCopy,file_key(f"daily:{plan['trading_date']}",spec))
            evidence=by_spec[(spec['kind'],spec['sha256'],spec['size'])]
            if not row or row.status!='verified' or not row.verified_at or row.drive_id!=evidence['drive_id'] or (row.metadata_json or {}).get('parent_id')!=evidence['parent_id']:
                raise ArchiveError('Google backup status changed; source retained.')

    def _headroom(self,db,rows):
        # DELETE-journal mode needs rollback pages even when deleting records.
        # Bound both the row count and transaction payload, and reserve a
        # conservative allowance for B-tree/index pages plus 64 MiB overhead.
        payload=sum(int(row[2] or 0) for row in rows)
        needed=64*1024**2+3*payload+len(rows)*64*1024
        path=Path(db.get_bind().url.database).resolve().parent
        available=shutil.disk_usage(path).free
        sample=self.sample_provider() or {};storage=sample.get('storage') or {}
        physical=storage.get('application_bytes')
        if physical is None:raise ArchiveError('Persistent storage measurement is unavailable; cleanup paused.')
        plan_limit=int(storage.get('plan_limit_bytes') or int(os.getenv('PERSISTENT_STORAGE_LIMIT_GB','15'))*1024**3)
        available=min(available,max(0,plan_limit-int(physical)))
        if available<needed:raise ArchiveError('Insufficient rollback-journal headroom; source retained. Download verified archives or add capacity before cleanup.')

    def _prune(self,plan,proof,writer_gate=None):
        # One call commits no more than 200 rows. A receipt and the deletes
        # share the transaction, so restart recovery never guesses a cursor.
        with getattr(self.cloud,'operation_lock',nullcontext()):
            return self._prune_locked(plan,proof,writer_gate)

    def _prune_locked(self,plan,proof,writer_gate=None):
        if not self.can_prune():raise ArchiveError('Recording resumed; cleanup waits for market close.')
        with self.factory() as db:self._assert_copy_evidence(db,plan,proof)
        # Keep account switches/cloud cleanup serialized across fresh remote
        # proof and local deletion, without holding the capture lock for HTTP.
        proof=self._verify_google(plan,self.cloud.access_token())
        if not self.can_prune():raise ArchiveError('Recording resumed; cleanup waits for market close.')
        if not self.lease.renew():raise ArchiveError('Archival ownership changed; no records removed.')
        def operation(db):
            if writer_gate and writer_gate.cancelled.is_set():raise ArchiveError('Archival cancelled; source retained.')
            if not self.can_prune():raise ArchiveError('Recording resumed; cleanup waits for market close.')
            if not guard_lease(db,self.lease.token):raise ArchiveError('Archival ownership expired; no records removed.')
            fresh=review_day(db,plan['trading_date'],self.now())
            if fresh.get('already_pruned'):return fresh['deleted_count']
            if any(fresh[k]!=plan[k] for k in ('manifest_checksum','snapshot_count','record_count','anchors','account_key')):
                raise ArchiveError('Source changed after backup verification; cleanup paused.')
            self._assert_copy_evidence(db,fresh,proof)
            rows=[];start,_=bounds(plan['trading_date'])
            for trade_id,last_second in sorted(fresh['anchors'].items()):
                rows.extend(db.execute(select(PnlSnapshot.trade_id,PnlSnapshot.snapshot_second,func.length(cast(PnlSnapshot.data,Text))).where(
                    PnlSnapshot.trade_id==trade_id,PnlSnapshot.snapshot_second>=start,PnlSnapshot.snapshot_second<last_second)
                    .order_by(PnlSnapshot.snapshot_second).limit(200-len(rows))).all())
                if len(rows)>=200:break
            self._headroom(db,rows)
            if not self.can_prune():raise ArchiveError('Recording resumed; cleanup waits for market close.')
            if writer_gate and writer_gate.cancelled.is_set():raise ArchiveError('Archival cancelled; source retained.')
            removed=0
            if rows:
                removed=db.execute(delete(PnlSnapshot).where(tuple_(PnlSnapshot.trade_id,PnlSnapshot.snapshot_second).in_([(r[0],r[1]) for r in rows]))).rowcount
                if removed!=len(rows):raise ArchiveError('Snapshot cleanup count changed; transaction rolled back.')
            receipt=db.get(ArchiveReceipt,plan['trading_date'])
            if not receipt:
                receipt=ArchiveReceipt(trading_date=plan['trading_date'],manifest_checksum=fresh['manifest_checksum'],snapshot_count=fresh['snapshot_count'],
                    record_count=fresh['record_count'],deleted_count=0,retained_count=len(fresh['anchors']),status='pruning',evidence={})
                db.add(receipt)
            receipt.deleted_count+=removed
            remaining=fresh['remaining_count']-removed
            if remaining+receipt.deleted_count!=receipt.snapshot_count or remaining<receipt.retained_count:
                raise ArchiveError('Archival recovery count changed; transaction rolled back.')
            receipt.status='completed' if remaining==receipt.retained_count else 'pruning'
            receipt.pruned_at=iso_now()
            receipt.evidence={'google_files':proof,'anchors':fresh['anchors'],'account_key':fresh['account_key']}
            return receipt.deleted_count
        with writer_gate or nullcontext():
            return run_transaction(operation,self.factory)
    async def _prune_batch(self,plan,proof):
        gate=_AsyncWriterGate(asyncio.get_running_loop(),self.writer_lock)
        worker=asyncio.create_task(asyncio.to_thread(self._prune,plan,proof,gate))
        try:return await asyncio.shield(worker)
        except asyncio.CancelledError:
            gate.cancelled.set()
            # A thread cannot be safely killed. Complete/roll back its bounded
            # transaction and release its lock before shutdown continues.
            while not worker.done():
                try:await asyncio.shield(worker)
                except asyncio.CancelledError:continue
                except Exception:break
            if not worker.cancelled():worker.exception()
            raise
    def prepare_once(self):
        if not self.config.enabled:return None
        with getattr(self.cloud,'operation_lock',nullcontext()):
            return self._prepare_locked()
    def _prepare_locked(self):
        if not self.config.enabled:return None
        if not self._loaded:
            with self.factory() as db:
                row=db.get(ArchiveState,1);self.state=copy.deepcopy(row.payload) if row else {}
            self._loaded=True
        if not self.config.enabled:return None
        if self.state.get('next_attempt',0)>time.time():return None
        physical,reusable=self._usage();effective=physical-reusable
        with self.factory() as db:
            unfinished_day=db.scalar(select(ArchiveReceipt.trading_date).where(ArchiveReceipt.status=='pruning').order_by(ArchiveReceipt.trading_date).limit(1))
        threshold=self.config.target_bytes if self.state.get('archiving') else self.config.threshold_bytes
        if effective<threshold and not unfinished_day:
            self._save('ready','Below the 12 GB trigger. No history removed.',reusable_bytes=reusable,effective_used_bytes=effective,next_attempt=0,archiving=False)
            return None
        if not self.lease.renew():return None
        self.cloud.config.validate()
        with self.factory() as db:
            credential=db.get(CloudDriveCredential,1)
            if not credential or credential.auth_required:raise ArchiveError('Connect Google Drive before automatic cleanup can run.')
            day=unfinished_day or db.scalar(select(DailyReport.trading_date).where(DailyReport.status=='finalized',DailyReport.snapshot_count>0,
                DailyReport.trading_date<self.now().astimezone(IST).date().isoformat(),
                ~select(ArchiveReceipt.trading_date).where(ArchiveReceipt.trading_date==DailyReport.trading_date,ArchiveReceipt.status=='completed').exists()).order_by(DailyReport.trading_date).limit(1))
        if not day:
            self.daily.notify_work()
            self._save('waiting_for_exports','No eligible completed daily export yet. Active state and all unverified records are retained.',next_attempt=time.time()+300,reusable_bytes=reusable)
            return None
        self._save('preparing','The storage trigger was reached; verifying eligible daily backups.',archiving=True)
        token=self.cloud.access_token()
        uploaded=self.sheets.upload_daily_artifacts(token,day,max_files=1)
        if uploaded:
            self._save('uploading','Uploading daily archive parts; no source records removed yet.',last_run_at=iso_now(),next_attempt=0)
            return None
        plan=self._review(day)
        if plan.get('already_pruned'):return None
        proof=self._verify_google(plan,token)
        return plan,proof
    def failure(self,error):
        attempts=int(self.state.get('attempts',0))+1
        message=str(error) if isinstance(error,BackupError) else 'Automatic archival paused; all source records retained.'
        self._save('blocked',message[:240],attempts=attempts,next_attempt=time.time()+min(1800,30*2**min(attempts,6)),last_run_at=iso_now())
    async def cycle(self):
        try:
            prepared=await asyncio.to_thread(self.prepare_once)
            if prepared:
                if not self.can_prune():
                    await asyncio.to_thread(self._save,'waiting_for_market_close','Backups are verified. Large cleanup waits until recording is inactive to protect one-second capture.',next_attempt=time.time()+300)
                    return
                started=time.monotonic();removed=0;completed=False
                # Yield the async writer lock between small commits, even when
                # recording is inactive. Never hold a day-sized transaction.
                for _ in range(20):
                    if not self.can_prune():break
                    removed=await self._prune_batch(*prepared)
                    with self.factory() as db:
                        receipt=db.get(ArchiveReceipt,prepared[0]['trading_date'])
                        completed=bool(receipt and receipt.status=='completed')
                    if completed or time.monotonic()-started>=3:break
                    await asyncio.sleep(0)
                physical,reusable=await asyncio.to_thread(self._usage)
                await asyncio.to_thread(self._save,'completed' if completed else 'pruning',
                    'Verified older snapshots removed. SQLite space is reusable; essential state and exports are retained.' if completed else 'Verified history cleanup is progressing in bounded batches; restart recovery is durable.',
                    last_success_at=iso_now(),last_pruned_snapshots=removed,reusable_bytes=reusable,effective_used_bytes=physical-reusable,attempts=0,next_attempt=0)
        except Exception as error:
            try:await asyncio.to_thread(self.failure,error)
            except Exception:self.state={**self.state,'status':'blocked','message':'Database unavailable; automatic cleanup paused and records retained.','next_attempt':time.time()+60}
    async def run(self,stop_event):
        self._loop,self._wake=asyncio.get_running_loop(),asyncio.Event()
        try:
            while not stop_event.is_set():
                if self.ready_provider():await self.cycle()
                try:await asyncio.wait_for(self._wake.wait(),timeout=self.config.interval_seconds)
                except asyncio.TimeoutError:pass
                self._wake.clear()
        finally:
            self._loop,self._wake=None,None
            if self.lease.token:await asyncio.to_thread(self.lease.release)
