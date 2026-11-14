"""Derived risk must follow real saved contract identity, not chart bounds."""
import copy
from datetime import datetime,timedelta,timezone
import pytest
from pydantic import ValidationError
from app import engine
from app.database import iso_now
from app.schemas import RiskInput


def nifty(last_kind='CE'):
    legs=[]
    for side,strike,kind,entry,mark,fee in [
        ('SELL',22700,'CE',185.10,329.95,26.01575),('SELL',22700,'PE',146.50,49.55,24.76125),
        ('BUY',22850,'CE',111.35,217.9,23.618875),('BUY',22950,last_kind,286.20,123.75,29.3015),
        ('BUY',22550,'PE',91.95,27.95,22.988375)]:
        legs.append(dict(id=str(len(legs)),instrument_id=str(strike)+kind,symbol=str(strike)+kind,
            broker='kotak',exchange='NSE',underlying='NIFTY',expiry='2026-11-03',side=side,
            strike=strike,option_type=kind,entry_price=entry,current_price=mark,charges=fee,
            lots=1,open_lots=1,filled_lots=1,lot_size=65,price_multiplier=1,realized_pnl=0,
            status='open',quote_at=iso_now()))
    return dict(id='synthetic-risk-only',broker='kotak',underlying='NIFTY',demo=False,
                created_at=iso_now(),legs=legs,spot=22700,charges=0,realized_pnl=0)


def test_ce_fixture_and_saved_pe_have_different_correct_theoretical_results():
    ce=engine.recalculate(nifty())
    assert ce['risk']['profit_unlimited'] is True and ce['risk']['max_profit'] is None
    assert ce['risk']['gross_max_loss']==20013.5 and ce['risk']['max_loss']==20140.19
    assert ce['risk']['net_premium']==-10263.5 and ce['risk']['breakevens']==[23259.85]
    assert ce['net_pnl']==pytest.approx(-11033.68575)
    pe=engine.recalculate(nifty('PE'))
    assert pe['risk']['profit_unlimited'] is False and pe['risk']['max_profit']==1471609.81
    assert pe['risk']['breakevens']==[22745.08]
    assert pe['risk']['max_loss']==20140.19 and pe['net_pnl']==pytest.approx(-11033.68575)
    assert pe['legs'][3]['option_type']=='PE'
    assert ce['risk']['validation_warnings'] and not pe['risk']['validation_warnings']
    for trade in (ce,pe):
        assert trade['margin_estimate'] is None and trade['hedge_benefit'] is None
        assert trade['broker_margin']['total_margin'] is None
        assert trade['exchange_margin']['total_margin'] is None


def test_risk_is_cached_for_ticks_and_recomputed_for_exposure_costs_and_realized():
    trade=engine.recalculate(nifty())
    original=trade['_risk_signature'];risk=trade['risk']
    trade['legs'][0]['current_price']+=10
    engine.recalculate(trade)
    assert trade['_risk_signature']==original and trade['risk'] is risk
    # Closing one long call changes the upper tail, and realized P&L contributes once.
    trade['legs'][3].update(open_lots=0,realized_pnl=125,status='closed')
    engine.recalculate(trade)
    assert trade['_risk_signature']!=original and trade['risk']['profit_unlimited'] is False
    assert trade['risk']['realized_pnl']==125
    original=trade['_risk_signature']
    trade['legs'][0].update(lots=2,open_lots=2)
    engine.recalculate(trade)
    assert trade['_risk_signature']!=original and trade['risk']['loss_unlimited'] is True
    for leg in trade['legs']:leg.update(open_lots=0,status='closed')
    engine.recalculate(trade)
    assert trade['risk']['closed'] and trade['risk']['points']==[]
    assert trade['risk']['max_loss']==pytest.approx(1.69)
    assert trade['paper_cash_reserve']==0


def test_public_read_replaces_stale_heuristic_without_rewriting_saved_history():
    trade=engine.recalculate(nifty('PE'));trade.update(margin_estimate=20140.19,hedge_benefit=603502.31,risk={'old':True},_risk_signature='legacy-calculation-v3')
    saved=copy.deepcopy(trade)
    public=engine.public_trade(trade)
    assert public['margin_estimate'] is None and public['hedge_benefit'] is None
    assert public['risk']['max_profit']==1471609.81
    assert trade==saved


@pytest.mark.parametrize('change',[{'expiry':'03-Nov-2026'},{'expiry':'2026-02-30'},{'lots':True},
    {'lots':1.5},{'lot_size':65.1},{'strike':0},{'price':True},{'price':float('nan')},
    {'open_lots':2},{'price_multiplier':0}])
def test_risk_contract_validation(change):
    leg=dict(underlying='NIFTY',expiry='2026-11-03',side='BUY',option_type='CE',strike=22700,
             lots=1,lot_size=65,price=100)
    with pytest.raises(ValidationError):RiskInput(legs=[{**leg,**change}])


def test_future_receipt_is_not_treated_as_fresh_and_corrupt_prices_are_rejected():
    future=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
    assert engine.age_seconds(future) is None
    for row in [{'ltp':True},{'ltp':float('inf')},{'ltp':1,'ask':-1},
                {'ltp':1,'received_at':future},{'ltp':1,'received_at':'not-a-time'}]:
        with pytest.raises(ValueError):engine.validate_quote(row)


def test_naturalgas_butterfly_payoff_is_not_required_margin():
    from app.payoff import strategy_risk
    rows = [("BUY", 300, "PE", 2.00), ("SELL", 315, "PE", 7.00),
            ("SELL", 315, "CE", 5.00), ("BUY", 330, "CE", 2.00)]
    legs = [dict(broker="kotak", exchange="MCX", underlying="NATURALGAS", expiry="2030-11-20",
                 side=side, strike=strike, option_type=kind, entry_price=price,
                 lots=1, open_lots=1, lot_size=1250, price_multiplier=1)
            for side, strike, kind, price in rows]
    risk = strategy_risk(legs, charges=100)
    assert risk["net_premium"] == 10000
    assert risk["max_profit"] == 9900
    assert risk["max_loss"] == 8850
    assert risk["breakevens"] == [307.08, 322.92]
    assert risk["estimated_margin"] is None and risk["hedge_benefit"] is None
