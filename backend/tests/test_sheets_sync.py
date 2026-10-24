"""Mocked Google API tests; recording database remains authoritative."""
import copy
from types import SimpleNamespace
import pytest,httpx
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker
from app.database import Base,configure_sqlite,iso_now
from app.models import Trade,PnlSnapshot
from app import daily_reports
from app.sheets_sync import SheetsSyncService,SheetsConfig,SheetsClient,SheetsBatch,SheetsProgress,SheetsServiceState,SheetsError,StorageFull

@pytest.fixture
def factory(tmp_path):
 sql=create_engine(f'sqlite:///{tmp_path / "sheets.db"}');configure_sqlite(sql,journal_mode='DELETE');Base.metadata.create_all(sql)
 result=sessionmaker(sql,expire_on_commit=False)
 with result() as db:
  for i in range(40):
   broker='kotak' if i%2==0 else 'fivepaisa';tid=f'trade-{i}'
   db.add(Trade(id=tid,broker=broker,status='open',data={'id':tid,'broker':broker,'strategy_id':f'strategy-{i//2}','name':'Mock strategy','demo':True,'legs':[]}))
   for second in range(5):
    at=1792994100+second;data={'timestamp_utc':iso_now(),'demo':True,'net_pnl':second,'legs':[{'id':'leg1','quote_at':'2026-10-26T03:15:00+00:00','quote_reused':second>0,'data_freshness':'reused' if second else 'fresh','open_lots':1,'lot_size':50}]}
    db.add(PnlSnapshot(trade_id=tid,snapshot_second=at,timestamp=iso_now(),data=data))
  db.commit()
 yield result
 sql.dispose()

class Google:
 def __init__(self):self.calls=[];self.fail=False;self.full=False
 def close(self):pass
 def quota(self):
  if self.full:raise StorageFull('Quota full; records retained.')
  return {'storageQuota':{'usage':'10','limit':'1000000'}}
 def ensure_document(self,doc):return f'mock-file-{doc["part"]}',[]
 def prepare_tabs(self,file,tabs):pass
 def write_verified(self,file,ranges):
  self.calls.append(copy.deepcopy(ranges))
  if self.fail:self.fail=False;raise SheetsError('Interrupted after remote write.')

def service(factory,google):
 return SheetsSyncService(factory,SimpleNamespace(access_token=lambda:'test-token'),SheetsConfig(enabled=True,max_snapshots=300),lambda token:google)

def test_peak_20_strategies_40_executions_batches_and_keeps_source(factory):
 google=Google();worker=service(factory,google);result=worker.sync_once()
 assert result['synced_rows']==200 and len(google.calls)==1
 assert worker.sync_once()['synced_rows']==0
 with factory() as db:
  assert db.scalar(select(func.count()).select_from(PnlSnapshot))==200
  assert db.scalar(select(func.count()).select_from(SheetsProgress))==40
  assert all(p.acknowledged_rows==5 for p in db.scalars(select(SheetsProgress)))

def test_interrupted_upload_replays_same_ranges_without_duplicating_cursors(factory):
 google=Google();google.fail=True;worker=service(factory,google)
 assert worker.sync_once().get('error')
 with factory() as db:
  assert db.scalar(select(func.count()).select_from(SheetsProgress))==0
  worker.retry(db)
 assert worker.sync_once()['synced_rows']==200
 assert google.calls[0]==google.calls[1]
 with factory() as db:assert all(p.acknowledged_rows==5 for p in db.scalars(select(SheetsProgress)))

def test_full_drive_persists_backoff_and_does_not_discard_source(factory):
 google=Google();google.full=True;worker=service(factory,google)
 assert worker.sync_once().get('error')
 replacement=service(factory,Google())
 assert replacement.sync_once().get('backoff')
 with factory() as db:
  assert db.get(SheetsServiceState,1).storage_full
  assert db.scalar(select(func.count()).select_from(PnlSnapshot))==200
  assert db.scalar(select(func.count()).select_from(SheetsProgress))==0

def test_plan_is_durable_and_unchanged_until_acknowledged(factory):
 worker=service(factory,Google());assert worker.plan()==200
 with factory() as db:first=db.scalars(select(SheetsBatch)).one();checksum=first.checksum
 assert worker.plan()==0
 with factory() as db:assert db.scalars(select(SheetsBatch)).one().checksum==checksum

def test_cleanup_refuses_unfinalized_unacknowledged_daily_data(factory):
 worker=service(factory,Google())
 with factory() as db:daily_reports.ensure_day(db,'2026-10-26');db.commit()
 with pytest.raises(SheetsError):worker.cleanup_confirm('2026-10-26','0'*64,200,True,'DELETE VERIFIED CLOUD COPIES')
 with factory() as db:assert db.scalar(select(func.count()).select_from(PnlSnapshot))==200

def test_remote_readback_mismatch_is_not_success():
 def handler(req):return httpx.Response(200,json={'responses':[{'updatedData':{'values':[['different']]}}]})
 with httpx.Client(transport=httpx.MockTransport(handler)) as http:
  client=SheetsClient('mock',http)
  with pytest.raises(SheetsError):client.write_verified('id',[{'range':'Sheet!A1:A1','values':[['expected']]}])

def test_existing_spreadsheet_is_reused_after_interrupted_creation():
 calls=[]
 def handler(req):
  calls.append(req.method)
  return httpx.Response(200,json={'files':[{'id':'existing','mimeType':'application/vnd.google-apps.spreadsheet','createdTime':'2026-10-26'}]})
 with httpx.Client(transport=httpx.MockTransport(handler)) as http:
  assert SheetsClient('mock',http).ensure_document({'lookup_key':'key','part':1,'trading_date':'2026-10-26'})==('existing',[])
 assert calls==['GET']

def test_configuration_never_uploads_more_often_than_five_seconds(monkeypatch):
 monkeypatch.setenv('GOOGLE_SHEETS_SYNC_SECONDS','1')
 assert SheetsConfig.from_env().interval_seconds==5
