"""Actual frontend helper/SSR regressions; no network or broker calls."""
import json
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture(scope='module')
def rendered():
    node=shutil.which('node')
    if not node:pytest.skip('Node is required')
    script=r"""
const Module=require('node:module'),fs=require('node:fs');
const esbuild=Module.createRequire(fs.realpathSync(process.argv[1]))('esbuild'),root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';
import Order from './src/QuickPaperOrder';import * as data from './src/watchlistData';
const at=new Date().toISOString(),contract={id:'kotak:test',instrument_id:'kotak:test',broker:'kotak',exchange:'NSE',segment:'nse_cm',underlying:'TEST',symbol:'TEST-EQ',instrument_type:'stock',option_type:'EQ',lot_size:1,expiry:null,demo:false};
const quote={...contract,ltp:100,bid:99.9,ask:100.1,previous_close:90,received_at:at};
const item={instrument_id:contract.id,contract,quote};
const newest={...quote,ltp:101,received_at:new Date(Date.now()+1000).toISOString()};
const contaminated=[{...newest,broker:'other'},{...newest,demo:true},{...newest,instrument_id:'other'},{...newest,exchange:'MCX'},{...newest,segment:'mcx_fo'},{...newest,received_at:'invalid'},{...newest,underlying:'RECYCLED'},{...newest,expiry:'2030-11-30'},{...newest,option_type:'CE'},{...newest,lot_size:65}].map(value=>({...value,ltp:999,received_at:value.received_at==='invalid'?'invalid':new Date(Date.now()+2000).toISOString()})).concat([newest]);
const original=[{instrument_id:'a'},{instrument_id:'b'},{instrument_id:'c'}];
const success={mode:'PAPER',results:[{broker:'kotak',success:true,trade_id:'paper-1',trade:{id:'paper-1',status:'open'}}]};
const failures=[{}, {...success,mode:'REAL'},{...success,results:[{broker:'kotak',success:false,trade_id:'paper-1',trade:{id:'paper-1'}}]},{...success,results:[{broker:'kotak',success:true,trade_id:'paper-1',trade:{id:'mismatch'}}]}];
const props={contract,side:'BUY',settings:{demo_mode:false,fill_basis:'bid_ask',slippage_bps:0,brokerage_per_order:20,charges_percent:.05,stale_after_seconds:10},positions:[],onClose:()=>{},onSuccess:()=>{},onManage:()=>{},notify:()=>{}};
const render=extra=>renderToStaticMarkup(React.createElement(Order,{...props,quote,...extra}));
console.log(JSON.stringify({newest:data.latestWatchQuote(item,contaminated)?.ltp,stale:[data.watchQuoteStale(null,10),data.watchQuoteStale({...quote,stale:true},10),data.watchQuoteStale({...quote,received_at:new Date(Date.now()-20000).toISOString()},10),data.watchQuoteStale(quote,10)],changeMissing:data.watchChange({...quote,previous_close:null}),order:data.reorderedWatchIds(original,'c','a'),untouched:original.map(v=>v.instrument_id),success:data.successfulPaperTrade(success)?.id,rejections:failures.map(data.successfulPaperTrade),live:render({}),missing:render({quote:null}),staleOrder:render({quote:{...quote,received_at:new Date(Date.now()-20000).toISOString()}}),demoOrder:render({settings:{...props.settings,demo_mode:true}}),fallback:render({quote:{...quote,bid:null,ask:null}}),equitySellEmpty:render({side:'SELL'}),equitySellOwned:render({side:'SELL',positions:[{id:'held-equity',name:'Owned equity',legs:[{instrument_id:contract.id,side:'BUY',open_lots:12}]}]})}));`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'watchlist-helper-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',loader:{'.css':'empty'},define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/watchlist-helper-test.cjs');mod._compile(output,root+'/watchlist-helper-test.cjs');
"""
    result=subprocess.run([node,'-e',script,str(ROOT/'frontend/node_modules/vite/package.json'),str(ROOT/'frontend')],capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stderr[:3000]
    return json.loads(result.stdout)

def test_watchlist_prices_are_current_and_independent(rendered):
    assert rendered['newest']==101
    assert rendered['stale']==[True,True,True,False]
    assert rendered['changeMissing']=={'change':None,'percent':None}

def test_manual_reorder_does_not_mutate_the_source(rendered):
    assert rendered['order']==['c','a','b']
    assert rendered['untouched']==['a','b','c']

def test_only_a_verified_paper_result_reports_success(rendered):
    assert rendered['success']=='paper-1'
    assert rendered['rejections']==[None,None,None,None]

def confirm_disabled(html):
    from html.parser import HTMLParser
    class Buttons(HTMLParser):
        def __init__(self):super().__init__();self.attrs=None;self.current='';self.result=None
        def handle_starttag(self,tag,attrs):
            if tag=='button':self.attrs=dict(attrs);self.current=''
        def handle_data(self,text):
            if self.attrs is not None:self.current+=text
        def handle_endtag(self,tag):
            if tag=='button' and self.attrs is not None:
                if 'Confirm Paper Trade' in self.current:self.result='disabled' in self.attrs
                self.attrs=None
    parser=Buttons();parser.feed(html)
    return parser.result

def test_order_confirmation_requires_fresh_genuine_quotes_and_spread_review(rendered):
    assert confirm_disabled(rendered['live']) is False
    for name in ('missing','staleOrder','demoOrder','fallback'):
        assert confirm_disabled(rendered[name]) is True,name
    assert 'Quantity' in rendered['live'] and 'shares' in rendered['live']
    assert 'No real broker order is sent.' in rendered['live']


def test_equity_sell_manages_owned_shares_without_opening_a_short(rendered):
    empty=rendered['equitySellEmpty'];owned=rendered['equitySellOwned']
    assert 'No equity shares to sell' in empty
    assert 'Manage shares to sell' in owned and '12 shares held' in owned
    assert 'market exits' in owned
    for html in (empty,owned):
        assert 'Confirm Paper Trade' not in html
        assert 'New paper sell order' not in html
    assert 'value="SELL"' not in rendered['live']
    assert 'value="LIMIT"' in rendered['live']
