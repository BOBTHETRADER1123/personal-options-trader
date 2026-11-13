"""Render category views against bounded records and authoritative backend totals.

These tests never call a broker, submit an order, or read a trading database.
"""
import html as html_lib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]


def node_runtime():
    node=shutil.which('node')
    bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node=node or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT/'frontend/node_modules/vite').is_dir():
        pytest.skip('Node and installed frontend dependencies are required.')
    return node


@pytest.fixture(scope='module')
def asset_renders():
    script=r"""
const Module=require('node:module'),fs=require('node:fs');
const esbuild=Module.createRequire(fs.realpathSync(process.argv[1]))('esbuild'),root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import {CategoryPnlCards,CategoryDashboard,CombinedAnalytics,tradeCategory} from './src/AssetViews';
const base={broker:'kotak',status:'OPEN',demo:false,net_pnl:10,net_premium:0,created_at:'2026-11-12T04:00:00Z'};
const stock={...base,id:'stock-one',name:'EQUITY_SENTINEL',underlying:'ACME',legs:[{option_type:'EQ',exchange:'NSE',open_lots:5,expiry:null}]};
const option={...base,id:'option-one',name:'FNO_SENTINEL',underlying:'NIFTY',net_pnl:-5,legs:[{option_type:'CE',exchange:'NSE',open_lots:1,expiry:'2026-11-19'}]};
const commodity={...base,id:'commodity-one',name:'MCX_SENTINEL',underlying:'GOLD',net_pnl:7,legs:[{option_type:'FUT',exchange:'MCX',open_lots:1,expiry:'2026-11-19'}]};
const records=[stock,option,commodity,{...option,id:'closed-one',name:'CLOSED_SENTINEL',status:'CLOSED'},{...stock,id:'pending-one',name:'PENDING_SENTINEL',status:'PENDING'}];
const summary={stocks:{net_pnl:1000,realized_pnl:800,unrealized_pnl:250,charges:50,active_trades:2,closed_trades:8},fno:{net_pnl:-200,realized_pnl:-100,unrealized_pnl:-80,charges:20,active_trades:1,closed_trades:4},commodities:{net_pnl:300,realized_pnl:200,unrealized_pnl:110,charges:10,active_trades:1,closed_trades:3},total:{net_pnl:1100,realized_pnl:900,unrealized_pnl:280,charges:80,active_trades:4,closed_trades:15}};
const render=(component,props)=>renderToStaticMarkup(React.createElement(component,props));
const noop=()=>{throw Error('Rendering must not submit a trade or run an action')};
const dashboards=Object.fromEntries(['stocks','fno','commodities'].map(category=>[category,render(CategoryDashboard,{category,summary,trades:records,onSelect:noop,onTrade:noop,onChain:noop,onHistory:noop})]));
console.log(JSON.stringify({cards:render(CategoryPnlCards,{summary}),unavailable:render(CategoryPnlCards,{}),dashboards,classifications:records.map(tradeCategory),authoritative:tradeCategory({...stock,category:'fno'}),analytics:render(CombinedAnalytics,{summary,timeline:[]})}));`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'asset-views-ssr-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',loader:{'.css':'empty'},define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/asset-views-ssr-test.cjs');mod._compile(output,root+'/asset-views-ssr-test.cjs');
"""
    result=subprocess.run([node_runtime(),'-e',script,str(ROOT/'frontend/node_modules/vite/package.json'),str(ROOT/'frontend')],capture_output=True,text=True,encoding='utf-8',check=False,timeout=30)
    assert result.returncode==0,result.stderr[:4000]
    return json.loads(result.stdout)


def card_values(rendered):
    values={}
    for label,value in re.findall(r'<div class="stat-top"><span>(.*?)</span>.*?<div class="stat-value[^>]*>(.*?)</div>',rendered):
        values[html_lib.unescape(label)]=html_lib.unescape(re.sub(r'<[^>]*>','',value))
    return values


def test_dashboard_cards_use_complete_backend_totals_not_bounded_loaded_trades(asset_renders):
    assert card_values(asset_renders['cards'])=={
        'Stocks P&L':'+₹1,000.00','F&O P&L':'−₹200.00',
        'Commodities P&L':'+₹300.00','Total P&L':'+₹1,100.00',
    }
    assert 'Recorded net paper P&amp;L · charges included' in asset_renders['cards']


def test_missing_category_totals_are_unavailable_instead_of_fabricated_zero(asset_renders):
    cards=card_values(asset_renders['unavailable'])
    assert set(cards)=={'Stocks P&L','F&O P&L','Commodities P&L','Total P&L'}
    assert set(cards.values())=={'Unavailable'}
    assert '₹0.00' not in asset_renders['unavailable']


def test_each_trade_is_assigned_to_exactly_one_of_three_categories(asset_renders):
    assert asset_renders['classifications']==['stocks','fno','commodities','fno','stocks']
    assert set(asset_renders['classifications'])=={'stocks','fno','commodities'}
    # Server classification remains authoritative for already classified records.
    assert asset_renders['authoritative']=='fno'


def test_category_positions_do_not_duplicate_other_assets_or_closed_trades(asset_renders):
    expected={'stocks':['EQUITY_SENTINEL','PENDING_SENTINEL'],'fno':['FNO_SENTINEL'],'commodities':['MCX_SENTINEL']}
    for category,rendered in asset_renders['dashboards'].items():
        visible=expected[category]
        for name in ('EQUITY_SENTINEL','PENDING_SENTINEL','FNO_SENTINEL','MCX_SENTINEL'):
            assert (f'<strong>{name}</strong>' in rendered)==(name in visible)
        assert 'CLOSED_SENTINEL' not in rendered


def test_analytics_preserves_the_same_authoritative_category_totals(asset_renders):
    assert card_values(asset_renders['analytics'])==card_values(asset_renders['cards'])
    assert 'No recorded performance yet' in asset_renders['analytics']
    assert 'Recorded net results from the backend' in asset_renders['analytics']
