"""Official master validation with mocked read-only HTTP; no broker login."""
import asyncio
import csv
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import engine, option_chain as chain
from app.database import Base
from app.instruments import ContractCatalog, normalize_row
from app.kotak_master import (MasterBatch, MasterRefreshError, SEGMENTS,
    download, parse_csv, resolve_paths, retry)
from app.models import Trade, PnlSnapshot

NOW = datetime(2026,11,4,5,0,tzinfo=timezone.utc)
TODAY = '2026-11-04'
STAMP = NOW.isoformat()
HOST = 'https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/'


def paths(day=TODAY):
    return {seg: HOST + day + '/transformed/' + seg + '.csv' for seg in SEGMENTS}


def row(segment='nse_fo', **changes):
    value = {'pSymbol':'123','pExchSeg':segment,'pTrdSymbol':'TEST CE','pSymbolName':'TEST',
             'expiry':'2026-11-24','strike':'25000','pOptionType':'CE','lLotSize':'65','tick_size':'.05'}
    value.update(changes)
    return value


def csv_bytes(rows):
    out=io.StringIO();writer=csv.DictWriter(out,fieldnames=list(rows[0]))
    writer.writeheader();writer.writerows(rows)
    return out.getvalue().encode('utf-8-sig')


def fake_client(responses):
    calls=[]
    class Rest:
        def request(self, **kwargs):
            calls.append(kwargs)
            response=responses.pop(0)
            if isinstance(response,Exception):raise response
            return response
    config=SimpleNamespace(consumer_key='SYNTHETIC_PRIVATE_KEY',get_url_details=lambda name:'https://mis.kotaksecurities.com/script-details/1.0/masterscrip/file-paths')
    return SimpleNamespace(api_client=SimpleNamespace(configuration=config,rest_client=Rest())),calls


def test_paths_are_resolved_from_current_official_response_not_hardcoded_date():
    actual=paths()
    assert resolve_paths({'data':{'filesPaths':list(actual.values())}},TODAY)==actual
    alternative={k:v.replace('/transformed/','/transformed-v1/').replace('.csv','-v1.csv') for k,v in actual.items()}
    assert resolve_paths({'data':{'filesPaths':list(alternative.values())}},TODAY)==alternative


@pytest.mark.parametrize('payload,code', [({},'invalid_data_object'),({'data':[]},'invalid_data_object'),
    ({'data':{}},'invalid_filesPaths'),({'data':{'filesPaths':['not-a-master']}},'segment_file_missing'),
    ({'data':{'filesPaths':list(paths('2026-11-03').values())}},'source_file_not_current'),
    ({'data':{'filesPaths':[p.replace('lapi.kotaksecurities.com','evil.example') for p in paths().values()]}},'untrusted_official_url'),
    ({'data':{'filesPaths':list(paths().values())+[paths()['nse_fo'].replace('/transformed/','/transformed-v1/')]}},'ambiguous_segment_files')])
def test_paths_fail_closed(payload,code):
    with pytest.raises(MasterRefreshError) as error:resolve_paths(payload,TODAY)
    assert error.value.details['code']==code


@pytest.mark.parametrize('change,code', [({'pExchSeg':'bse_fo'},'segment_mismatch'),
    ({'expiry':'wrong'},'invalid_contract_fields'),({'lLotSize':'0'},'invalid_contract_fields'),
    ({'pSymbol':'nan'},'invalid_contract_fields')])
def test_csv_row_stage_and_number_are_safe_and_exact(change,code):
    with pytest.raises(MasterRefreshError) as error:parse_csv(csv_bytes([row(**change)]),'nse_fo',today=TODAY,stamp=STAMP)
    assert error.value.details=={'code':code,'stage':'csv_row','segment':'nse_fo','row_number':2}


@pytest.mark.parametrize('column', ['pSymbol','pExchSeg','pSymbolName','expiry','strike','pOptionType','lLotSize','tick_size'])
def test_required_columns_are_checked_before_any_replacement(column):
    value=row();value.pop(column)
    with pytest.raises(MasterRefreshError) as error:parse_csv(csv_bytes([value]),'nse_fo',today=TODAY,stamp=STAMP)
    assert error.value.details['code']=='required_columns_missing'


