"""Verified energy quantities are applied exactly once; unknown units block fills."""
import json

import pytest

from app import engine
from app.instruments import ContractCatalog, normalize_row

UNITS = [('CRUDEOIL', 100, 'Barrels'), ('CRUDEOILM', 10, 'bbl'),
         ('NATURALGAS', 1250, 'mmBtu'), ('NATGASMINI', 250, 'MMBtu')]


def five(symbol='NATURALGAS', quantity=1250, **changes):
    row = {'Exch':'M', 'ExchType':'D', 'ScripCode':'123', 'Name':'OPAQUE', 'SymbolRoot':symbol,
           'Expiry':'2026-11-20', 'StrikeRate':'340', 'TickSize':'.05', 'ScripType':'CE',
           'LotSize':str(quantity), 'Multiplier':str(quantity)}
    row.update(changes)
    return row


def kotak(symbol='NATGASMINI', quantity=250, unit='mmBtu', **changes):
    row = {'pSymbol':'123', 'pExchSeg':'mcx_fo', 'pSymbolName':symbol, 'pTrdSymbol':'OPAQUE',
           'pOptionType':'CE', 'lLotSize':str(quantity), 'lExpiryDate':'1795219199',
           'lPrecision':'2', 'dStrikePrice;':'34000', 'dTickSize':'5', 'lMultiplier':'-1',
           'pPriceUnits':unit, 'pTrdUnits':unit, 'pDeliveryUnits':unit,
           'dPriceQuatation':'1', 'dPriceNum':'1', 'dPriceDen':'1', 'dGenNum':'1', 'dGenDen':'1'}
    row.update(changes)
    return row


@pytest.mark.parametrize('symbol,quantity,unit', UNITS)
@pytest.mark.parametrize('kind', ['CE', 'PE', 'XX'])
def test_fivepaisa_quantity_multiplier_is_not_applied_twice(symbol, quantity, unit, kind):
    result = normalize_row('fivepaisa', five(symbol, quantity, ScripType=kind))
    assert result['price_multiplier'] == 1 and result['multiplier_verified']
    assert result['broker_quantity_multiplier'] == quantity
    assert result['monetary_conversion_source'] == 'mcx_energy_contract_units_v1'
    assert engine.monetary_units(result) == quantity
    # One rupee change for two lots must yield twice the physical contract
    # quantity, rather than a squared quantity from broker metadata.
    assert 2 * engine.monetary_units(result) == 2 * quantity


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


@pytest.mark.parametrize('change', [{'Multiplier':'1'}, {'Multiplier':'-1'}, {'LotSize':'1'},
                                   {'SymbolRoot':'GOLD'}, {'Multiplier':''}])
def test_fivepaisa_unsupported_or_mismatched_quantity_never_guessed(change):
    result = normalize_row('fivepaisa', five(**change))
    assert result['price_multiplier'] is None and not result['multiplier_verified']
    assert result['monetary_conversion_source'] is None


def test_explicit_normalized_operator_override_keeps_intent_and_provenance():
    result = normalize_row('fivepaisa', five('GOLD', 1, Multiplier='100', price_multiplier='100'))
    assert result['price_multiplier'] == 100 and result['broker_quantity_multiplier'] == 100
    assert result['monetary_conversion_source'] == 'operator_verified_normalized_multiplier'
    assert engine.monetary_units(result) == 100
    with pytest.raises(ValueError):
        normalize_row('fivepaisa', five(price_multiplier='0'))


def test_old_unverified_kotak_factor_is_blocked_and_retired_cache_is_omitted(tmp_path):
    old=normalize_row('kotak',kotak('NATURALGAS',1250,'mmBtu'))
    old.pop('monetary_conversion_source');old.update(price_multiplier=1250,multiplier_verified=True)
    original_trade_leg=dict(old)
    verified=normalize_row('kotak',kotak(pSymbol='456'))
    equity={**verified,'id':'equity','instrument_id':'equity','exchange':'NSE','price_multiplier':1}
    retired=normalize_row('fivepaisa',five())
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
