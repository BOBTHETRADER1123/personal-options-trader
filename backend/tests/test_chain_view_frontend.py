"""Exercise the actual chain viewport helpers and market-only screen rendering.

No broker, network or trading database is used by these frontend regressions.
"""
import json
import html as html_lib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def node_runtime():
    node = shutil.which('node')
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node = node or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT / 'frontend/node_modules/typescript').is_dir():
        pytest.skip('Node and installed frontend dependencies are required.')
    return node


@pytest.fixture(scope='module')
def view_results():
    script = r"""
const fs=require('node:fs'),Module=require('node:module'),ts=require(process.argv[1]),path=process.argv[2];
const output=ts.transpileModule(fs.readFileSync(path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}});
const mod=new Module(path);mod._compile(output.outputText,path);
const {visibleStrikeRows,centerChainViewport,requireMarketChain}=mod.exports;
const rows=Array.from({length:11},(_,i)=>({strike:100+i*10,atm:i===5,ltp:50+i}));
const samples=[{demo:true,mode:'live',rows:[1]},{demo:false,mode:'sample',rows:[2]}];
const rejected=samples.map(value=>{const before=JSON.stringify(value);let threw=false;try{requireMarketChain(value)}catch(error){threw=error instanceof Error}return {threw,untouched:before===JSON.stringify(value)}});
const live={demo:false,mode:'live',rows:[]},unavailable={mode:'live',status:'unavailable'};
const accepted=[requireMarketChain(live)===live,requireMarketChain(unavailable)===unavailable];
const before=visibleStrikeRows(rows,2,150);
const moved=rows.map(row=>({...row,atm:row.strike===190,ltp:row.ltp+3}));
const after=visibleStrikeRows(moved,2,150);
const viewport={scrollTop:300,scrollLeft:77,scrollWidth:1600,clientWidth:600,clientHeight:400,getBoundingClientRect:()=>({top:100,height:400})};
const row={offsetTop:12345,getBoundingClientRect:()=>({top:650,height:50})};
centerChainViewport(viewport,row);
const clipped={scrollTop:0,scrollLeft:77,scrollWidth:200,clientWidth:600,clientHeight:400,getBoundingClientRect:()=>({top:100,height:400})};
centerChainViewport(clipped,{getBoundingClientRect:()=>({top:0,height:50})});
console.log(JSON.stringify({
 rejected,accepted,before:before.map(row=>row.strike),after:after.map(row=>row.strike),updatedValues:after.map(row=>row.ltp),
 all:visibleStrikeRows(rows,null,150).map(row=>row.strike),missing:visibleStrikeRows(rows,2,null).map(row=>row.strike),
 first:visibleStrikeRows(rows,2,100).map(row=>row.strike),last:visibleStrikeRows(rows,2,200).map(row=>row.strike),
 empty:visibleStrikeRows([],2,150),nearest:visibleStrikeRows(rows,1,151).map(row=>row.strike),
 sourceUntouched:rows.every((row,i)=>row.strike===100+i*10&&row.ltp===50+i),
 viewport:{scrollTop:viewport.scrollTop,scrollLeft:viewport.scrollLeft,clientHeight:viewport.clientHeight,clientWidth:viewport.clientWidth,scrollWidth:viewport.scrollWidth},
 clipped:{scrollTop:clipped.scrollTop,scrollLeft:clipped.scrollLeft}
}));
"""
    result = subprocess.run(
        [node_runtime(), '-e', script, str(ROOT / 'frontend/node_modules/typescript'), str(ROOT / 'frontend/src/chainView.ts')],
        capture_output=True, text=True, encoding='utf-8', check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr[:4000]
    return json.loads(result.stdout)


def test_quote_updates_keep_the_selected_strike_window(view_results):
    assert view_results['before'] == [130, 140, 150, 160, 170]
    assert view_results['after'] == view_results['before']
    assert view_results['updatedValues'] == [56, 57, 58, 59, 60]
    assert view_results['sourceUntouched']


def test_all_strikes_and_missing_anchor_preserve_available_rows(view_results):
    assert view_results['all'] == list(range(100, 201, 10))
    assert view_results['missing'] == view_results['all']
    assert view_results['empty'] == []


def test_edge_windows_never_invent_contract_strikes(view_results):
    assert view_results['first'] == [100, 110, 120]
    assert view_results['last'] == [180, 190, 200]
    assert view_results['nearest'] == [140, 150, 160]


def test_atm_centering_uses_inner_viewport_geometry(view_results):
    # Page/table offsets must not be mistaken for the nested scroller's coordinates.
    assert view_results['viewport'] == {
        'scrollTop': 675, 'scrollLeft': 500,
        'clientHeight': 400, 'clientWidth': 600, 'scrollWidth': 1600,
    }


def test_atm_centering_clamps_small_content_to_zero(view_results):
    assert view_results['clipped'] == {'scrollTop': 0, 'scrollLeft': 0}


def test_option_chain_initial_render_has_no_sample_switch():
    script = r"""
const Module=require('node:module'),fs=require('node:fs');
const esbuild=Module.createRequire(fs.realpathSync(process.argv[1]))('esbuild'),root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import OptionChain from './src/OptionChain';
console.log(JSON.stringify(renderToStaticMarkup(React.createElement(OptionChain,{paperTrades:[],onBuildStrategy:()=>{throw Error('Rendering must not create a trade')},onNotify:()=>{}}))));`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'chain-view-ssr-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',loader:{'.css':'empty'},define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/chain-view-ssr-test.cjs');mod._compile(output,root+'/chain-view-ssr-test.cjs');
"""
    result = subprocess.run(
        [node_runtime(), '-e', script, str(ROOT / 'frontend/node_modules/vite/package.json'), str(ROOT / 'frontend')],
        capture_output=True, text=True, encoding='utf-8', check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr[:4000]
    html = json.loads(result.stdout)
    assert 'Sample chain' not in html
    assert 'SYNTHETIC SAMPLE' not in html
    assert 'Kotak Neo market data' in html
    assert 'Go to ATM' in html
    assert 'Update contracts' in html
    assert 'data-chain-style="aurora"' in html
    for label in ('Classic', 'Terminal', 'Aurora'):
        assert re.search(r'<button[^>]*>' + label + r'</button>', html)


def test_market_only_chain_rejects_both_sample_response_forms(view_results):
    assert view_results['rejected'] == [
        {'threw': True, 'untouched': True}, {'threw': True, 'untouched': True},
    ]
    assert view_results['accepted'] == [True, True]


@pytest.fixture(scope='module')
def summary_renders():
    script = r"""
const Module=require('node:module'),fs=require('node:fs');
const esbuild=Module.createRequire(fs.realpathSync(process.argv[1]))('esbuild'),root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import Summary from './src/OptionChainSummary';
const base={broker:'kotak',demo:false,underlying:'NIFTY',exchange:'NSE',status:'OPEN',unrealized_pnl:125,realized_pnl:-25,net_premium:-1000,net_pnl:100,legs:[{exchange:'NSE',open_lots:1}]};
const win={...base,status:'CLOSED',unrealized_pnl:0,realized_pnl:200,net_premium:50,net_pnl:195,legs:[{exchange:'NSE',open_lots:0}]};
const loss={...win,realized_pnl:-100,net_premium:-25,net_pnl:-105};
const huge={...base,unrealized_pnl:999999,realized_pnl:999999,net_premium:999999};
const rows=[base,win,loss,{...huge,demo:true},{...huge,broker:'other'},{...huge,underlying:'BANKNIFTY'},{...huge,exchange:'BSE',legs:[{exchange:'BSE',open_lots:1}]},{...huge,legs:[{exchange:'NSE',open_lots:1},{exchange:'BSE',open_lots:1}]},{...huge,demo:undefined}];
const props={summary:{atm_iv:12,days_to_expiry:7,lot_size:65,pcr:1.2},rows:[],selected:{symbol:'NIFTY',exchange:'NSE'},mode:'live',broker:'kotak'};
const render=extra=>renderToStaticMarkup(React.createElement(Summary,{...props,...extra}));
console.log(JSON.stringify({loaded:render({trades:rows}),empty:render({trades:[]}),unavailable:render({trades:undefined}),missingValues:render({trades:[{...base,unrealized_pnl:null}]}),unselected:render({trades:rows,selected:null}),sample:render({trades:rows,mode:'sample'})}));`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'chain-summary-ssr-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',loader:{'.css':'empty'},define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/chain-summary-ssr-test.cjs');mod._compile(output,root+'/chain-summary-ssr-test.cjs');
"""
    result = subprocess.run(
        [node_runtime(), '-e', script, str(ROOT / 'frontend/node_modules/vite/package.json'), str(ROOT / 'frontend')],
        capture_output=True, text=True, encoding='utf-8', check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr[:4000]
    return json.loads(result.stdout)


def summary_cards(rendered):
    # Read the actual rendered labels and values, rather than recalculate the model.
    return {
        html_lib.unescape(label): html_lib.unescape(value)
        for label, value in re.findall(r'<span class="ocs-label">(.*?)</span><strong[^>]*>(.*?)</strong>', rendered)
    }


def test_live_account_cards_exclude_sample_other_broker_and_exchange_records(summary_renders):
    cards = summary_cards(summary_renders['loaded'])
    assert cards['Unrealized gross P&L'] == '₹125.00'
    assert cards['Realized gross P&L'] == '₹75.00'
    assert cards['Net option premium'] == '₹975.00'
    assert cards['Open positions'] == '1'
    assert cards['Recent win rate'] == '50%'
    assert 'Loaded closed trades only' in summary_renders['loaded']
    assert 'Selected underlying · loaded paper records' in summary_renders['loaded']
    assert all(cards[label] == '—' for label in ('Max pain', 'Resistance', 'Support'))


def test_empty_loaded_accounts_are_distinct_from_unavailable_records(summary_renders):
    empty = summary_cards(summary_renders['empty'])
    assert empty['Unrealized gross P&L'] == '₹0.00'
    assert empty['Open positions'] == '0'
    assert empty['Recent win rate'] == '—'
    for name in ('unavailable', 'unselected'):
        cards = summary_cards(summary_renders[name])
        assert all(cards[label] == '—' for label in (
            'Unrealized gross P&L', 'Realized gross P&L', 'Open positions', 'Net option premium', 'Recent win rate',
        ))
    assert summary_cards(summary_renders['missingValues'])['Unrealized gross P&L'] == '—'


def test_sample_account_figures_cannot_be_mixed_with_live_records(summary_renders):
    cards = summary_cards(summary_renders['sample'])
    assert cards['Unrealized gross P&L'] == '₹9,99,999.00'
    assert cards['Open positions'] == '1'
    assert cards['Recent win rate'] == '—'


@pytest.fixture(scope='module')
def basket_renders():
    script = r"""
const Module=require('node:module'),fs=require('node:fs');
const esbuild=Module.createRequire(fs.realpathSync(process.argv[1]))('esbuild'),root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import Preview from './src/ChainBasketPreview';
const specs=[['SELL',22700,'CE',185.10],['SELL',22700,'PE',146.50],['BUY',22850,'CE',111.35],['BUY',22950,'CE',286.20],['BUY',22550,'PE',91.95]];
const legs=specs.map(([side,strike,option_type,entry_price])=>({side,strike,option_type,entry_price,broker:'kotak',exchange:'NSE',underlying:'NIFTY',expiry:'2026-11-03',lots:1,open_lots:1,lot_size:65}));
const render=(rows,reference=22700,stale=false)=>renderToStaticMarkup(React.createElement(Preview,{legs:rows,reference,stale}));
console.log(JSON.stringify({normal:render(legs),farWindow:render(legs,100000),missing:render(legs.map((leg,i)=>i?leg:{...leg,entry_price:undefined})),mixed:render(legs.map((leg,i)=>i?leg:{...leg,expiry:'2026-11-10'})),stale:render(legs,22700,true),mcx:render([{...legs[0],exchange:'MCX',underlying:'CRUDEOIL',price_multiplier:undefined}])}));`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'chain-basket-ssr-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',loader:{'.css':'empty'},define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/chain-basket-ssr-test.cjs');mod._compile(output,root+'/chain-basket-ssr-test.cjs');
"""
    result = subprocess.run(
        [node_runtime(), '-e', script, str(ROOT / 'frontend/node_modules/vite/package.json'), str(ROOT / 'frontend')],
        capture_output=True, text=True, encoding='utf-8', check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr[:4000]
    return json.loads(result.stdout)


def basket_limits(rendered):
    return {
        label: html_lib.unescape(value)
        for label, value in re.findall(r'<span>(Maximum profit|Maximum loss|Breakevens) <strong[^>]*>(.*?)</strong>', rendered)
    }


def test_basket_shows_exact_unlimited_risk_independently_of_chart_range(basket_renders):
    limits = basket_limits(basket_renders['normal'])
    assert limits['Maximum profit'] == 'Unlimited'
    assert limits['Maximum loss'] == '₹20,013.50'
    assert limits == basket_limits(basket_renders['farWindow'])
    assert 'fees excluded' in basket_renders['normal']
    assert 'chart range does not determine theoretical limits' in basket_renders['normal']


def test_basket_missing_prices_and_mixed_expiries_do_not_invent_payoff(basket_renders):
    for key in ('missing', 'mixed', 'mcx'):
        assert 'Payoff unavailable' in basket_renders[key]
        assert basket_limits(basket_renders[key]) == {}
        assert '<path' not in basket_renders[key]
    assert 'Entry premiums must be finite and nonnegative.' in basket_renders['missing']
    assert 'Combined expiry payoff requires one broker, exchange, underlying and expiry.' in basket_renders['mixed']
    assert 'verified monetary units' in basket_renders['mcx']


def test_basket_marks_last_known_quotes_without_changing_theoretical_formula(basket_renders):
    assert 'Last-known quotes' in basket_renders['stale']
    assert basket_limits(basket_renders['stale']) == basket_limits(basket_renders['normal'])
