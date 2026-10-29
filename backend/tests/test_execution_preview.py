"""Compile the actual TypeScript preview and compare it with real paper fills."""
import copy
import json
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from app import engine

ROOT=Path(__file__).resolve().parents[2]


def run_previews(cases):
    node=shutil.which('node')
    if not node:
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        node=str(bundled) if bundled.is_file() else None
    typescript=ROOT/'frontend/node_modules/typescript'
    if not node or not typescript.is_dir():
        pytest.skip('Install Node.js and frontend dependencies to verify the TypeScript preview.')
    script="""
const fs=require('node:fs'), Module=require('node:module');
const ts=require(process.argv[1]);
const path=process.argv[2];
const source=fs.readFileSync(path,'utf8');
const output=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,strict:true},reportDiagnostics:true});
if((output.diagnostics||[]).some(d=>d.category===ts.DiagnosticCategory.Error)) throw Error('Preview TypeScript compilation failed');
const mod=new Module(path);
mod._compile(output.outputText,path);
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(c=>mod.exports.executionPreview(c.contract,c.leg,c.quote,c.settings,c.nowMs))));
"""
    result=subprocess.run([node,'-e',script,str(typescript),str(ROOT/'frontend/src/executionPreview.ts')],
        input=json.dumps(cases),text=True,capture_output=True,check=True,timeout=30)
    return json.loads(result.stdout)


def case(side='BUY',**overrides):
    now=datetime.now(timezone.utc)
    contract={'id':'kotak:nse_fo:123','broker':'kotak','instrument_id':'kotak:nse_fo:123',
        'exchange':'NSE','segment':'nse_fo','lot_size':50,'price_multiplier':1,
        'expiry':'2035-10-29','underlying':'NIFTY','option_type':'CE','strike':25000,'symbol':'TEST','demo':False}
    value={'contract':contract,'leg':{'side':side,'lots':2,'order_type':'MARKET','price':1},
        'quote':{'broker':'kotak','instrument_id':contract['id'],'ltp':100,'bid':99,'ask':101,
                 'received_at':now.isoformat(),'demo':False},
        'settings':{**engine.DEFAULT_SETTINGS,'demo_mode':False,'fill_basis':'bid_ask',
                    'slippage_bps':20,'brokerage_per_order':20,'charges_percent':0.1},
        'nowMs':now.timestamp()*1000}
    for key,changes in overrides.items():value[key].update(changes)
    return value


def test_frontend_fill_prices_match_actual_backend_orders(monkeypatch):
    cases=[case(),case('SELL'),case(settings={'fill_basis':'ltp'}),
        case(quote={'ask':None}),case('SELL',quote={'bid':None}),
        case(leg={'order_type':'LIMIT','price':99}),
        case('SELL',leg={'order_type':'LIMIT','price':103}),
        case(leg={'order_type':'LIMIT','price':102},settings={'slippage_bps':1000}),
        case('SELL',leg={'order_type':'LIMIT','price':98},settings={'slippage_bps':1000}),
        case('SELL',quote={'ltp':0.005,'bid':0.005,'ask':0.006},settings={'slippage_bps':10000}),
        case(contract={'exchange':'MCX','lot_size':1250,'price_multiplier':1,'underlying':'NATURALGAS',
                       'monetary_conversion_source':'synthetic_verified_spec'})]
    results=run_previews(cases)
    for item,preview in zip(cases,results,strict=True):
        contract=item['contract'];quote=item['quote'];settings=item['settings']
        monkeypatch.setattr(engine,'contract_for',lambda *args,contract=contract:contract)
        monkeypatch.setattr(engine,'resolve_quote',lambda *args,quote=quote:copy.deepcopy(quote))
        request={**item['leg'],'instrument_id':contract['id']}
        actual=engine.new_leg(None,request,'kotak',settings,quote['received_at'])
        assert preview['valid'] is True
        assert preview['pendingLimit']==(actual['status']=='pending')
        assert preview['previewPrice']==pytest.approx(actual['entry_price'])
        assert preview['estimatedCharges']==pytest.approx(actual['charges'])
        assert preview['quantity']==actual['lots']*engine.monetary_units(actual)
        if actual['status']=='pending':assert preview['fillPrice'] is None
        else:
            assert preview['fillPrice']==pytest.approx(actual['entry_price'])
            assert preview['slippageCost']==pytest.approx(actual['slippage_cost'])


def test_frontend_marks_stale_and_foreign_quotes_without_inventing_fills():
    stale=case(quote={'received_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()})
    foreign=case(quote={'broker':'fivepaisa'})
    wrong_token=case(quote={'instrument_id':'kotak:nse_fo:999'})
    no_quote=case();no_quote['quote']=None
    unverified_mcx=case(contract={'exchange':'MCX','price_multiplier':None})
    expired=case(contract={'expiry':'2020-01-01'})
    wrong_mode=case(contract={'demo':True},quote={'demo':True})
    retired=case(contract={'broker':'fivepaisa'},quote={'broker':'fivepaisa'})
    for preview in run_previews([stale,foreign,wrong_token,no_quote,unverified_mcx,expired,wrong_mode,retired]):
        assert preview['valid'] is False and preview['reason']
        assert preview['fillPrice'] is None and preview['previewPrice'] is None
        assert preview['estimatedCharges'] is None


def test_frontend_explains_spread_and_ltp_fallback_separately():
    wide=case(quote={'bid':90,'ask':110})
    missing=case(quote={'ask':None})
    narrow=case(quote={'bid':99,'ask':101})
    ltp_basis=case(quote={'ask':None},settings={'fill_basis':'ltp'})
    values=run_previews([wide,missing,narrow,ltp_basis])
    assert values[0]['spreadPercent']==pytest.approx(20)
    assert values[0]['wideSpread'] is True and values[0]['notices']
    assert values[1]['fallbackToLtp'] is True and values[1]['basePrice']==100
    assert values[1]['missingBidAsk'] is True and values[1]['spreadPercent'] is None
    assert values[1]['notices']
    assert values[2]['wideSpread'] is False
    assert values[3]['fallbackToLtp'] is False and values[3]['basePrice']==100
