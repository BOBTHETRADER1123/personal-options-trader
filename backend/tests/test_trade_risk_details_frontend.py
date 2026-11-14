"""Render the real Risk tab: booked results must not become future expiry risk."""
import json
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def risk_views():
    node = shutil.which('node')
    bundled = Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node = node or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT/'frontend/node_modules/vite').is_dir():
        pytest.skip('Frontend dependencies and Node are required.')
    script = r"""
const Module=require('node:module'),esbuild=Module.createRequire(require('node:fs').realpathSync(process.argv[1]))('esbuild');const root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import TradeRiskDetails from './src/TradeRiskDetails';
const closed={status:'CLOSED',realized_pnl:250,charges:10,net_pnl:240,legs:[{underlying:'CRUDEOIL',option_type:'PE',open_lots:0,status:'closed',lots:12,lot_size:100}],risk:{supported:true,max_profit:240,max_loss:0,net_premium:0,breakevens:[]}};
const pending={status:'PENDING',realized_pnl:0,charges:0,net_pnl:0,legs:[{option_type:'CE',open_lots:0,status:'pending',lots:1}],risk:{supported:false,reason:'Pending prices have not been valued.'}};
const active={status:'OPEN',legs:[{option_type:'CE',open_lots:1,status:'open',lots:1}],risk:{supported:true,profit_unlimited:true,loss_unlimited:false,max_loss:1000,net_premium:0,breakevens:[100]}};
const cases={closed,cancelled:{...closed,status:'CANCELLED',realized_pnl:0,charges:0,net_pnl:0},pending,pendingValued:{...pending,risk:{supported:true,max_profit:200,max_loss:100,net_premium:-100,breakevens:[110]}},mixed:{...active,risk:{supported:false,reason:'Mixed expiries have no single combined expiry payoff.'}},active,missing:{...closed,realized_pnl:null,charges:null,net_pnl:null}};
console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(([name,trade])=>[name,renderToStaticMarkup(React.createElement(TradeRiskDetails,{trade}))]))));`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'risk-details-ssr-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/risk-details-ssr-test.cjs');mod._compile(output,root+'/risk-details-ssr-test.cjs');
"""
    result = subprocess.run(
        [node, '-e', script, str(ROOT/'frontend/node_modules/vite/package.json'), str(ROOT/'frontend')],
        capture_output=True, text=True, encoding='utf-8', check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr[:4000]
    return {name: unescape(re.sub('<[^>]*>', ' ', html)) for name, html in json.loads(result.stdout).items()}


def test_closed_results_are_not_presented_as_expiry_risk(risk_views):
    view = risk_views['closed']
    assert 'Final realized gross P&L' in view and '₹250.00' in view
    assert 'Recorded charges' in view and '₹10' in view
    assert 'Final net P&L' in view and '₹240.00' in view
    assert view.count('Not applicable · no remaining exposure') == 2
    assert 'Maximum expiry profit after charges' not in view
    assert 'Maximum expiry loss after charges' not in view
    assert 'credit' not in view and 'Broker margin unavailable' not in view


def test_cancelled_trade_has_no_remaining_exposure_or_invented_premium(risk_views):
    view = risk_views['cancelled']
    assert 'paper trade is cancelled' in view
    assert 'Final net P&L' in view and 'No remaining exposure' in view
    assert 'credit' not in view and 'Net option premium' not in view


def test_pending_order_is_not_an_executed_position(risk_views):
    view = risk_views['pending']
    assert 'Pending orders are not filled' in view and 'no executed exposure yet' in view
    assert 'Recorded net P&L' in view and 'Final net P&L' not in view
    assert 'Pending prices have not been valued' in view
    assert 'Maximum expiry profit after charges' not in view and not re.search(r'₹0\.00\s+credit', view)
    potential = risk_views['pendingValued']
    assert 'Potential maximum expiry profit if filled' in potential
    assert 'Potential maximum expiry loss if filled' in potential
    assert 'Potential net option premium if filled' in potential
    assert 'Potential breakevens if filled' in potential


def test_active_mixed_expiry_trade_keeps_margin_panel_visible(risk_views):
    view = risk_views['mixed']
    assert 'Mixed expiries have no single combined expiry payoff' in view
    assert 'Broker margin unavailable' in view
    assert 'Exchange SPAN margin' in view and 'Final capital required' in view
    assert 'Maximum expiry loss after charges' not in view
    assert 'No remaining exposure' not in view


def test_active_analytical_risk_is_preserved_without_zero_credit_label(risk_views):
    view = risk_views['active']
    assert 'Maximum expiry profit after charges' in view and 'Unlimited' in view
    assert 'Maximum expiry loss after charges' in view and '₹1,000.00' in view
    assert 'Broker margin unavailable' in view and 'No net premium' in view
    assert not re.search(r'₹0\.00\s+credit', view)


def test_missing_recorded_results_are_not_displayed_as_zero(risk_views):
    view = risk_views['missing']
    assert view.count('Unavailable') == 3
    assert '₹0.00' not in view
