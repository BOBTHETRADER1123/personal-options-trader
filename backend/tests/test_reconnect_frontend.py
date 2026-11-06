"""Exercise the actual reconnect handler with fake HTTP and browser timers."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
SCRIPT=r"""
const fs=require('node:fs'),vm=require('node:vm'),ts=require(process.argv[1]);
const root=process.argv[2],scenario=process.argv[3],source=fs.readFileSync(root+'/frontend/src/pages.tsx','utf8');
const file=ts.createSourceFile('pages.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
let handler;
function visit(node){if(ts.isFunctionDeclaration(node)&&node.name?.text==='reconnectFeed')handler=node.getText(file);else ts.forEachChild(node,visit);}
visit(file);if(!handler)throw Error('Actual reconnect handler missing');
const compiled=ts.transpileModule(handler,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
let next=0,now=0,calls=[],notifications=[],busy=[],refreshes=0,health=0;
const timers=new Map(),reconnectInFlight={current:false},masterInFlight={current:scenario==='master-busy'},reconnectController={current:null};
const context={AbortController,Error,Promise,reconnectInFlight,masterInFlight,reconnectController,
 window:{setTimeout:(fn,delay)=>{const id=++next;timers.set(id,{fn,due:now+delay});return id;},clearTimeout:id=>timers.delete(id)},
 setBusy:value=>busy.push(value),setHealthRefresh:()=>health++,onRefresh:()=>refreshes++,notify:(message,error)=>notifications.push({message,error:Boolean(error)}),
 api:(path,options)=>{calls.push({path,method:options.method,signal:options.signal});
  if(['timeout','duplicate','abort'].includes(scenario))return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const error=Error('cancelled');error.name='AbortError';reject(error);},{once:true}));
  if(scenario==='rate-limit')return Promise.reject(Error('Please wait 10 seconds before reconnecting again.'));
  if(scenario==='failure')return Promise.resolve({ok:false,status:'authentication_required',message:'Broker sign-in needs attention.'});
  if(scenario==='closed')return Promise.resolve({ok:false,status:'market_closed',message:'The verified selected market session is closed.'});
  return Promise.resolve({ok:true,status:'connecting',message:'Connection requested. Waiting for quotes.'});
 }};
vm.createContext(context);vm.runInContext(compiled,context);
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
(async()=>{
 const before=calls.length,work=context.reconnectFeed();await flush();
 if(scenario==='duplicate'){context.reconnectFeed();await flush();}
 let beforeTimeout;
 if(['timeout','duplicate'].includes(scenario)){
  now=29999;await flush();beforeTimeout=calls[0].signal.aborted;
  now=30000;for(const [id,timer]of timers)if(timer.due<=now){timers.delete(id);timer.fn();}await flush();
 }
 if(scenario==='abort')reconnectController.current.abort();
 await work;
 console.log(JSON.stringify({before,beforeTimeout,calls:calls.map(({path,method,signal})=>({path,method,aborted:signal.aborted})),notifications,busy,refreshes,health,timers:timers.size,inFlight:reconnectInFlight.current,controller:reconnectController.current}));
})().catch(error=>{console.error(error);process.exit(1);});
"""


def run(scenario):
    bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node=shutil.which('node') or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT/'frontend/node_modules/typescript').is_dir():
        pytest.skip('Installed Node/frontend dependencies needed')
    result=subprocess.run([node,'-e',SCRIPT,str(ROOT/'frontend/node_modules/typescript'),str(ROOT),scenario],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('scenario',['success','failure','closed','rate-limit','timeout','duplicate','abort','master-busy'])
def test_reconnect_is_an_explicit_bounded_request_without_master_download(scenario):
    result=run(scenario)
    assert result['before']==0  # Loading declarations cannot reconnect anything.
    assert result['timers']==0 and not result['inFlight'] and result['controller'] is None
    if scenario=='master-busy':
        assert result['calls']==[] and result['notifications']==[]
        return
    assert len(result['calls'])==1
    assert result['calls'][0]['path']=='/api/brokers/kotak/reconnect'
    assert result['calls'][0]['method']=='POST'
    assert result['busy']==['kotak-reconnect','']
    if scenario in ['timeout','duplicate']:
        assert result['beforeTimeout'] is False and result['calls'][0]['aborted']
        assert 'server may still be reconnecting' in result['notifications'][0]['message']
        assert result['refreshes']==0
    elif scenario=='abort':
        assert result['notifications']==[] and result['refreshes']==0
    elif scenario=='rate-limit':
        assert result['notifications']==[{'message':'Please wait 10 seconds before reconnecting again.','error':True}]
    else:
        assert result['refreshes']==1 and result['health']==1
        assert result['notifications'][0]['error'] is (scenario=='failure')
        assert not any('connected' in n['message'].lower() for n in result['notifications'])
