

def test_failed_master_retry_wait_is_fifteen_seconds_not_five_minutes(tmp_path,monkeypatch):
    sql=create_engine('sqlite://');Base.metadata.create_all(sql)
    old=engine.catalog;engine.set_catalog(ContractCatalog(tmp_path/'instruments.json'))
    chain.configure(asyncio.Lock());chain._attempts.clear()
    clock=[1000.0];calls=[]
    monkeypatch.setattr(chain.time,'monotonic',lambda:clock[0])
    def fail(broker):
        calls.append(broker)
        raise MasterRefreshError('temporary','response_status',http_status=424)
    monkeypatch.setattr(chain,'download_master',fail)
    try:
        with Session(sql) as db:
            assert asyncio.run(chain.refresh_master(db,True))['last_error']['http_status']==424
            blocked=asyncio.run(chain.refresh_master(db,True))
            assert blocked['retry_after_seconds']==15 and len(calls)==1
            assert 'five minutes' not in blocked['message']
            clock[0]+=15
            asyncio.run(chain.refresh_master(db,True))
            assert len(calls)==2
    finally:engine.set_catalog(old);sql.dispose();chain._attempts.clear()
import asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import engine, option_chain as chain
from app.database import Base
from app.instruments import ContractCatalog
from app.kotak_master import MasterRefreshError
