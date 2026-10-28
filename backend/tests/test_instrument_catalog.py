"""Bounded instrument selection routes; no broker credentials or network calls."""
import secrets
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import main
from app.database import Base,configure_sqlite,get_db
from app.models import UserSession
from app.security import COOKIE,digest


def contract(number,*,underlying='NIFTY',exchange='NSE',expiry='2050-01-01',broker='kotak',demo=False):
    return {'id':f'{broker}:{number}','instrument_id':f'{broker}:{number}','broker':broker,
            'underlying':underlying,'exchange':exchange,'expiry':expiry,'demo':demo,
            'strike':number,'option_type':'CE','lot_size':65}


@pytest.fixture
def client(tmp_path,monkeypatch):
    sql=create_engine(f'sqlite:///{tmp_path / "catalog.db"}',connect_args={'check_same_thread':False})
    configure_sqlite(sql,journal_mode='DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False);token=secrets.token_urlsafe(48)
    with factory() as db:
        db.add(UserSession(token_hash=digest(token),csrf_token='csrf',expires_at=time.time()+600));db.commit()
    def db_override():
        with factory() as db:yield db
    main.app.dependency_overrides[get_db]=db_override
    monkeypatch.setattr(main,'database_ready',True)
    monkeypatch.setattr(main,'_catalog_view_cache',{})
    monkeypatch.setattr(main,'_catalog_day',lambda:'2049-12-31')
    mode={'demo':False}
    monkeypatch.setattr(main.engine,'get_settings',lambda db:{'demo_mode':mode['demo']})
    source=SimpleNamespace(_live=[],_demo=[],revision=1)
    monkeypatch.setattr(main,'catalog',source)
    c=TestClient(main.app)
    yield c,token,source,mode
    c.close();main.app.dependency_overrides.clear();sql.dispose()


def sign_in(client):
    client[0].cookies.set(COOKIE,client[1]);return client[0]


def test_catalog_and_contracts_require_owner(client):
    c=client[0]
    assert c.get('/api/instruments/catalog').status_code==401
    assert c.get('/api/instruments').status_code==401


def test_large_catalog_is_bounded_but_all_choices_remain_visible(client):
    c=sign_in(client);source=client[2]
    source._live=[contract(i,underlying=f'STOCK{i//10:03}') for i in range(3005)]
    result=c.get('/api/instruments').json()
    assert len(result['instruments'])==2000 and result['total']==3005
    assert result['truncated'] and result['selection_required']
    assert result['next_offset']==2000 and result['catalog_url']=='/api/instruments/catalog'
    metadata=c.get('/api/instruments/catalog').json()
    assert metadata['total']==3005 and len(metadata['groups'])==301
    assert 'STOCK300' in metadata['underlyings']
    assert 'instruments' not in metadata
    assert all(g['expiries']==['2050-01-01'] for g in metadata['groups'])


def test_scoped_selection_excludes_retired_broker_contracts(client):
    c=sign_in(client);source=client[2]
    source._live=[contract(1),contract(1,broker='fivepaisa'),contract(2,expiry='2050-02-01'),
                  contract(3,exchange='BSE'),contract(4,underlying='BANKNIFTY'),
                  contract(5,broker='fivepaisa',underlying='RETIRED_ONLY')]
    result=c.get('/api/instruments',params={'underlying':'NIFTY','exchange':'NSE','expiry':'2050-01-01'}).json()
    assert [r['id'] for r in result['instruments']]==['kotak:1']
    assert result['total']==1 and not result['truncated'] and result['next_offset'] is None
    assert result['instruments'][0]['lot_size']==65
    metadata=c.get('/api/instruments/catalog').json()
    assert metadata['total']==4 and 'RETIRED_ONLY' not in metadata['underlyings']
    assert all(g['brokers']==['kotak'] for g in metadata['groups'])
    group=next(g for g in metadata['groups'] if g['underlying']=='NIFTY' and g['exchange']=='NSE')
    assert group['brokers']==['kotak'] and group['expiries']==['2050-01-01','2050-02-01']
    own=c.get('/api/instruments',params={'broker':'fivepaisa'}).json()
    assert own['instruments']==[] and own['total']==0
    # Retiring the integration filters choices; it does not erase source rows.
    assert [r['id'] for r in source._live if r['broker']=='fivepaisa']==['fivepaisa:1','fivepaisa:5']


def test_pagination_is_complete_without_duplicate_contracts(client):
    c=sign_in(client);client[2]._live=[contract(i) for i in range(4501)]
    identifiers=[];offset=0
    while offset is not None:
        result=c.get('/api/instruments',params={'underlying':'NIFTY','expiry':'2050-01-01','limit':2000,'offset':offset}).json()
        assert len(result['instruments'])<=2000
        identifiers.extend(r['id'] for r in result['instruments']);offset=result['next_offset']
    assert len(identifiers)==len(set(identifiers))==4501


def test_small_sample_catalog_remains_complete(client):
    c=sign_in(client);source,mode=client[2:];mode['demo']=True
    source._live=[contract(100)];source._demo=[contract(i,demo=True) for i in range(1428)]
    result=c.get('/api/instruments').json()
    assert len(result['instruments'])==1428 and result['demo'] and not result['truncated']
    assert all(r['demo'] for r in result['instruments'])


def test_metadata_cache_tracks_revision_mode_and_ist_date(client,monkeypatch):
    c=sign_in(client);source,mode=client[2:]
    source._live=[contract(1,expiry='2050-01-01')];source._demo=[contract(2,demo=True)]
    first=main._catalog_view(False)
    assert main._catalog_view(False) is first
    source._live=[contract(3,underlying='CRUDEOIL',exchange='MCX')];source.revision+=1
    refreshed=c.get('/api/instruments/catalog').json()
    assert refreshed['underlyings']==['CRUDEOIL'] and refreshed['revision']==2
    mode['demo']=True
    assert c.get('/api/instruments/catalog').json()['underlyings']==['NIFTY']
    monkeypatch.setattr(main,'_catalog_day',lambda:'2050-01-02')
    assert c.get('/api/instruments/catalog').json()['total']==0
    assert c.get('/api/instruments').json()['instruments']==[]


@pytest.mark.parametrize('params',[{'limit':2001},{'limit':0},{'offset':-1}])
def test_invalid_page_bounds_are_rejected(client,params):
    assert sign_in(client).get('/api/instruments',params=params).status_code==422
