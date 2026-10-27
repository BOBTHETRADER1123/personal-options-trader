"""Current native Kotak derivative master fields, with no symbol-based guesses."""
import pytest

from app.instruments import normalize_row


def native(**changes):
    # Relevant public schema/value types observed in the official 2026 master.
    # The human symbol deliberately carries no parsable strike or expiry.
    row = {'pSymbol': '51624', 'pExchSeg': 'nse_fo', 'pInstType': 'OPTIDX',
           'pSymbolName': 'NIFTY', 'pTrdSymbol': 'OPAQUE BROKER SYMBOL',
           'pOptionType': 'CE', 'lLotSize': '65', 'lExpiryDate': '1477578600',
           'lMultiplier': '-1', 'lPrecision': '2', 'dStrikePrice;': '2.91e+06', 'dTickSize ': '5'}
    row.update(changes)
    return row


def test_current_nse_paise_fields_and_native_epoch_are_normalized_together():
    result = normalize_row('kotak', native())
    assert result['strike'] == 29100 and result['tick_size'] == .05
    assert result['expiry'] == '2026-10-27' and result['lot_size'] == 65
    assert result['symbol'] == 'OPAQUE BROKER SYMBOL'
    assert result['id'] == 'kotak:nse_fo:51624' and result['price_multiplier'] == 1


def test_native_mcx_unknown_multiplier_does_not_prevent_catalog_loading():
    result = normalize_row('kotak', native(pSymbol='584157', pExchSeg='mcx_fo', pInstType='OPTFUT',
        pSymbolName='NATGASMINI', lLotSize='250', lExpiryDate='1795219199', **{'dStrikePrice;':'54500'}))
    assert result['strike'] == 545 and result['tick_size'] == .05
    assert result['expiry'] == '2026-11-20' and result['lot_size'] == 250
    assert result['price_multiplier'] is None and result['multiplier_verified'] is False


@pytest.mark.parametrize('sentinel', ['-1', '-1.0', '0', '0.0'])
def test_only_native_kotak_multiplier_sentinels_are_unverified(sentinel):
    row = native(pExchSeg='mcx_fo', lExpiryDate='1795219199', lMultiplier=sentinel)
    assert normalize_row('kotak', row)['price_multiplier'] is None
    with pytest.raises(ValueError):
        normalize_row('kotak', {**row, 'price_multiplier': sentinel})


def test_explicit_normalized_prices_override_native_paise_without_double_scaling():
    result = normalize_row('kotak', native(strike='29100', tick_size='.05'))
    assert result['strike'] == 29100 and result['tick_size'] == .05


def test_legacy_normalized_input_without_precision_preserves_human_values():
    row = native(**{'dStrikePrice;': '29100', 'dTickSize ': '.05'})
    row.pop('lPrecision')
    result = normalize_row('kotak', row)
    assert result['strike'] == 29100 and result['tick_size'] == .05


@pytest.mark.parametrize('precision', ['-1', '0', '3', '2.5', 'NaN', 'unknown'])
def test_unrecognized_native_price_schema_fails_closed(precision):
    with pytest.raises(ValueError):
        normalize_row('kotak', native(lPrecision=precision))


def test_future_ignores_native_no_strike_sentinel_but_scales_tick():
    result = normalize_row('kotak', native(pOptionType='XX', **{'dStrikePrice;': '-1'}))
    assert result['option_type'] == 'FUT' and result['strike'] == 0 and result['tick_size'] == .05


def test_fivepaisa_human_price_fields_and_strict_multiplier_unchanged():
    row = {'Exch':'M', 'ExchType':'D', 'ScripCode':'123', 'Name':'OPAQUE', 'SymbolRoot':'NATURALGAS',
           'Expiry':'20301029', 'StrikeRate':'545', 'TickSize':'.05', 'ScripType':'CE', 'LotSize':'1250', 'Multiplier':'1'}
    result = normalize_row('fivepaisa', row)
    assert result['strike'] == 545 and result['tick_size'] == .05 and result['price_multiplier'] == 1
    with pytest.raises(ValueError):
        normalize_row('fivepaisa', {**row, 'Multiplier':'-1'})


def test_direct_bse_end_of_day_epoch_keeps_utc_business_date():
    result = normalize_row('kotak', native(pExchSeg='bse_fo', pSymbolName='SENSEX', lExpiryDate='1795219199'))
    assert result['expiry'] == '2026-11-20'
