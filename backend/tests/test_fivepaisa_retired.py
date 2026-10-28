"""Retired broker history survives while all new runtime access uses Kotak only."""
import asyncio
import copy
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker

from app import engine,option_chain
from app.brokers import FeedManager
from app.database import Base,configure_sqlite
from app.instruments import BROKERS,LEGACY_BROKERS,ContractCatalog,import_master
from app.models import Trade,ActionRecord
from app.schemas import TradeInput,SettingsInput


def contract(broker):
 return {'id':broker+':nse_fo:123','instrument_id':broker+':nse_fo:123','broker':broker,
         'token':'123','symbol':'OFFLINE NIFTY CE','underlying':'NIFTY','exchange':'NSE',
         'segment':'nse_fo','expiry':'2050-12-31','strike':25000,'option_type':'CE',
         'lot_size':65,'tick_size':.05,'demo':False}


@pytest.fixture
def journal(tmp_path):
 sql=create_engine('sqlite:///'+(tmp_path/'history.db').as_posix())
 configure_sqlite(sql,'DELETE');Base.metadata.create_all(sql)
 old=engine.catalog;engine.set_catalog(ContractCatalog(tmp_path/'none.json'))
 with sessionmaker(sql,expire_on_commit=False)() as db:
  engine.update_settings(db,{'demo_mode':False});db.commit()
  yield db
 engine.set_catalog(old);sql.dispose()


def test_only_kotak_runtime_adapter_is_constructed_even_with_saved_env(monkeypatch):
 monkeypatch.setenv('FIVEPAISA_CLIENT_CODE','offline-legacy')
 monkeypatch.setenv('FIVEPAISA_ACCESS_TOKEN','offline-legacy-token')
 manager=FeedManager(lambda quote:None)
 assert [adapter.broker for adapter in manager._adapters]==['kotak']
 assert set(manager.statuses())=={'kotak'}
 assert BROKERS==('kotak',) and 'fivepaisa' in LEGACY_BROKERS


def test_new_request_and_feed_settings_reject_retired_broker():
 with pytest.raises(ValidationError):
  TradeInput(name='Disabled',underlying='NIFTY',brokers=['fivepaisa'],
             legs=[{'instrument_id':'fivepaisa:123','side':'BUY','lots':1}],idempotency_key='offline-key')
 with pytest.raises(ValidationError):SettingsInput(preferred_feed='fivepaisa')


def test_catalog_skips_retired_rows_without_changing_master_file(tmp_path):
 path=tmp_path/'mixed.json';original=json.dumps({'instruments':[contract('fivepaisa'),contract('kotak')]})
 path.write_text(original,encoding='utf-8');catalog=ContractCatalog(path)
 assert {row['broker'] for row in catalog.all(False)}=={'kotak'}
 assert {row['broker'] for row in catalog.all(True)}=={'kotak'}
 assert catalog.find(contract('fivepaisa')['id']) is None
 assert path.read_text(encoding='utf-8')==original


def test_runtime_catalog_cannot_reenable_retired_broker(tmp_path):
 catalog=ContractCatalog(tmp_path/'none.json')
 with pytest.raises(ValueError):catalog.merge_live_contracts([contract('fivepaisa')])
 # Even a caller-supplied allowlist cannot turn an old cached row into a feed.
 catalog._live=[contract('fivepaisa'),contract('kotak')]
 assert {row['broker'] for row in catalog.select_live([row['id'] for row in catalog._live],{'kotak','fivepaisa'})}=={'kotak'}


def test_retired_master_import_and_live_chain_reject_before_network(tmp_path):
 output=tmp_path/'master.json'
 with pytest.raises(ValueError,match='Only Kotak Neo'):import_master('fivepaisa','',output)
 assert not output.exists()
 for operation in (option_chain.validate_broker,option_chain.download_master,option_chain.configured):
  with pytest.raises(HTTPException) as error:operation('fivepaisa')
  assert error.value.status_code==422


def test_retired_quotes_cannot_be_recorded_buffered_or_replayed(journal):
 quote={'broker':'fivepaisa','instrument_id':'fivepaisa:123','ltp':100,'demo':False}
 with pytest.raises(ValueError):engine.record_quote(journal,quote)
 with pytest.raises(ValueError):engine.buffer_quote(quote)
 assert not engine._trade_uses_quote({'broker':'fivepaisa'},quote,engine.get_settings(journal))
 with pytest.raises(HTTPException) as error:engine.execution_price(journal,{},'BUY','fivepaisa',engine.get_settings(journal))
 assert error.value.status_code==409


@pytest.mark.parametrize('action',['duplicate','add_leg','add_lots','replace_leg','exit_leg','exit_strategy','partial_exit'])
def test_saved_history_is_unchanged_and_new_actions_blocked(journal,action):
 original={'id':'retired-trade','broker':'fivepaisa','status':'open','name':'Preserved trade','demo':False,'legs':[]}
 journal.add(Trade(id=original['id'],broker='fivepaisa',status='open',data=copy.deepcopy(original)));journal.commit()
 with pytest.raises(HTTPException) as error:
  engine.apply_action(journal,original['id'],{'action':action,'idempotency_key':'retired-action','confirmed':True})
 assert error.value.status_code==409
 assert journal.get(Trade,original['id']).data==original
 assert list(journal.scalars(select(ActionRecord)))==[]
 public=engine.public_trade(original)
 assert public['broker_retired'] and public['broker']=='fivepaisa'
 assert original==journal.get(Trade,original['id']).data


def test_retired_open_history_does_not_restart_snapshot_recording(journal):
 original={'id':'retired-trade','broker':'fivepaisa','status':'open','demo':False,'legs':[]}
 journal.add(Trade(id=original['id'],broker='fivepaisa',status='open',data=original));journal.commit()
 assert engine.take_snapshots(journal)['recorded']==0
 assert journal.get(Trade,original['id']).data==original