def test_csv_rejects_non_csv_duplicates_expired_only_and_unchecked_paise():
    for raw,code in [(b'<html>unavailable</html>','non_csv_response'),(csv_bytes([row(),row()]),'duplicate_contract_id'),
                     (csv_bytes([row(expiry='2026-11-03')]),'no_current_derivatives'),
                     (csv_bytes([row(dStrikePrice='2500000')]),'native_price_precision_missing')]:
        with pytest.raises(MasterRefreshError) as error:parse_csv(raw,'nse_fo',today=TODAY,stamp=STAMP)
        assert error.value.details['code']==code


def test_csv_validates_bom_header_whitespace_and_native_current_expiry():
    value=row();value.pop('expiry');value.pop('strike');value.pop('tick_size')
    value.update(pExpiryDate=str(int(datetime(2026,11,24,tzinfo=timezone.utc).timestamp()))+'.0',
                 **{'dStrikePrice;':'2500000','dTickSize ':'5','lPrecision':'2'})
    rows,meta=parse_csv(csv_bytes([value]),'nse_fo',today=TODAY,stamp=STAMP)
    assert rows[0]['expiry']=='2026-11-24' and rows[0]['strike']==25000
    assert rows[0]['tick_size']==.05 and rows[0]['master_active']
    assert rows[0]['master_source_date']==TODAY and meta['active_count']==1


def test_bounded_retry_only_for_temporary_errors(caplog):
    delays=[];count=[]
    def operation():
        count.append(1)
        if len(count)<3:raise httpx.ConnectTimeout('SYNTHETIC_PRIVATE_KEY')
        return 7
    assert retry(operation,stage='csv_download',deadline=100,clock=lambda:0,sleep=delays.append)==7
    assert delays==[1,2] and 'SYNTHETIC_PRIVATE_KEY' not in caplog.text
    with pytest.raises(MasterRefreshError) as error:
        retry(lambda: (_ for _ in ()).throw(MasterRefreshError('permanent','response_status',http_status=403)),
              stage='scrip_master_request',deadline=100,clock=lambda:0,sleep=delays.append)
    assert error.value.details['http_status']==403 and delays==[1,2]


