"""Export resource/recovery regressions use isolated synthetic records only."""
from dataclasses import replace
import hashlib
import io
import threading
import time
from types import SimpleNamespace
import zipfile
import pytest
from sqlalchemy import func,select,update
from app import daily_reports as daily
from app.models import PnlSnapshot
from test_daily_reports import factory,calendar,insert_trade,make,DAY,AFTER


def test_zip_normalization_streams_members_and_preserves_deterministic_bytes(monkeypatch):
 source=io.BytesIO()
 with zipfile.ZipFile(source,'w',zipfile.ZIP_DEFLATED) as archive:
  archive.writestr('b.xml',b'<rows>'+b'<cell>synthetic</cell>'*10000+b'</rows>')
  archive.writestr('a.xml',b'<test/>')
 original=source.getvalue();expected=io.BytesIO()
 with zipfile.ZipFile(io.BytesIO(original)) as reader,zipfile.ZipFile(expected,'w',zipfile.ZIP_DEFLATED) as writer:
  for name in sorted(reader.namelist()):
   item=zipfile.ZipInfo(name,date_time=(2000,1,1,0,0,0));item.compress_type=zipfile.ZIP_DEFLATED
   writer.writestr(item,reader.read(name))
 def forbidden(*_args,**_kwargs):raise AssertionError('whole ZIP member read would defeat the bound')
 with monkeypatch.context() as patch:
  patch.setattr(zipfile.ZipFile,'read',forbidden)
  actual=daily._normalize_xlsx(original)
 assert actual==expected.getvalue()
 assert daily._normalize_xlsx(actual)==actual


def test_long_normalization_renews_short_export_lease(factory,calendar,monkeypatch):
 with factory() as db:insert_trade(db)
 service=make(factory,calendar,lease_seconds=.35)
 original=daily._normalize_xlsx
 def slow(payload):time.sleep(.8);return original(payload)
 monkeypatch.setattr(daily,'_normalize_xlsx',slow)
 result=service.generate_day(DAY,AFTER)
 assert result['status']=='finalized' and result['attempts']==1
 assert result['record_count']==6
 with factory() as db:
  assert db.scalar(select(func.count()).select_from(PnlSnapshot))==3
  assert db.get(daily.DailyReport,DAY).lease_owner is None


def test_replaced_owner_cannot_publish_or_clear_new_claim(factory,calendar,monkeypatch):
 with factory() as db:insert_trade(db)
 service=make(factory,calendar)
 original=daily._normalize_xlsx
 def replace_owner(payload):
  with factory() as db:
   db.execute(update(daily.DailyReport).where(daily.DailyReport.trading_date==DAY).values(lease_owner='replacement',lease_until=time.time()+100))
   db.commit()
  return original(payload)
 monkeypatch.setattr(daily,'_normalize_xlsx',replace_owner)
 with pytest.raises(daily.DailyReportError,match='ownership'):service.generate_day(DAY,AFTER)
 with factory() as db:
  report=db.get(daily.DailyReport,DAY)
  assert report.status=='generating' and report.lease_owner=='replacement'
  assert db.scalar(select(func.count()).select_from(PnlSnapshot))==3
  assert db.scalar(select(func.count()).select_from(daily.DailyReportPart))==0


def test_heartbeat_never_resurrects_expired_lease(factory,calendar):
 with factory() as db:
  insert_trade(db);report=daily.ensure_day(db,DAY)
  report.status,report.lease_owner,report.lease_until='generating','expired',time.time()-1;db.commit()
 service=make(factory,calendar)
 with pytest.raises(daily.DailyReportError,match='expired'):daily._ExportLease(service,DAY,'expired')._renew()
 with factory() as db:assert db.get(daily.DailyReport,DAY).lease_until<time.time()


def test_recording_pause_keeps_lease_and_resumes_without_losing_rows(factory,calendar):
 with factory() as db:insert_trade(db)
 allowed=threading.Event();service=make(factory,calendar,lease_seconds=.35);service.can_work=allowed.is_set
 results=[];errors=[]
 def work():
  try:results.append(service.generate_day(DAY,AFTER))
  except BaseException as error:errors.append(error)
 worker=threading.Thread(target=work);worker.start()
 try:
  time.sleep(.8)
  with factory() as db:
   report=db.get(daily.DailyReport,DAY)
   assert report.status=='generating' and report.lease_until>time.time()
   assert db.scalar(select(func.count()).select_from(daily.DailyReportPart))==0
 finally:allowed.set();worker.join(5)
 assert not worker.is_alive() and not errors
 assert results[0]['status']=='finalized' and results[0]['record_count']==6
 with factory() as db:assert db.scalar(select(func.count()).select_from(PnlSnapshot))==3


def test_cpu_checkpoint_accounts_for_export_thread_time(monkeypatch):
 clock={'cpu':0.,'wall':0.,'slept':0.}
 monkeypatch.setattr(daily.time,'thread_time',lambda:clock['cpu'])
 monkeypatch.setattr(daily.time,'monotonic',lambda:clock['wall'])
 def sleep(seconds):clock['wall']+=seconds;clock['slept']+=seconds
 monkeypatch.setattr(daily.time,'sleep',sleep)
 service=SimpleNamespace(can_work=lambda:True,_stop_event=None,cpu_fraction=.35)
 guard=daily._ExportLease(service,DAY,'test')
 clock['cpu']=clock['wall']=.35
 guard.checkpoint()
 assert clock['slept']==pytest.approx(.65)
 assert clock['cpu']/clock['wall']==pytest.approx(.35)


def test_stop_during_recording_pause_preserves_source(factory,calendar):
 with factory() as db:insert_trade(db)
 service=make(factory,calendar);service.can_work=lambda:False;service._stop_event=threading.Event()
 service._stop_event.set()
 with pytest.raises(daily.DailyReportError,match='stopped safely'):service.generate_day(DAY,AFTER)
 with factory() as db:
  assert db.scalar(select(func.count()).select_from(PnlSnapshot))==3
  assert db.get(daily.DailyReport,DAY).status=='failed'
  assert db.scalar(select(func.count()).select_from(daily.DailyReportPart))==0


def test_default_parts_are_bounded_and_csv_has_one_utf8_bom(factory,calendar):
 assert daily.ReportLimits().max_workbook_cells==1_000_000
 with factory() as db:insert_trade(db)
 service=make(factory,calendar);service.generate_day(DAY,AFTER)
 import gzip
 with factory() as db:
  body=gzip.decompress(daily.artifact(db,DAY,'csv_gz')['file_bytes'])
 assert body.startswith(b'\xef\xbb\xbf') and body.count(b'\xef\xbb\xbf')==1
