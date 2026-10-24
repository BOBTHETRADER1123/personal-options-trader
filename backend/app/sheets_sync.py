"""Durable Google Sheets batches. Absolute ranges make interrupted writes replayable.

SQLite remains authoritative. No source trade or snapshot is deleted here.
"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import Boolean,Float,Integer,JSON,String,func,select
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base,SessionLocal,iso_now,run_transaction
from .models import PnlSnapshot,Trade
from .scheduler import CollectorLease
from .cloud_backup import AccessTokenExpired,BackupError,CloudBackupService,CloudDriveCredential,DRIVE,DriveClient,file_key,verify_remote

IST=ZoneInfo('Asia/Kolkata')
SHEETS='https://sheets.googleapis.com/v4/spreadsheets'
SHEET_MIME='application/vnd.google-apps.spreadsheet'
HEADERS=['Snapshot UTC','Snapshot IST','Broker','Trade ID','Strategy ID','Strategy name','Underlying','Exchange','Segment',
 'Contract ID','Instrument type','Strike','Expiry','Buy / sell','Lots','Open lots','Lot size','Open quantity','Monetary multiplier',
 'Entry UTC','Entry price','Exit UTC','Exit price','Latest price','Quote received UTC','Exchange quote UTC','Quote age seconds',
 'Freshness','Reused quote','Bid','Ask','Leg unrealized P&L','Leg realized P&L','Leg gross P&L','Leg net P&L','Leg charges',
 'Strategy unrealized P&L','Strategy realized P&L','Strategy gross P&L','Estimated charges','Strategy net P&L',
 'Maximum observed profit','Maximum observed loss','Maximum theoretical profit','Maximum theoretical loss','Breakeven levels',
 'Estimated margin','Estimated hedge benefit','Net premium','Close reason','Snapshot row key']
COLS=len(HEADERS)

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def day_of(second):return datetime.fromtimestamp(second,timezone.utc).astimezone(IST).date().isoformat()
def day_bounds(day):
    start=datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=IST)
    return int(start.timestamp()),int((start+timedelta(days=1)).timestamp())
def column_name(number):
    value=''
    while number:number,part=divmod(number-1,26);value=chr(65+part)+value
    return value

class SheetsDocument(Base):
    __tablename__='sheets_documents'
    trading_date:Mapped[str]=mapped_column(String(10),primary_key=True)
    part:Mapped[int]=mapped_column(Integer,primary_key=True)
    drive_id:Mapped[str|None]=mapped_column(String(150),nullable=True)
    lookup_key:Mapped[str]=mapped_column(String(64),unique=True)
    status:Mapped[str]=mapped_column(String(24),default='pending')
    allocated_cells:Mapped[int]=mapped_column(Integer,default=800)
    duplicate_file_ids:Mapped[list]=mapped_column(JSON,default=list)
    updated_at:Mapped[str]=mapped_column(String(40),default=iso_now)

class SheetsTab(Base):
    __tablename__='sheets_tabs'
    trading_date:Mapped[str]=mapped_column(String(10),primary_key=True)
    trade_id:Mapped[str]=mapped_column(String(80),primary_key=True)
    continuation:Mapped[int]=mapped_column(Integer,primary_key=True)
    document_part:Mapped[int]=mapped_column(Integer)
    sheet_id:Mapped[int]=mapped_column(Integer)
    title:Mapped[str]=mapped_column(String(100))
    next_row:Mapped[int]=mapped_column(Integer,default=2)
    allocated_rows:Mapped[int]=mapped_column(Integer,default=2)

class SheetsProgress(Base):
    __tablename__='sheets_progress'
    trade_id:Mapped[str]=mapped_column(String(80),primary_key=True)
    last_second:Mapped[int]=mapped_column(Integer,default=0)
    acknowledged_rows:Mapped[int]=mapped_column(Integer,default=0)
    updated_at:Mapped[str]=mapped_column(String(40),default=iso_now)

class SheetsBatch(Base):
    __tablename__='sheets_batches'
    id:Mapped[str]=mapped_column(String(40),primary_key=True)
    trading_date:Mapped[str]=mapped_column(String(10),index=True)
    document_part:Mapped[int]=mapped_column(Integer)
    status:Mapped[str]=mapped_column(String(24),default='planned',index=True)
    payload:Mapped[dict]=mapped_column(JSON)
    checksum:Mapped[str]=mapped_column(String(64))
    snapshot_count:Mapped[int]=mapped_column(Integer)
    record_count:Mapped[int]=mapped_column(Integer)
    attempts:Mapped[int]=mapped_column(Integer,default=0)
    next_attempt:Mapped[float]=mapped_column(Float,default=0)
    error:Mapped[str|None]=mapped_column(String(240),nullable=True)
    created_at:Mapped[str]=mapped_column(String(40),default=iso_now)
    verified_at:Mapped[str|None]=mapped_column(String(40),nullable=True)

class DailyCloudCopy(Base):
    __tablename__='daily_cloud_copies'
    key:Mapped[str]=mapped_column(String(64),primary_key=True)
    trading_date:Mapped[str]=mapped_column(String(10),index=True)
    kind:Mapped[str]=mapped_column(String(20))
    part:Mapped[int]=mapped_column(Integer)
    drive_id:Mapped[str]=mapped_column(String(150))
    source_sha256:Mapped[str]=mapped_column(String(64))
    metadata_json:Mapped[dict]=mapped_column(JSON,default=dict)
    status:Mapped[str]=mapped_column(String(24),default='reserved')
    verified_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    trashed_at:Mapped[str|None]=mapped_column(String(40),nullable=True)

class SheetsServiceState(Base):
    __tablename__='sheets_service_state'
    id:Mapped[int]=mapped_column(Integer,primary_key=True,default=1)
    next_attempt:Mapped[float]=mapped_column(Float,default=0)
    attempts:Mapped[int]=mapped_column(Integer,default=0)
    error:Mapped[str|None]=mapped_column(String(240),nullable=True)
    storage_full:Mapped[bool]=mapped_column(Boolean,default=False)
    quota:Mapped[dict]=mapped_column(JSON,default=dict)
    last_success:Mapped[str|None]=mapped_column(String(40),nullable=True)

class SheetsError(BackupError):pass
class StorageFull(SheetsError):pass

@dataclass(frozen=True)
class SheetsConfig:
    enabled:bool=False
    interval_seconds:int=5
    max_snapshots:int=300
    max_payload_bytes:int=1_500_000
    max_cells:int=9_000_000
    max_rows_per_tab:int=200_000
    max_batches_per_cycle:int=3
    row_block:int=1000
    @classmethod
    def from_env(cls):
        return cls(enabled=os.getenv('GOOGLE_SHEETS_SYNC_ENABLED','false').lower()=='true',
            interval_seconds=max(5,int(os.getenv('GOOGLE_SHEETS_SYNC_SECONDS','5'))),
            max_snapshots=max(1,min(2000,int(os.getenv('GOOGLE_SHEETS_BATCH_SNAPSHOTS','300')))),
            max_cells=max(COLS*25+800,min(9_500_000,int(os.getenv('GOOGLE_SHEETS_MAX_CELLS','9000000')))))

def _cell(value):
    if value is None or isinstance(value,float) and not math.isfinite(value):return ''
    return canonical(value) if isinstance(value,(dict,list)) else value

def snapshot_rows(snapshot,trade):
    data=snapshot.data;rows=[];original={str(l.get('id')):l for l in trade.get('legs',[])};risk=data.get('risk',trade.get('risk',{}))
    for index,leg in enumerate(data.get('legs') or [{}]):
        source=original.get(str(leg.get('id')),{});key=f"{snapshot.trade_id}:{snapshot.snapshot_second}:{leg.get('id',index)}"
        row=[data.get('timestamp_utc',snapshot.timestamp),data.get('timestamp_ist'),data.get('broker',trade.get('broker')),
            snapshot.trade_id,data.get('strategy_id',trade.get('strategy_id')),trade.get('name'),leg.get('underlying',data.get('underlying')),
            leg.get('exchange'),leg.get('segment'),leg.get('instrument_id'),leg.get('option_type'),leg.get('strike'),leg.get('expiry'),
            leg.get('side'),leg.get('lots'),leg.get('open_lots'),leg.get('lot_size'),(leg.get('open_lots') or 0)*(leg.get('lot_size') or 0),
            leg.get('price_multiplier'),source.get('entry_at'),leg.get('entry_price'),source.get('exit_at'),source.get('exit_price'),
            leg.get('latest_price',leg.get('ltp')),leg.get('quote_at'),leg.get('exchange_quote_at'),leg.get('quote_age_seconds'),
            leg.get('data_freshness'),leg.get('quote_reused'),leg.get('bid'),leg.get('ask'),leg.get('unrealized_pnl'),leg.get('realized_pnl'),
            leg.get('gross_pnl'),leg.get('net_pnl'),leg.get('charges'),data.get('unrealized_pnl'),data.get('realized_pnl'),data.get('gross_pnl'),
            data.get('charges'),data.get('net_pnl'),data.get('mfe',trade.get('mfe')),data.get('mae',trade.get('mae')),
            'Unlimited' if risk.get('profit_unlimited') else risk.get('max_profit'),
            'Unlimited' if risk.get('loss_unlimited') else risk.get('max_loss'),risk.get('breakevens'),data.get('margin_estimate',trade.get('margin_estimate')),
            data.get('hedge_benefit',trade.get('hedge_benefit')),data.get('net_premium',trade.get('net_premium')),trade.get('close_reason'),key]
        rows.append([_cell(v) for v in row])
    return rows

class SheetsClient:
    def __init__(self,token,client=None):
        self.client=client or httpx.Client(timeout=httpx.Timeout(40,connect=10),follow_redirects=False)
        self.own_client=client is None;self.headers={'Authorization':f'Bearer {token}'}
    def close(self):
        if self.own_client:self.client.close()
    def request(self,method,url,**kwargs):
        try:response=self.client.request(method,url,headers=self.headers,**kwargs)
        except httpx.HTTPError:raise SheetsError('Google connection failed; local records remain queued.') from None
        if response.status_code==401:raise AccessTokenExpired('Google authorization needs refresh; records remain queued.')
        if response.status_code in (404,409):return response
        if not 200<=response.status_code<300:
            try:reasons=[r.get('reason') for r in response.json().get('error',{}).get('errors',[])]
            except (ValueError,AttributeError,TypeError):reasons=[]
            if 'storageQuotaExceeded' in reasons:raise StorageFull('Google Drive is full. Download and clean verified copies; local records are retained.')
            raise SheetsError(f'Google request failed (HTTP {response.status_code}); local records remain queued.')
        return response
    def quota(self):return self.request('GET',f'{DRIVE}/about',params={'fields':'storageQuota,user(emailAddress,displayName)'}).json()
    def ensure_document(self,document):
        key=document['lookup_key'];query=f"trashed=false and appProperties has {{ key='pot_sheets_key' and value='{key}' }}"
        found=self.request('GET',f'{DRIVE}/files',params={'q':query,'spaces':'drive','pageSize':100,
            'fields':'files(id,name,mimeType,trashed,createdTime,appProperties)'}).json().get('files',[])
        found=[f for f in found if f.get('mimeType')==SHEET_MIME and not f.get('trashed')]
        if found:
            found.sort(key=lambda r:(r.get('createdTime',''),r['id']))
            return found[0]['id'],[r['id'] for r in found[1:]]
        suffix='' if document['part']==1 else f"_Part_{document['part']:03d}"
        value=self.request('POST',f'{DRIVE}/files',params={'fields':'id,mimeType,appProperties'},json={
            'name':f"Paper_Trading_{document['trading_date']}{suffix}",'mimeType':SHEET_MIME,'appProperties':{'pot_sheets_key':key}}).json()
        if not value.get('id') or value.get('mimeType')!=SHEET_MIME:raise SheetsError('Spreadsheet creation could not be verified.')
        return value['id'],[]
    def prepare_tabs(self,file_id,tabs):
        response=self.request('GET',f'{SHEETS}/{file_id}',params={'fields':'spreadsheetId,sheets(properties)'})
        if response.status_code==404:raise SheetsError('Current spreadsheet is missing. Restore it before resuming synchronization.')
        existing={s['properties']['sheetId']:s['properties'] for s in response.json().get('sheets',[])};requests=[]
        if not any(p.get('title')=='Daily_Summary' for p in existing.values()):
            if len(existing)==1 and next(iter(existing.values())).get('title') in ('Sheet1','Sheet 1'):
                identifier=next(iter(existing));requests.append({'updateSheetProperties':{'properties':{'sheetId':identifier,'title':'Daily_Summary',
                    'gridProperties':{'rowCount':100,'columnCount':8}},'fields':'title,gridProperties.rowCount,gridProperties.columnCount'}})
            else:requests.append({'addSheet':{'properties':{'sheetId':0,'title':'Daily_Summary','gridProperties':{'rowCount':100,'columnCount':8}}}})
        for tab in tabs:
            current=existing.get(tab['sheet_id'])
            if current and current.get('title')!=tab['title']:raise SheetsError('Worksheet identifier conflicts; local records retained.')
            props={'sheetId':tab['sheet_id'],'title':tab['title'],'gridProperties':{'rowCount':tab['allocated_rows'],'columnCount':COLS,'frozenRowCount':1}}
            if not current:requests.append({'addSheet':{'properties':props}})
            elif current.get('gridProperties',{}).get('rowCount',0)<tab['allocated_rows']:
                requests.append({'updateSheetProperties':{'properties':props,'fields':'gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount'}})
        if requests:
            result=self.request('POST',f'{SHEETS}/{file_id}:batchUpdate',json={'requests':requests})
            if result.status_code==409:raise SheetsError('Worksheet creation conflicted; the same planned ranges will be retried.')
    def write_verified(self,file_id,ranges):
        value=self.request('POST',f'{SHEETS}/{file_id}/values:batchUpdate',json={'valueInputOption':'RAW','data':ranges,
            'includeValuesInResponse':True,'responseValueRenderOption':'UNFORMATTED_VALUE'}).json()
        results=value.get('responses',[])
        if len(results)!=len(ranges):raise SheetsError('Google did not verify every range; unchanged batch retained.')
        for expected,result in zip(ranges,results):
            width=max(map(len,expected['values']),default=0)
            pad=lambda rows:[list(row)+['']*(width-len(row)) for row in rows]
            if pad(result.get('updatedData',{}).get('values',[]))!=pad(expected['values']):raise SheetsError('Google readback differs from the planned batch; records remain queued.')
        return True
    def trash_owned(self,file_id,key_name,key):
        response=self.request('GET',f'{DRIVE}/files/{file_id}',params={'fields':'id,trashed,appProperties'})
        if response.status_code==404:raise SheetsError('Cloud copy is missing; cleanup evidence needs review.')
        value=response.json()
        if value.get('appProperties',{}).get(key_name)!=key:raise SheetsError('Cloud ownership does not match; no deletion performed.')
        if not value.get('trashed'):self.request('PATCH',f'{DRIVE}/files/{file_id}',json={'trashed':True},params={'fields':'id,trashed'})
        verified=self.request('GET',f'{DRIVE}/files/{file_id}',params={'fields':'id,trashed'}).json()
        if verified.get('id')!=file_id or not verified.get('trashed'):raise SheetsError('Google Trash operation could not be verified.')

def _document(row):return {k:getattr(row,k) for k in ('trading_date','part','drive_id','lookup_key')}

class SheetsSyncService:
    def __init__(self,session_factory=SessionLocal,cloud_service=None,config=None,client_factory=SheetsClient,drive_factory=DriveClient):
        self.factory=session_factory;self.cloud=cloud_service or CloudBackupService(session_factory=session_factory)
        self.config=config or SheetsConfig.from_env();self.client_factory=client_factory;self.drive_factory=drive_factory
        self.lease=CollectorLease(session_factory,name='google_sheets_sync',ttl_seconds=120)
        self._wake=None;self._loop=None;self._quota_at=0;self._next_attempt=0;self._lock=threading.Lock()
    def notify_work(self):
        with self._lock:loop,wake=self._loop,self._wake
        if loop and wake and not loop.is_closed():loop.call_soon_threadsafe(wake.set)
    def _tab(self,db,date,trade,required):
        tabs=list(db.scalars(select(SheetsTab).where(SheetsTab.trading_date==date,SheetsTab.trade_id==trade['id']).order_by(SheetsTab.continuation.desc())))
        tab=tabs[0] if tabs else None;document=db.get(SheetsDocument,(date,tab.document_part)) if tab else None
        block=max(required,self.config.row_block)
        if tab and tab.next_row+required-1<=self.config.max_rows_per_tab:
            needed=max(0,tab.next_row+required-1-tab.allocated_rows)
            growth=min(self.config.max_rows_per_tab-tab.allocated_rows,math.ceil(needed/block)*block) if needed else 0
            if document.allocated_cells+growth*COLS<=self.config.max_cells:
                tab.allocated_rows+=growth;document.allocated_cells+=growth*COLS;return document,tab
        document=db.scalar(select(SheetsDocument).where(SheetsDocument.trading_date==date).order_by(SheetsDocument.part.desc()).limit(1))
        reserve=min(self.config.max_rows_per_tab,max(1+required,self.config.row_block))
        if reserve*COLS+800>self.config.max_cells:reserve=required+1
        if reserve*COLS+800>self.config.max_cells:raise SheetsError('One snapshot exceeds spreadsheet limits; source retained.')
        if not document or document.allocated_cells+reserve*COLS>self.config.max_cells or document.status=='trashed':
            part=document.part+1 if document else 1;document=SheetsDocument(trading_date=date,part=part,
                lookup_key=hashlib.sha256(f'personal-options-trader:daily:{date}:{part}'.encode()).hexdigest(),status='pending',allocated_cells=800)
            db.add(document)
        continuation=tabs[0].continuation+1 if tabs else 1;suffix='Kotak' if trade.get('broker')=='kotak' else '5paisa'
        token=hashlib.sha256(trade['id'].encode()).hexdigest()[:10];title=f'Trade_{token}_{suffix}'+(f'_C{continuation}' if continuation>1 else '')
        sheet_id=int(hashlib.sha256(f'{trade["id"]}:{continuation}'.encode()).hexdigest()[:7],16)+1
        tab=SheetsTab(trading_date=date,trade_id=trade['id'],continuation=continuation,document_part=document.part,
            sheet_id=sheet_id,title=title,next_row=2,allocated_rows=reserve);db.add(tab);document.allocated_cells+=reserve*COLS;db.flush()
        return document,tab
    def plan(self):
        def operation(db):
            if db.scalar(select(SheetsBatch.id).where(SheetsBatch.status!='verified').limit(1)):return 0
            source=list(db.scalars(select(PnlSnapshot).outerjoin(SheetsProgress,SheetsProgress.trade_id==PnlSnapshot.trade_id).where(
                PnlSnapshot.snapshot_second>func.coalesce(SheetsProgress.last_second,0)).order_by(PnlSnapshot.snapshot_second,PnlSnapshot.trade_id).limit(self.config.max_snapshots)))
            groups={};size=0
            for snapshot in source:
                stored=db.get(Trade,snapshot.trade_id)
                if not stored:raise SheetsError('A recorded trade summary is missing; history retained for recovery.')
                trade={**stored.data,'id':stored.id,'broker':stored.broker};rows=snapshot_rows(snapshot,trade);cost=len(canonical(rows).encode())
                if cost>self.config.max_payload_bytes:raise SheetsError('One snapshot exceeds upload limit; history retained.')
                if size and size+cost>self.config.max_payload_bytes:break
                date=day_of(snapshot.snapshot_second);document,tab=self._tab(db,date,trade,len(rows));key=(date,document.part)
                group=groups.setdefault(key,{'ranges':{},'tabs':{},'progress':{},'snapshot_count':0,'record_count':0})
                tkey=(tab.trade_id,tab.continuation)
                if tkey not in group['ranges']:group['ranges'][tkey]={'title':tab.title,'start':tab.next_row,'values':[]}
                group['ranges'][tkey]['values'].extend(rows);tab.next_row+=len(rows)
                group['tabs'][tkey]={k:getattr(tab,k) for k in ('sheet_id','title','allocated_rows')}
                entry=group['progress'].setdefault(snapshot.trade_id,{'last_second':0,'rows':0})
                entry['last_second']=max(entry['last_second'],snapshot.snapshot_second);entry['rows']+=len(rows)
                group['snapshot_count']+=1;group['record_count']+=len(rows);size+=cost
            for (date,part),group in groups.items():
                ranges=[]
                for entry in group['ranges'].values():
                    title=entry['title'].replace("'","''")
                    ranges.extend([{'range':f"'{title}'!A1:{column_name(COLS)}1",'majorDimension':'ROWS','values':[HEADERS]},
                        {'range':f"'{title}'!A{entry['start']}:{column_name(COLS)}{entry['start']+len(entry['values'])-1}",'majorDimension':'ROWS','values':entry['values']}])
                summary=[['Trading date',date],['Spreadsheet part',part],['Source of truth','Durable application database'],
                    ['Sampling','One second; original quote timestamps retained'],['Write policy','Absolute verified ranges; retries overwrite'],
                    ['Leg rows in latest batch',group['record_count']],['Updated UTC',iso_now()]]
                ranges.append({'range':"'Daily_Summary'!A1:B7",'majorDimension':'ROWS','values':summary})
                payload={'ranges':ranges,'tabs':list(group['tabs'].values()),'progress':group['progress']}
                db.add(SheetsBatch(id=uuid.uuid4().hex,trading_date=date,document_part=part,status='planned',payload=payload,
                    checksum=hashlib.sha256(canonical(payload).encode()).hexdigest(),snapshot_count=group['snapshot_count'],record_count=group['record_count']))
            return sum(g['snapshot_count'] for g in groups.values())
        return run_transaction(operation,self.factory)
    def _execute(self,client,batch_id):
        with self.factory() as db:
            batch=db.get(SheetsBatch,batch_id)
            if not batch or batch.status=='verified':return 0
            payload=copy.deepcopy(batch.payload)
            if hashlib.sha256(canonical(payload).encode()).hexdigest()!=batch.checksum:raise SheetsError('Queued upload failed local integrity check; snapshots retained.')
            info=_document(db.get(SheetsDocument,(batch.trading_date,batch.document_part)))
        if not info['drive_id']:
            file_id,duplicates=client.ensure_document(info)
            def save(db):
                row=db.get(SheetsDocument,(info['trading_date'],info['part']))
                if row.drive_id and row.drive_id!=file_id:raise SheetsError('Concurrent document creation needs review; no rows acknowledged.')
                row.drive_id,row.duplicate_file_ids,row.status=file_id,duplicates,'ready';row.updated_at=iso_now()
            run_transaction(save,self.factory);info['drive_id']=file_id
        client.prepare_tabs(info['drive_id'],payload['tabs']);client.write_verified(info['drive_id'],payload['ranges'])
        def acknowledge(db):
            batch=db.get(SheetsBatch,batch_id)
            if batch.status=='verified':return 0
            for trade_id,update in payload['progress'].items():
                cursor=db.get(SheetsProgress,trade_id)
                if not cursor:cursor=SheetsProgress(trade_id=trade_id,last_second=0,acknowledged_rows=0);db.add(cursor)
                cursor.last_second=max(cursor.last_second,update['last_second']);cursor.acknowledged_rows+=update['rows'];cursor.updated_at=iso_now()
            batch.status,batch.verified_at,batch.error='verified',iso_now(),None;count=batch.record_count;batch.payload={}
            state=db.get(SheetsServiceState,1)
            if not state:state=SheetsServiceState(id=1);db.add(state)
            state.attempts,state.error,state.storage_full,state.next_attempt=0,None,False,0;state.last_success=iso_now()
            return count
        return run_transaction(acknowledge,self.factory)
    def _failure(self,error):
        def save(db):
            state=db.get(SheetsServiceState,1)
            if not state:state=SheetsServiceState(id=1,attempts=0,quota={});db.add(state)
            state.attempts+=1;state.error=str(error)[:240] if isinstance(error,BackupError) else 'Google synchronization failed; local records retained.'
            state.storage_full=isinstance(error,StorageFull);delay=900 if state.storage_full else min(900,5*2**min(state.attempts,8));state.next_attempt=time.time()+delay
            self._next_attempt=state.next_attempt
            batch=db.scalar(select(SheetsBatch).where(SheetsBatch.status!='verified').order_by(SheetsBatch.created_at,SheetsBatch.document_part).limit(1))
            if batch:batch.attempts+=1;batch.error,batch.next_attempt=state.error,state.next_attempt
        run_transaction(save,self.factory)
    def _pending_daily_date(self,db):
        from .daily_reports import DailyReport,DailyReportPart
        return db.scalar(select(DailyReportPart.trading_date).join(DailyReport,DailyReport.trading_date==DailyReportPart.trading_date).where(
            DailyReport.status=='finalized',~select(DailyCloudCopy.key).where(DailyCloudCopy.trading_date==DailyReportPart.trading_date,
                DailyCloudCopy.source_sha256==DailyReportPart.sha256,DailyCloudCopy.status.in_(['verified','trashed'])).exists()
        ).order_by(DailyReportPart.trading_date,DailyReportPart.part_number).limit(1))
    def upload_daily_artifacts(self,token,date,max_files=1):
        """Reserve IDs before media upload; verify hashes before acknowledging."""
        from .daily_reports import cloud_artifacts
        client=self.drive_factory(token);uploaded=0
        try:
            date_value=datetime.strptime(date,'%Y-%m-%d');parent=None;path=''
            for name in ('Paper Trading Records',str(date_value.year),date_value.strftime('%B'),'Daily Reports'):
                path+='/'+name;parent=self.cloud._folder(client,path,name,parent)
            with self.factory() as db:
                for artifact in cloud_artifacts(db,date):
                    key=file_key(f'daily:{date}',artifact)
                    with self.factory() as state_db:
                        copy_row=state_db.get(DailyCloudCopy,key)
                        if copy_row and copy_row.status in ('verified','trashed'):continue
                        identifier=copy_row.drive_id if copy_row else None
                    if artifact['size']>self.cloud.config.max_file_bytes:raise SheetsError('Daily export exceeds upload file limit; local export remains available. Split it before cloud upload.')
                    if not identifier:
                        found=client.find(parent,key)
                        if len(found)>1:raise SheetsError('Duplicate daily archives require review before cleanup; local records retained.')
                        identifier=found[0]['id'] if found else client.generate_id()
                        def reserve(state_db):
                            row=state_db.get(DailyCloudCopy,key)
                            if not row:state_db.add(DailyCloudCopy(key=key,trading_date=date,kind=artifact['kind'],
                                part=artifact.get('part_number',1),drive_id=identifier,source_sha256=artifact['sha256'],status='reserved',metadata_json={}))
                        run_transaction(reserve,self.factory)
                        with self.factory() as state_db:identifier=state_db.get(DailyCloudCopy,key).drive_id
                    remote=client.metadata(identifier)
                    if remote is None:remote=client.upload(identifier,artifact['filename'],parent,artifact['mime_type'],key,artifact['file_bytes'])
                    verified=verify_remote(remote,artifact,parent,client)
                    def acknowledge(state_db):
                        row=state_db.get(DailyCloudCopy,key);row.status,row.verified_at,row.metadata_json='verified',iso_now(),verified
                    run_transaction(acknowledge,self.factory);uploaded+=1
                    if uploaded>=max_files:break
            return uploaded
        finally:client.close()
    def sync_once(self):
        if not self.config.enabled:return {'enabled':False,'synced_rows':0}
        if self._next_attempt>time.time():return {'enabled':True,'backoff':True,'next_attempt':self._next_attempt,'synced_rows':0}
        with self.factory() as db:
            state=db.get(SheetsServiceState,1)
            if state and state.next_attempt>time.time():
                self._next_attempt=state.next_attempt
                return {'enabled':True,'backoff':True,'next_attempt':self._next_attempt,'synced_rows':0}
        if not self.lease.renew():return {'enabled':True,'standby':True,'synced_rows':0}
        client=None
        try:
            self.plan()
            with self.factory() as db:
                pending=list(db.scalars(select(SheetsBatch.id).where(SheetsBatch.status!='verified').order_by(SheetsBatch.created_at,SheetsBatch.trading_date,SheetsBatch.document_part).limit(self.config.max_batches_per_cycle)))
                daily=self._pending_daily_date(db)
            if not pending and not daily:return {'enabled':True,'synced_rows':0}
            token=self.cloud.access_token();client=self.client_factory(token)
            if time.monotonic()-self._quota_at>60:
                quota=client.quota();run_transaction(lambda db:self._save_quota(db,quota),self.factory);self._quota_at=time.monotonic()
                storage=quota.get('storageQuota',{})
                if storage.get('limit') and int(storage.get('usage',0))>=int(storage['limit']):raise StorageFull('Google Drive is full. Download and clean verified copies; local records are retained.')
            count=sum(self._execute(client,key) for key in pending)
            uploaded=self.upload_daily_artifacts(token,daily) if daily else 0
            return {'enabled':True,'synced_rows':count,'uploaded_daily_artifacts':uploaded}
        except Exception as error:
            if isinstance(error,AccessTokenExpired):self.cloud._token,self.cloud._expires=None,0
            self._failure(error);return {'enabled':True,'error':'Google synchronization paused; local records remain queued.','synced_rows':0}
        finally:
            if client:client.close()
    def _save_quota(self,db,quota):
        state=db.get(SheetsServiceState,1)
        if not state:state=SheetsServiceState(id=1);db.add(state)
        state.quota={'storageQuota':quota.get('storageQuota',{}),'user':quota.get('user',{}),'sampled_at':iso_now()}
    def retry(self,db):
        state=db.get(SheetsServiceState,1)
        if state:state.next_attempt=0
        db.commit();self._next_attempt=0;self.notify_work()
    def status(self,db):
        state=db.get(SheetsServiceState,1)
        credential=db.get(CloudDriveCredential,1)
        pending=db.scalar(select(func.count()).select_from(PnlSnapshot).outerjoin(SheetsProgress,SheetsProgress.trade_id==PnlSnapshot.trade_id).where(PnlSnapshot.snapshot_second>func.coalesce(SheetsProgress.last_second,0))) or 0
        documents=list(db.scalars(select(SheetsDocument).order_by(SheetsDocument.trading_date.desc(),SheetsDocument.part).limit(100)))
        quota=state.quota if state else {};storage=quota.get('storageQuota',{})
        def count(name):
            try:return int(storage[name])
            except (KeyError,ValueError,TypeError):return None
        return {'enabled':self.config.enabled,'connected':bool(credential and not credential.auth_required),
            'unsent_records':pending,'last_error':state.error if state else None,'account_email':quota.get('user',{}).get('emailAddress'),
            'drive_storage':{'usage_bytes':count('usage'),'limit_bytes':count('limit'),'trash_bytes':count('usageInDriveTrash')},
            'interval_seconds':self.config.interval_seconds,'pending_snapshots':pending,
            'last_success':state.last_success if state else None,'error':state.error if state else None,'storage_full':bool(state and state.storage_full),
            'next_attempt':state.next_attempt if state else 0,'google_account':state.quota if state else {},
            'documents':[{**_document(row),'id':row.drive_id,'date':row.trading_date,'name':f'Paper_Trading_{row.trading_date}'+(f'_Part_{row.part:03d}' if row.part>1 else ''),
                'last_sync_at':state.last_success if state else None,'cell_count':row.allocated_cells,'status':row.status,'allocated_cells':row.allocated_cells,'cell_limit':self.config.max_cells,
                'duplicate_file_ids':row.duplicate_file_ids,'url':f'https://docs.google.com/spreadsheets/d/{row.drive_id}/edit' if row.drive_id else None} for row in documents]}
    async def run(self,stop_event):
        with self._lock:self._loop,self._wake=asyncio.get_running_loop(),asyncio.Event()
        next_cycle=0
        try:
            while not stop_event.is_set():
                # A one-second recorder may wake this task frequently. Wakes
                # never turn the five-second upload policy into one-second API calls.
                remaining=next_cycle-time.monotonic()
                if remaining>0:
                    try:await asyncio.wait_for(stop_event.wait(),timeout=remaining)
                    except asyncio.TimeoutError:pass
                if stop_event.is_set():break
                result={}
                if self.config.enabled:
                    try:result=await asyncio.to_thread(self.sync_once)
                    except Exception:result={'error':True}
                next_cycle=time.monotonic()+self.config.interval_seconds
                self._wake.clear()
                delay=600 if not self.config.enabled or not result.get('synced_rows') and not result.get('uploaded_daily_artifacts') and not result.get('error') and not result.get('backoff') else self.config.interval_seconds
                if result.get('backoff'):delay=max(self.config.interval_seconds,min(900,result.get('next_attempt',time.time()+60)-time.time()))
                try:await asyncio.wait_for(self._wake.wait(),timeout=delay)
                except asyncio.TimeoutError:pass
        finally:
            with self._lock:self._loop,self._wake=None,None
            try:await asyncio.to_thread(self.lease.release)
            except Exception:pass
    def cleanup_preview(self,db,date):
        from .daily_reports import DailyReport,DailyReportPart
        report=db.get(DailyReport,date)
        if not report:raise SheetsError('Daily export not found.')
        acknowledged=bool(getattr(report,'local_ack_at',None) and getattr(report,'local_ack_checksum',None)==report.manifest_checksum)
        start,end=day_bounds(date)
        unsent=db.scalar(select(func.count()).select_from(PnlSnapshot).outerjoin(SheetsProgress,SheetsProgress.trade_id==PnlSnapshot.trade_id).where(
            PnlSnapshot.snapshot_second>=start,PnlSnapshot.snapshot_second<end,PnlSnapshot.snapshot_second>func.coalesce(SheetsProgress.last_second,0))) or 0
        plans=db.scalar(select(func.count()).select_from(SheetsBatch).where(SheetsBatch.trading_date==date,SheetsBatch.status!='verified')) or 0
        artifacts=list(db.scalars(select(DailyReportPart).where(DailyReportPart.trading_date==date)))
        copies=list(db.scalars(select(DailyCloudCopy).where(DailyCloudCopy.trading_date==date)))
        complete=bool(artifacts) and all(any(c.source_sha256==a.sha256 and c.status in ('verified','trashed') and c.verified_at for c in copies) for a in artifacts)
        docs=list(db.scalars(select(SheetsDocument).where(SheetsDocument.trading_date==date)))
        captured=db.scalar(select(func.count()).select_from(PnlSnapshot).where(PnlSnapshot.snapshot_second>=start,PnlSnapshot.snapshot_second<end)) or 0
        source_unchanged=captured==report.snapshot_count
        eligible=report.status=='finalized' and acknowledged and not unsent and not plans and complete and source_unchanged and not any(d.duplicate_file_ids for d in docs)
        return {'trading_date':date,'eligible':eligible,'finalized':report.status=='finalized','manifest_checksum':report.manifest_checksum,
            'record_count':report.record_count,'local_backup_acknowledged':acknowledged,'unsynchronized_snapshots':unsent,'pending_batches':plans,
            'all_archives_verified':complete,'source_snapshot_count_matches':source_unchanged,'files':[d.drive_id for d in docs if d.drive_id and d.status!='trashed']+[c.drive_id for c in copies if c.status!='trashed'],
            'warning':'Cloud copies move to Google Drive Trash. Emptying Trash permanently deletes them. A laptop copy alone does not protect against laptop failure.'}
    def cleanup_confirm(self,date,manifest_checksum,confirmed_record_count,confirmed=False,confirmation=''):
        if not confirmed or confirmation!='DELETE VERIFIED CLOUD COPIES':raise SheetsError('Explicit local-backup confirmation and the displayed phrase are required.')
        with self.factory() as db:
            preview=self.cleanup_preview(db,date)
            if not preview['eligible'] or preview['manifest_checksum']!=manifest_checksum or preview['record_count']!=confirmed_record_count:raise SheetsError('Cleanup evidence is incomplete or export changed; no cloud copies removed.')
            docs=[_document(d) for d in db.scalars(select(SheetsDocument).where(SheetsDocument.trading_date==date,SheetsDocument.status!='trashed'))]
            copies=[{k:getattr(c,k) for k in ('key','drive_id')} for c in db.scalars(select(DailyCloudCopy).where(DailyCloudCopy.trading_date==date,DailyCloudCopy.status!='trashed'))]
        client=self.client_factory(self.cloud.access_token())
        try:
            before=client.quota()
            for doc in docs:
                client.trash_owned(doc['drive_id'],'pot_sheets_key',doc['lookup_key'])
                def mark(db):
                    row=db.get(SheetsDocument,(date,doc['part']));row.status,row.updated_at='trashed',iso_now()
                run_transaction(mark,self.factory)
            for item in copies:
                client.trash_owned(item['drive_id'],'pot_backup_key',item['key'])
                def mark(db):
                    row=db.get(DailyCloudCopy,item['key']);row.status,row.trashed_at='trashed',iso_now()
                run_transaction(mark,self.factory)
            return {'moved_to_trash':len(docs)+len(copies),'storage_before':before,'storage_after':client.quota(),'source_records_deleted':False,
                'notice':'Trash consumes storage until you explicitly empty it. Gmail, Photos and other files also consume account storage.'}
        finally:client.close()
