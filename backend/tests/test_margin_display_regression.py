"""The existing risk display must not promote heuristic or single-order amounts."""
import json
import subprocess
from test_frontend_review import ROOT, node_runtime


def test_margin_components_require_verified_fresh_basket_sources():
    script=r"""
const Module=require('node:module'),esbuild=Module.createRequire(require('node:fs').realpathSync(process.argv[1]))('esbuild'),root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import Breakdown from './src/MarginBreakdown';import {brokerMarginValue,riskWarnings} from './src/tradeMath';
const base={estimated_margin:12566.38,hedge_benefit:83871.12,broker_margin:{status:'unavailable',verified:false,total_margin:12566.38,hedge_benefit:83871.12,reason:'No supported basket adapter',premium_paid:5000,premium_received:15000,as_of:'2026-11-14T03:45:00Z',provider_request_performed:false},exchange_margin:{status:'unavailable',verified:false,span_margin:99999,reason:'Current risk parameters missing'}};
const verified={status:'verified',verified:true,scope:'isolated_paper_basket',freshness:'fresh',total_margin:12345.67,span_margin:10000,elm:1000,additional_margin:1345.67,hedge_benefit:2222.22,final_capital_required:15000,source:'Synthetic test basket adapter',as_of:'2026-10-30T03:45:00Z'};
const render=(value:any)=>renderToStaticMarkup(React.createElement(Breakdown,{source:value}));
const single={broker_margin:{...verified,scope:'single_order'}};
const stale={broker_margin:{...verified,freshness:'stale'}};
const warnings=riskWarnings({validation_warnings:['Quote stale','Quote stale'],premium_warnings:['Premium ordering inconsistent']});
console.log(JSON.stringify({html:render(base),single:brokerMarginValue(single),stale:brokerMarginValue(stale),basket:brokerMarginValue({broker_margin:verified}),verifiedHtml:render({broker_margin:verified}),warnings}));`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'margin-display-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/margin-display-test.cjs');mod._compile(output,root+'/margin-display-test.cjs');
"""
    result=subprocess.run([node_runtime(),'-e',script,str(ROOT/'frontend/node_modules/vite/package.json'),str(ROOT/'frontend')],capture_output=True,text=True,check=False,timeout=30)
    assert result.returncode==0,result.stderr[:4000]
    result=json.loads(result.stdout)
    html=result['html']
    for label in ('Exchange SPAN margin','Exchange ELM','Exchange additional / special margin','Total calculated exchange margin','Broker margin unavailable','Verified hedge benefit','Final capital required'):
        assert label in html
    assert '12,566.38' not in html and '83,871.12' not in html and '99,999' not in html
    assert 'Current risk parameters missing' in html and 'Parameter file: Unavailable' in html
    assert result['single'] is None and result['stale'] is None
    assert result['basket']==12345.67
    assert '12,345.67' in result['verifiedHtml'] and '09:15:00 IST' in result['verifiedHtml']
    assert result['warnings']==['Quote stale','Premium ordering inconsistent']

    assert 'Option premium paid' in html and '5,000.00' in html
    assert 'Option premium received' in html and '15,000.00' in html
    assert 'Availability assessed' in html and '14 Nov, 09:15:00 IST' in html
    assert 'Quoted:' not in html and 'Calculated:' not in html
