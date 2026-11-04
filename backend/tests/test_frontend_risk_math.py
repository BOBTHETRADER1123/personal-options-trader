"""The displayed TypeScript risk uses analytical tails, costs and exact quantities."""
import copy
import json
import shutil
import subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]

def run_risk(cases):
    node=shutil.which('node')
    bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node=node or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT/'frontend/node_modules/typescript').is_dir():pytest.skip('Node and frontend dependencies required.')
    script=r"""
const fs=require('node:fs'),Module=require('node:module'),ts=require(process.argv[1]),path=process.argv[2];
const output=ts.transpileModule(fs.readFileSync(path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}});
const mod=new Module(path);mod._compile(output.outputText,path);
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify(cases.map(c=>mod.exports.previewRisk(c.legs,c.reference??22700,c.charges??0,c.realized??0))));
"""
    result=subprocess.run([node,'-e',script,str(ROOT/'frontend/node_modules/typescript'),str(ROOT/'frontend/src/tradeMath.ts')],input=json.dumps(cases),text=True,capture_output=True,check=True,timeout=30)
    return json.loads(result.stdout)

def legs(last_kind='CE'):
    entries=[('SELL',22700,'CE',185.10),('SELL',22700,'PE',146.50),('BUY',22850,'CE',111.35),('BUY',22950,last_kind,286.20),('BUY',22550,'PE',91.95)]
    return [dict(broker='kotak',exchange='NSE',underlying='NIFTY',expiry='2026-11-03',side=side,strike=strike,option_type=kind,entry_price=price,lots=1,lot_size=65) for side,strike,kind,price in entries]

def test_exact_five_call_case_is_unlimited_and_reference_independent():
    cases=[dict(legs=legs(),reference=reference,charges=126.69) for reference in (1,22700,100000000)]
    for risk in run_risk(cases):
        assert risk['supported'] and risk['profitUnlimited'] and risk['maxProfit'] is None
        assert risk['netPremium']==-10263.50
        assert risk['maxLossGross']==20013.50 and risk['maxLoss']==20140.19
        assert risk['breakevens']==pytest.approx([23259.85])
        assert risk['estimatedMargin'] is None and risk['hedgeBenefit'] is None
        assert risk['validationWarnings']

def test_actual_saved_put_case_has_finite_profit_and_different_breakeven():
    risk=run_risk([dict(legs=legs('PE'),charges=126.69)])[0]
    assert risk['profitUnlimited'] is False
    assert risk['maxProfit']==1471609.81
    assert risk['breakevens']==pytest.approx([22745.08])
    assert risk['estimatedMargin'] is None and risk['hedgeBenefit'] is None

def test_partial_quantity_and_realized_profit_offset_are_applied_once():
    position=legs()[:1];position[0].update(side='BUY',entry_price=100,lots=3,open_lots=1)
    risk=run_risk([dict(legs=position,charges=126.69,realized=500)])[0]
    assert risk['maxLoss']==6126.69
    assert risk['breakevens']==pytest.approx([22794.26])

def test_mixed_expiries_and_invalid_units_never_invent_combined_risk():
    mixed=legs();mixed[-1]['expiry']='2026-11-10'
    invalid=legs();invalid[0]['lot_size']=65.5
    a,b=run_risk([dict(legs=mixed),dict(legs=invalid)])
    assert not a['supported'] and len(a['groups'])==2
    assert a['maxLoss'] is None and a['estimatedMargin'] is None
    assert not b['supported'] and b['reason']
    negative=legs();negative[0]['lots']=-1
    excess=legs();excess[0].update(lots=1,open_lots=2)
    assert all(not risk['supported'] for risk in run_risk([dict(legs=negative),dict(legs=excess)]))

def test_zero_payoff_interval_and_closed_position_are_explicit():
    neutral=legs()[:1];neutral[0].update(entry_price=100)
    neutral.append({**neutral[0],'side':'BUY'})
    flat,closed=run_risk([dict(legs=neutral),dict(legs=[],charges=20,realized=100)])
    assert flat['breakevenIntervals']==[{'from':0,'to':None}]
    assert flat['maxProfit']==0 and flat['maxLoss']==0
    assert closed['supported'] and closed['maxProfit']==80 and closed['maxLoss']==0


def test_positive_and_negative_half_cent_rounding_matches_decimal_backend():
    from app.payoff import strategy_risk
    for result,realized in zip(run_risk([{'legs':[],'realized':-.005},{'legs':[],'realized':.005}]),[-.005,.005]):
        backend=strategy_risk([],realized_pnl=realized)
        assert result['maxProfit']==backend['max_profit']
        assert result['maxLoss']==backend['max_loss']
    position=legs()[:1];position[0].update(side='BUY',entry_price=.005,lot_size=1)
    assert run_risk([{'legs':position}])[0]['netPremium']==-.01
