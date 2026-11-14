"""Exercise real single-order UI guards without broker or application calls."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def checks():
    node = shutil.which('node')
    bundled = Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node = node or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT/'frontend/node_modules/vite').is_dir():
        pytest.skip('Frontend dependencies and Node are required.')
    script = r"""
const Module=require('node:module'),esbuild=Module.createRequire(require('node:fs').realpathSync(process.argv[1]))('esbuild');const root=process.argv[2];
const source=`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import Check,{kotakMarginLegs as legs,kotakMarginContextFingerprint as signature,visibleKotakMarginAmounts as amounts,safeKotakMarginError as safeError,kotakMarginRequestDigest as digest} from './src/KotakMarginCheck';
const now=Date.parse('2026-11-14T08:00:00Z');
const leg={id:'leg-first',instrument_id:'kotak:mcx_fo:123',broker:'kotak',demo:false,token:'123',segment:'mcx_fo',expiry:'2035-01-01',symbol:'NATURALGAS TEST',option_type:'CE',side:'SELL',lots:3,open_lots:2,lot_size:1250,price_multiplier:2,current_price:12.5,entry_price:12,status:'open'};
const base={id:'trade-one',broker:'kotak',demo:false,status:'OPEN',legs:[leg]};
const selected=legs(base,now)[0];
const result={broker:'kotak',status:'single_order_response',verified:true,scope:'single_order_real_account_diagnostic',paper_portfolio_verified:false,provider_request_performed:true,as_of:new Date(now).toISOString(),request_fingerprint:'a'.repeat(64),request:{exchange_segment:'mcx_fo',instrument_token:'123',transaction_type:'S',product:'NRML',order_type:'L',quantity:'2500',price:'12.5'},order_margin:25000,required_margin:0};
const responseMutations={basket:r=>r.scope='basket',unverified:r=>r.verified=false,paper:r=>r.paper_portfolio_verified=true,stale:r=>r.as_of=new Date(now-60000).toISOString(),future:r=>r.as_of=new Date(now+6000).toISOString(),wrongToken:r=>r.request.instrument_token='124',wrongQty:r=>r.request.quantity='5000',wrongSide:r=>r.request.transaction_type='B',wrongPrice:r=>r.request.price='12.6',wrongProduct:r=>r.request.product='MIS',badAmount:r=>r.order_margin=NaN,booleanAmount:r=>r.required_margin=false,noFingerprint:r=>r.request_fingerprint='',notPerformed:r=>r.provider_request_performed=false};
const rejected=Object.fromEntries(Object.entries(responseMutations).map(([name,mutate])=>{const row=structuredClone(result);mutate(row);return [name,amounts(row,selected,'NRML',now)===null];}));
const excluded={demo:{...base,demo:true},retired:{...base,broker_retired:true},closed:{...base,status:'CLOSED'},cancelled:{...base,status:'CANCELLED'},pending:{...base,legs:[{...leg,status:'pending',open_lots:0}]},expired:{...base,legs:[{...leg,expiry:'2026-11-13'}]},stock:{...base,legs:[{...leg,option_type:'EQ'}]},legacy:{...base,legs:[{...leg,broker:'fivepaisa'}]},disabled:{...base,legs:[{...leg,enabled:false}]},invalidPrice:{...base,legs:[{...leg,current_price:0,entry_price:0}]} };
const exclusionChecks=Object.fromEntries(Object.entries(excluded).map(([name,trade])=>[name,legs(trade,now).length===0]));
const baseline=signature(base,selected,'NRML');
const edited=(mutate)=>{const source=structuredClone(base);mutate(source);return signature(source,legs(source,now)[0],'NRML')!==baseline;};
const invalidation={lots:edited(t=>t.legs[0].open_lots=1),price:edited(t=>t.legs[0].current_price=12.6),replacement:edited(t=>{t.legs[0].instrument_id='kotak:mcx_fo:124';t.legs[0].token='124';}),side:edited(t=>t.legs[0].side='BUY'),product:signature(base,selected,'MIS')!==baseline,closed:edited(t=>t.status='CLOSED'),otherLeg:edited(t=>t.legs.push({...leg,id:'leg-second',token:'456',instrument_id:'kotak:mcx_fo:456',side:'BUY'}))};
let networkCalls=0;globalThis.fetch=()=>{networkCalls++;throw Error('SSR must not call the broker or app');};
const render=source=>renderToStaticMarkup(React.createElement(Check,{source}));
const html=render(base),disabledHtml=render(excluded.demo);
(async()=>console.log(JSON.stringify({requestDigest:await digest(result.request),selected,rejected,exclusionChecks,invalidation,valid:amounts(result,selected,'NRML',now),edgeStale:amounts(result,selected,'NRML',now+60000),wrapped:legs({trade:base},now).length,fallbackPrice:legs({...base,legs:[{...leg,current_price:null}]},now)[0].price,networkCalls,html,disabledHtml,safeErrors:[safeError(Object.assign(new Error('TOKEN=private-secret'),{status:500})),safeError({name:'AbortError',message:'private-secret'}),safeError({status:429,message:'private-secret'})]})))().catch(error=>{console.error(error);process.exitCode=1;});`;
const output=esbuild.buildSync({stdin:{contents:source,resolveDir:root,sourcefile:'kotak-margin-ssr-test.tsx',loader:'tsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',define:{'import.meta.env':'{}'}}).outputFiles[0].text;
const mod=new Module(root+'/kotak-margin-ssr-test.cjs');mod._compile(output,root+'/kotak-margin-ssr-test.cjs');
"""
    result = subprocess.run(
        [node, '-e', script, str(ROOT/'frontend/node_modules/vite/package.json'), str(ROOT/'frontend')],
        capture_output=True, text=True, encoding='utf-8', check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr[:4000]
    return json.loads(result.stdout)


def test_mcx_single_order_uses_remaining_broker_units_without_cash_multiplier(checks):
    selected = checks['selected']
    assert selected['lots'] == 2 and selected['quantity'] == 2500
    assert selected['lotSize'] == 1250 and selected['price'] == 12.5
    assert checks['wrapped'] == 1 and checks['fallbackPrice'] == 12


def test_sample_retired_pending_closed_and_invalid_contracts_are_excluded(checks):
    assert all(checks['exclusionChecks'].values()), checks['exclusionChecks']


def test_single_order_reply_must_match_scope_exact_inputs_and_freshness(checks):
    assert checks['valid'] == {'ordMrgn': 25000, 'reqdMrgn': 0}
    assert all(checks['rejected'].values()), checks['rejected']
    assert checks['edgeStale'] is None


def test_exposure_edits_and_price_product_changes_invalidate_prior_checks(checks):
    assert all(checks['invalidation'].values()), checks['invalidation']


def test_response_digest_matches_the_exact_backend_sdk_request_fingerprint(checks):
    request = {'exchange_segment': 'mcx_fo', 'instrument_token': '123',
               'transaction_type': 'S', 'product': 'NRML', 'order_type': 'L',
               'quantity': '2500', 'price': '12.5'}
    expected = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert checks['requestDigest'] == expected


def test_rendering_does_not_start_requests_and_labels_only_single_order_check(checks):
    html = checks['html']
    assert checks['networkCalls'] == 0
    assert 'Check single order' in html and 'NATURALGAS TEST' in html
    assert '2,500' in html and '₹12.50' in html and 'NRML' in html
    assert 'Never add these amounts across legs' in html
    assert 'No real order is placed' in html
    assert 'disabled=""' not in html
    assert 'disabled=""' in checks['disabledHtml']


def test_errors_never_echo_arbitrary_broker_or_network_text(checks):
    assert all('private-secret' not in error and 'TOKEN' not in error for error in checks['safeErrors'])
    assert 'timed out' in checks['safeErrors'][1]
    assert 'rate limited' in checks['safeErrors'][2]
