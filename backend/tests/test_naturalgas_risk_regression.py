"""User-supplied MCX spread: exact intrinsic risk, independent of margin data."""
import copy
from decimal import Decimal
import pytest
from app.payoff import strategy_risk, leg_payoff
from test_frontend_risk_math import run_risk


def naturalgas(lots=1):
    return [dict(broker='kotak',exchange='MCX',underlying='NATURALGAS',
                 expiry='2026-11-20',side=side,option_type='PE',strike=strike,
                 entry_price=price,lots=lots,open_lots=lots,lot_size=1250,
                 price_multiplier=1,token=token,symbol=symbol)
            for side,strike,price,token,symbol in (
                ('SELL',300,24.05,'TEST-300','SYNTHETIC_NG_300PE'),
                ('BUY',290,18.15,'TEST-290','SYNTHETIC_NG_290PE'))]


def test_naturalgas_exact_expiry_values_and_charges():
    risk=strategy_risk(naturalgas(),spot=300,charges=66.38)
    assert risk['net_premium']==7375
    assert risk['gross_max_profit']==7375 and risk['gross_max_loss']==5125
    assert risk['max_profit']==7308.62 and risk['max_loss']==5191.38
    assert risk['breakevens']==[294.15]
    assert risk['breakevens_unrounded']==pytest.approx([294.153104])
    assert risk['estimated_margin'] is None and risk['hedge_benefit'] is None
    assert risk['profit_unlimited'] is False and risk['loss_unlimited'] is False
    assert abs(sum(leg_payoff(leg,risk['breakevens_unrounded'][0]) for leg in naturalgas())-66.38)<1e-7


@pytest.mark.parametrize('lots',[1,2,20])
def test_multiple_lots_preserve_exact_units_and_do_not_invent_margin(lots):
    risk=strategy_risk(naturalgas(lots),charges=Decimal('66.38')*lots)
    assert risk['net_premium']==7375*lots
    assert risk['max_profit']==pytest.approx(7308.62*lots)
    assert risk['max_loss']==pytest.approx(5191.38*lots)
    assert risk['breakevens_unrounded']==pytest.approx([294.153104])
    assert risk['estimated_margin'] is None and risk['hedge_benefit'] is None


def test_leg_exit_realized_offsets_and_empty_position_recalculate():
    position=naturalgas(2)
    for leg in position:leg['open_lots']=1
    partial=strategy_risk(position,charges=66.38,realized_pnl=500)
    assert partial['max_profit']==7808.62 and partial['max_loss']==4691.38
    unhedged=strategy_risk(position[:1],charges=66.38,realized_pnl=500)
    assert unhedged['max_loss']>300000
    assert unhedged['estimated_margin'] is None
    for leg in position:leg['open_lots']=0
    closed=strategy_risk(position,charges=66.38,realized_pnl=500)
    assert closed['closed'] and closed['max_profit']==433.62 and closed['max_loss']==0


def test_mixed_expiries_and_missing_monetary_contract_data_are_explicit():
    position=naturalgas();position[1]['expiry']='2026-12-20'
    mixed=strategy_risk(position,charges=66.38)
    assert not mixed['supported'] and len(mixed['groups'])==2
    assert mixed['max_loss'] is None and mixed['estimated_margin'] is None
    with pytest.raises(ValueError,match='multiplier'):
        strategy_risk([{key:value for key,value in naturalgas()[0].items() if key!='price_multiplier'}])
    with pytest.raises(ValueError,match='expiry'):
        strategy_risk([{**naturalgas()[0],'expiry':'2026-02-30'}])


def test_browser_preview_agrees_with_backend_for_supplied_spread_and_changes():
    positions=[naturalgas(),naturalgas(20),naturalgas()[:1],[]]
    cases=[dict(legs=copy.deepcopy(legs),charges=66.38,realized=500 if not legs else 0) for legs in positions]
    for case,front in zip(cases,run_risk(cases)):
        back=strategy_risk(case['legs'],charges=case['charges'],realized_pnl=case['realized'])
        assert front['maxProfit']==back['max_profit']
        assert front['maxLoss']==back['max_loss']
        assert front['breakevens']==back['breakevens']
        assert front['estimatedMargin'] is None and front['hedgeBenefit'] is None
