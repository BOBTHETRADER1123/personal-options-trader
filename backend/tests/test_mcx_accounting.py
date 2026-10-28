"""MCX physical quantity is counted once in an isolated paper ledger."""
import copy
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import engine
from app.database import Base,configure_sqlite,iso_now
from app.instruments import ContractCatalog,normalize_row
from app.models import Trade
from fastapi import HTTPException

@pytest.mark.parametrize('symbol,quantity',[('CRUDEOIL',100),('CRUDEOILM',10),('NATURALGAS',1250),('NATGASMINI',250)])
@pytest.mark.parametrize('side',['BUY','SELL'])
def test_one_rupee_move_uses_physical_quantity_once(tmp_path,symbol,quantity,side):
    # Synthetic future identity, native broker field units; no real account/API.
    unit='Barrels' if symbol.startswith('CRUDEOIL') else 'mmBtu'
    contract=normalize_row('kotak',{'pSymbol':'123','pExchSeg':'mcx_fo','pSymbolName':symbol,
        'pTrdSymbol':'TEST FUTURE','pOptionType':'XX','lExpiryDate':'2030-10-29','dStrikePrice':'0',
        'lLotSize':str(quantity),'dTickSize':'5','lPrecision':'2','lMultiplier':'-1',
        'pPriceUnits':unit,'pTrdUnits':unit,'pDeliveryUnits':unit,'dPriceQuatation':'1'})
    path=tmp_path/'master.json';path.write_text(json.dumps({'instruments':[contract]}))
    old_catalog=engine.catalog;catalog=ContractCatalog(path);engine.set_catalog(catalog)
    sql=create_engine(f'sqlite:///{tmp_path / "ledger.db"}');configure_sqlite(sql)
    Base.metadata.create_all(sql)
    try:
        with sessionmaker(sql,expire_on_commit=False)() as db:
            settings=engine.update_settings(db,{'demo_mode':False,'fill_basis':'ltp','brokerage_per_order':0,
                'charges_percent':0,'slippage_bps':0,'unlimited_paper_buying_power':True})
            def quote(price):
                engine.record_quote(db,{'broker':'kotak','instrument_id':contract['id'],'ltp':price,
                    'bid':price,'ask':price,'received_at':iso_now(),'demo':False})
            quote(300)
            result=engine.create_trades(db,{'name':'Unit verification','underlying':symbol,'brokers':['kotak'],
                'legs':[{'instrument_id':contract['id'],'side':side,'lots':1,'enabled':True,'order_type':'MARKET'}],
                'idempotency_key':'one-point','confirmed':True})
            assert len(result['trades'])==1,result
            trade_id=result['trades'][0]['id'];quote(301)
            trade=copy.deepcopy(db.get(Trade,trade_id).data)
            expected=quantity*(1 if side=='BUY' else -1)
            assert trade['unrealized_pnl']==pytest.approx(expected)
            assert trade['legs'][0]['price_multiplier']==1
            assert trade['legs'][0]['monetary_conversion_source']
            engine.close_leg(db,trade,trade['legs'][0],1,settings);engine.recalculate(trade)
            assert trade['realized_pnl']==pytest.approx(expected)
            assert trade['net_pnl']==pytest.approx(expected)
    finally:
        sql.dispose();engine.set_catalog(old_catalog)


def test_stale_cached_mcx_factor_cannot_be_used_for_new_fills(monkeypatch):
    legacy={'id':'kotak:mcx_fo:123','exchange':'MCX','price_multiplier':1250}
    monkeypatch.setattr(engine,'contract_for',lambda *args:legacy)
    # Rejection precedes price lookup or any persistent paper-order mutation.
    with pytest.raises(HTTPException) as error:
        engine.new_leg(None,{'instrument_id':legacy['id']},'kotak',{'demo_mode':False},iso_now())
    assert error.value.status_code==422 and 'verified quantity' in error.value.detail


def test_broker_natural_gas_mini_symbol_uses_verified_energy_session():
    from datetime import datetime,timezone
    from app.scheduler import ExchangeCalendar
    session=ExchangeCalendar().state({'exchange':'MCX','underlying':'NATGASMINI','expiry':'2026-11-20'},
        datetime(2026,10,27,12,tzinfo=timezone.utc))
    assert session['known'] and session['open']
