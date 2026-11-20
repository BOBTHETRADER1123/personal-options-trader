"""Verified energy quantities are applied exactly once; unknown units block fills."""
import json

import pytest

from app import engine
from app.instruments import ContractCatalog, normalize_row

UNITS = [('CRUDEOIL', 100, 'Barrels'), ('CRUDEOILM', 10, 'bbl'),
         ('NATURALGAS', 1250, 'mmBtu'), ('NATGASMINI', 250, 'MMBtu')]


def kotak(symbol='NATGASMINI', quantity=250, unit='mmBtu', **changes):
    row = {'pSymbol':'123', 'pExchSeg':'mcx_fo', 'pSymbolName':symbol, 'pTrdSymbol':'OPAQUE',
           'pOptionType':'CE', 'lLotSize':str(quantity), 'lExpiryDate':'1795219199',
           'lPrecision':'2', 'dStrikePrice;':'34000', 'dTickSize':'5', 'lMultiplier':'-1',
           'pPriceUnits':unit, 'pTrdUnits':unit, 'pDeliveryUnits':unit,
           'dPriceQuatation':'1', 'dPriceNum':'1', 'dPriceDen':'1', 'dGenNum':'1', 'dGenDen':'1'}
    row.update(changes)
    return row


@pytest.mark.parametrize('symbol,quantity,unit', UNITS)
def test_kotak_requires_explicit_matching_units_and_unity_quotation(symbol, quantity, unit):
    result = normalize_row('kotak', kotak(symbol, quantity, unit))
    assert result['price_multiplier'] == 1 and engine.monetary_units(result) == quantity
    assert result['monetary_conversion_source'] == 'mcx_energy_contract_units_v1'


@pytest.mark.parametrize('change', [{'pPriceUnits':''}, {'pTrdUnits':''}, {'pPriceUnits':'kg'},
                                   {'dPriceQuatation':'10'}, {'dPriceDen':'100'}, {'lLotSize':'1'}])
def test_kotak_unverified_conversion_remains_unavailable(change):
    result = normalize_row('kotak', kotak(**change))
    assert result['price_multiplier'] is None and not result['multiplier_verified']
    assert result['monetary_conversion_source'] is None


def test_old_unverified_kotak_factor_is_blocked_and_retired_cache_is_omitted(tmp_path):
    old=normalize_row('kotak',kotak('NATURALGAS',1250,'mmBtu'))
    old.pop('monetary_conversion_source');old.update(price_multiplier=1250,multiplier_verified=True)
    original_trade_leg=dict(old)
    verified=normalize_row('kotak',kotak(pSymbol='456'))
    equity={**verified,'id':'equity','instrument_id':'equity','exchange':'NSE','price_multiplier':1}
    retired={'id':'fivepaisa:123:legacy','instrument_id':'fivepaisa:123:legacy','broker':'fivepaisa','demo':False}
    path=tmp_path/'public-master.json'
    path.write_text(json.dumps({'instruments':[old,verified,equity,retired]}))
    before=path.read_bytes();catalog=ContractCatalog(path)
    assert catalog.error is None and len(catalog._live)==3
    cached=catalog.find(old['id'],'kotak')
    assert cached['price_multiplier'] is None and cached['multiplier_verified'] is False
    assert cached['requires_master_refresh'] is True
    assert catalog.find(verified['id'],'kotak')['price_multiplier']==1
    assert catalog.find('equity','kotak')['price_multiplier']==1
    assert catalog.find(retired['id']) is None
    assert path.read_bytes()==before and original_trade_leg['price_multiplier']==1250
