"""Render real review controls and exercise the quote-change confirmation guard."""
import json
import shutil
import subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]

def node_runtime():
    node=shutil.which('node')
    bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node=node or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT/'frontend/node_modules/vite').is_dir():pytest.skip('Frontend dependencies and Node are required.')
    return node

def test_material_quote_changes_require_a_new_review():
    script=r"""
const fs=require('node:fs'),Module=require('node:module');
const ts=require(process.argv[1]);const path=process.argv[2];
const output=ts.transpileModule(fs.readFileSync(path,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}});
const mod=new Module(path);mod._compile(output.outputText,path);
const base={fill_basis:'bid_ask',slippage_bps:0,brokerage_per_order:20,charges_percent:.05,previews:[{contract:{id:'kotak:1'},side:'BUY',quantity:100,pendingLimit:false,wideSpread:false,fallbackToLtp:false,previewPrice:100}]};
function changed(mutate){const next=structuredClone(base);mutate(next);return mod.exports.reviewNeedsAnotherConfirmation(base,next);}
const results=[changed(n=>{}),changed(n=>n.previews[0].previewPrice=100.5),changed(n=>n.previews[0].previewPrice=102),changed(n=>n.previews[0].wideSpread=true),changed(n=>n.previews[0].fallbackToLtp=true),changed(n=>n.previews[0].pendingLimit=true),changed(n=>n.brokerage_per_order=40),changed(n=>n.previews[0].contract.id='kotak:2')];
process.stdout.write(JSON.stringify(results));
"""
    result=subprocess.run([node_runtime(),'-e',script,str(ROOT/'frontend/node_modules/typescript'),str(ROOT/'frontend/src/executionPreview.ts')],capture_output=True,text=True,check=False,timeout=30)
    assert result.returncode==0,result.stderr[:4000]
    assert json.loads(result.stdout)==[False,False,True,True,True,True,True,True]

def test_wide_spread_review_renders_acknowledgment_and_disables_confirmation():
    script=r"""
const Module=require('node:module'),esbuild=Module.createRequire(require('node:fs').realpathSync(process.argv[1]))('esbuild');const root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import Review from './src/PaperTradeReview';
const leg={contract:{id:'kotak:1',symbol:'TEST',underlying:'TEST',strike:100,option_type:'CE',expiry:'2035-01-01',exchange:'NSE'},side:'BUY',lots:1,quantity:50,order_type:'MARKET',ltp:100,bid:90,ask:110,fillPrice:110,estimatedCharges:22.75,spreadPercent:20,wideSpread:true};
const base={payload:{name:'Synthetic review test',brokers:['kotak']},analysis:{net_premium:-5500,max_profit:null,max_loss:5500,estimated_margin:5500},previews:[leg],demo:false,fill_basis:'bid_ask',slippage_bps:0};
const render=r=>renderToStaticMarkup(React.createElement(Review,{review:r,addingTo:false,busy:false,onClose:()=>{},onRefresh:()=>{},onConfirm:()=>{throw Error('SSR must not create trades')}}));
console.log(JSON.stringify([render(base),render({...base,previews:[{...leg,bid:99,ask:101,wideSpread:false,spreadPercent:2}]}),render({...base,previews:[{...leg,pendingLimit:true,fillPrice:null,limitPrice:80,estimatedCharges:0}]})]));`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'review-ssr-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/review-ssr-test.cjs');mod._compile(output,root+'/review-ssr-test.cjs');
"""
    result=subprocess.run([node_runtime(),'-e',script,str(ROOT/'frontend/node_modules/vite/package.json'),str(ROOT/'frontend')],capture_output=True,text=True,check=False,timeout=30)
    assert result.returncode==0,result.stderr[:4000]
    wide,narrow,pending=json.loads(result.stdout)
    assert 'Check the spread before confirming' in wide and 'type="checkbox"' in wide
    assert 'disabled=""' in wide.split('Confirm paper')[0].rsplit('<button',1)[1]
    assert 'Check the spread before confirming' not in narrow
    assert 'disabled=""' not in narrow.split('Confirm paper')[0].rsplit('<button',1)[1]
    assert 'Pending limit' in pending and 'not filled' in pending