def test_complete_three_segment_download_uses_official_get_headers_and_no_csv_credentials(monkeypatch,caplog):
    public=paths();client,calls=fake_client([httpx.Response(503),httpx.Response(200,json={'data':{'filesPaths':list(public.values())}})])
    downloads=[]
    def handler(request):
        downloads.append(request)
        segment=request.url.path.split('/')[-1].removesuffix('.csv')
        return httpx.Response(200,content=csv_bytes([row(segment)]),headers={'content-type':'text/csv'})
    original=httpx.Client
    monkeypatch.setattr('app.kotak_master.httpx.Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    waits=[]
    result=download(client=client,now=NOW,sleep=waits.append)
    assert len(result)==3 and set(result.metadata['segments'])==set(SEGMENTS)
    assert waits==[1] and all(c['method']=='GET' for c in calls)
    assert all(c['headers']['Authorization']=='SYNTHETIC_PRIVATE_KEY' for c in calls)
    assert all('authorization' not in req.headers for req in downloads)
    assert 'SYNTHETIC_PRIVATE_KEY' not in caplog.text


@pytest.mark.parametrize('status,body,code', [(403,None,'scrip_master_http_error'),(200,b'not json','invalid_json')])
def test_sdk_error_details_are_not_swallowed_or_retried(status,body,code):
    client,calls=fake_client([httpx.Response(status,content=body or b'private broker message')])
    with pytest.raises(MasterRefreshError) as error:download(client=client,now=NOW,sleep=lambda _:None)
    assert error.value.details['code']==code and len(calls)==1
    assert 'private broker message' not in str(error.value)


def test_csv_redirect_cannot_leave_official_host(monkeypatch):
    client,_=fake_client([httpx.Response(200,json={'data':{'filesPaths':list(paths().values())}})])
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(302,headers={'location':'https://evil.example/data.csv'})
    original=httpx.Client
    monkeypatch.setattr('app.kotak_master.httpx.Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    with pytest.raises(MasterRefreshError) as error:download(client=client,now=NOW)
    assert error.value.details['code']=='untrusted_official_url' and len(requests)==1


def test_atomic_refresh_retains_referenced_history_and_never_mutates_trades(tmp_path,monkeypatch):
    sql=create_engine('sqlite://');Base.metadata.create_all(sql)
    catalog=ContractCatalog(tmp_path/'instruments.json');old=engine.catalog;engine.set_catalog(catalog)
    prior=normalize_row('kotak',row(pSymbol='99',expiry='2026-11-03'))
    current=normalize_row('kotak',row());current.update(master_active=True,master_refresh_at=datetime.now(timezone.utc).isoformat(),master_source_date=datetime.now(chain.IST).date().isoformat())
    catalog.merge_live_contracts([prior])
    chain.configure(asyncio.Lock());chain._attempts.clear()
    try:
        with Session(sql) as db:
            saved={'id':'saved','broker':'kotak','legs':[{'instrument_id':prior['id'],'entry_price':125}]}
            db.add(Trade(id='saved',broker='kotak',status='closed',data=saved))
            db.add(PnlSnapshot(trade_id='saved',snapshot_second=123,timestamp=STAMP,data={'net_pnl':42}))
            db.commit()
            metadata={'last_successful_refresh_at':current['master_refresh_at'],'segments':{s:{'source_date':current['master_source_date']} for s in SEGMENTS}}
            monkeypatch.setattr(chain,'download_master',lambda broker:MasterBatch([current],metadata))
            result=asyncio.run(chain.refresh_master(db,True))
            assert result['new_trades_allowed'] and result['last_successful_refresh_at']
            retained=next(c for c in catalog._live if c['id']==prior['id'])
            assert retained['master_active'] is False
            assert db.get(Trade,'saved').data==saved and db.get(PnlSnapshot,('saved',123)).data=={'net_pnl':42}
            before=catalog.path.read_bytes();revision=catalog.revision
            chain._attempts.clear()
            broken=MasterBatch([{**current,'lot_size':0}],metadata)
            monkeypatch.setattr(chain,'download_master',lambda broker:broken)
            # Force fresh attempt without modifying successful source metadata.
            monkeypatch.setattr(chain,'MASTER_REFRESH_SECONDS',0)
            result=asyncio.run(chain.refresh_master(db,True))
            assert result['last_error']['stage']=='cache_validation'
            assert result['last_successful_refresh_at']==metadata['last_successful_refresh_at']
            assert catalog.path.read_bytes()==before and catalog.revision==revision
            assert db.get(Trade,'saved').data==saved and db.scalar(select(PnlSnapshot.snapshot_second))==123
    finally:engine.set_catalog(old);sql.dispose()


def test_duplicate_logical_contract_with_different_token_is_rejected():
    with pytest.raises(MasterRefreshError) as error:
        parse_csv(csv_bytes([row(),row(pSymbol='999')]),'nse_fo',today=TODAY,stamp=STAMP)
    assert error.value.details['code']=='duplicate_contract_identity'
    assert error.value.details['row_number']==3


def test_token_reuse_does_not_relabel_an_open_paper_leg(tmp_path):
    old=normalize_row('kotak',row())
    new={**old,'expiry':'2026-12-29'}
    candidate=SimpleNamespace(_live=[new])
    original={'demo':False,'status':'open','legs':[{**old,'instrument_id':old['id'],'status':'open'}]}
    before=json.dumps(original,sort_keys=True)
    with pytest.raises(MasterRefreshError) as error:chain._assert_active_identities(candidate,[original])
    assert error.value.details['code']=='active_contract_identity_changed'
    assert json.dumps(original,sort_keys=True)==before


def test_slow_cache_validation_never_holds_recording_writer_lock(tmp_path,monkeypatch):
    import threading
    original=engine.catalog
    catalog=ContractCatalog(tmp_path/'instruments.json');engine.set_catalog(catalog)
    sql=create_engine('sqlite://');Base.metadata.create_all(sql)
    lock=asyncio.Lock();chain.configure(lock);chain._attempts.clear()
    contract=normalize_row('kotak',row())
    started,release=threading.Event(),threading.Event()
    prepare=chain.prepare_master
    def slow(*args,**kwargs):
        started.set()
        assert release.wait(3)
        return prepare(*args,**kwargs)
    monkeypatch.setattr(chain,'prepare_master',slow)
    monkeypatch.setattr(chain,'download_master',lambda broker:MasterBatch([contract],{}))
    async def scenario(db):
        task=asyncio.create_task(chain.refresh_master(db,True))
        try:
            assert await asyncio.to_thread(started.wait,2)
            await asyncio.wait_for(lock.acquire(),.1)
            lock.release()
        finally:release.set()
        await task
    try:
        with Session(sql) as db:asyncio.run(scenario(db))
    finally:engine.set_catalog(original);sql.dispose()
