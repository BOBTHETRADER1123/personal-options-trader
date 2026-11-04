"""Execute the real App connection effect with fake time, fetch and sockets.

No real network, owner session, credentials or broker connectivity are involved.
"""
import json
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[2]
SCRIPT=r"""
const fs=require('node:fs'),vm=require('node:vm'),ts=require(process.argv[1]);
const app=fs.readFileSync(process.argv[2],'utf8').replaceAll('\r\n','\n'),scenario=process.argv[3];
const start=app.indexOf(' useEffect(()=>{\n  if(!session?.authenticated)return;');
if(start<0)throw Error('Connection effect not found');
const marker=' },[session?.authenticated,refresh]);',end=app.indexOf(marker,start)+marker.length;
const source=app.slice(start,end).replaceAll('import.meta','__import_meta');
const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
let now=1000000,nextId=0,cleanup,visible=true,mode='success',calls=[],sockets=[],states=[],dashboards=[],refreshes=0;
const timers=new Map(),listeners=new Map();
const addTimer=(fn,delay,interval=false)=>{const id=++nextId;timers.set(id,{fn,delay,due:now+delay,interval});return id;};
const clockDate=class extends Date {static now(){return now;}};
class Socket{
 constructor(url){this.url=url;this.readyState=0;sockets.push(this);}
 open(){this.readyState=1;this.onopen?.({});}
 message(value){this.onmessage?.({data:JSON.stringify(value)});}
 close(){this.readyState=3;this.onclose?.({});}
}
const context={session:{authenticated:true},refresh:()=>{refreshes++;},pageVisible:()=>visible,
 wsRef:{current:null},API_BASE:'',location:{protocol:'https:',host:'personal-options-trader.vercel.app'},
 __import_meta:{env:{VITE_WS_URL:'wss://libzw8xd976f.ramnaymcloud.com/ws'}},
 document:{addEventListener:(event,fn)=>listeners.set(event,fn),removeEventListener:event=>listeners.delete(event)},
 setTimeout:(fn,delay)=>addTimer(fn,delay),clearTimeout:id=>timers.delete(id),
 setInterval:(fn,delay)=>addTimer(fn,delay,true),clearInterval:id=>timers.delete(id),
 Date:clockDate,Math:{...Math,random:()=>1,min:Math.min},AbortController,WebSocket:Socket,
 setDashboard:value=>dashboards.push(value),setConnection:value=>states.push(value),setAuthError:()=>{},setLoading:()=>{},setJobs:()=>{},
 api:(path,options={})=>{
  if(path!='/api/ws-ticket')return Promise.resolve([]);
  calls.push({at:now,options});
  if(mode==='reject')return Promise.reject(Error('offline'));
  if(mode==='hang')return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Error('aborted')),{once:true}));
  return Promise.resolve({ticket:'synthetic-test-ticket'});
 },useEffect:fn=>{cleanup=fn();}};
vm.createContext(context);
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
const advance=async milliseconds=>{
 const target=now+milliseconds;
 while(true){let selected;
  for(const [id,timer] of timers)if(timer.due<=target&&(!selected||timer.due<selected[1].due))selected=[id,timer];
  if(!selected)break;
  const [id,timer]=selected;now=timer.due;if(timer.interval)timer.due+=timer.delay;else timers.delete(id);timer.fn();await flush();
 }now=target;await flush();
};
const boot=async()=>{vm.runInContext(compiled,context);await flush();};
(async()=>{
 let result;
 if(scenario==='timeout'){
  mode='hang';await boot();await advance(7999);const before=calls.length;await advance(1);
  const aborted=calls[0].options.signal.aborted;await advance(1000);
  result={before,aborted,attempts:calls.length,delay:calls[1].at-calls[0].at};
 }else if(scenario==='backoff'){
  mode='reject';await boot();await advance(190000);result={attemptTimes:calls.map(row=>row.at-1000000)};
 }else if(scenario==='heartbeat-reset'){
  mode='reject';await boot();await advance(1000);mode='success';await advance(2000);
  const socket=sockets.at(-1);socket.open();socket.message({type:'heartbeat',server_time:'synthetic'});socket.close();await advance(1000);
  result={attemptTimes:calls.map(row=>row.at-1000000),dashboards:dashboards.length,healthy:states.includes(true),url:socket.url};
 }else if(scenario==='visibility'){
  mode='hang';await boot();listeners.get('visibilitychange')();await flush();const duplicateAttempts=calls.length;
  visible=false;listeners.get('visibilitychange')();await flush();await advance(60000);const hiddenAttempts=calls.length;
  mode='success';visible=true;listeners.get('visibilitychange')();await flush();
  result={duplicateAttempts,hiddenAttempts,resumedAttempts:calls.length,firstAborted:calls[0].options.signal.aborted};
 }else if(scenario==='opening-timeout'){
  await boot();await advance(8000);const closed=sockets[0].readyState;await advance(1000);result={closed,attempts:calls.length};
 }else if(scenario==='cleanup'){
  await boot();const socket=sockets[0];socket.open();socket.message({type:'dashboard',data:{value:1}});cleanup();await flush();await advance(120000);
  result={closed:socket.readyState,attempts:calls.length,timers:timers.size,listeners:listeners.size,dashboards:dashboards.length};
 }else throw Error('Unknown scenario');
 cleanup();console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exit(1);});
"""


def run_scenario(scenario):
    node=shutil.which('node')
    bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    node=node or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT/'frontend/node_modules/typescript').is_dir():
        pytest.skip('Node and existing frontend dependencies are required.')
    result=subprocess.run([node,'-e',SCRIPT,str(ROOT/'frontend/node_modules/typescript'),
                           str(ROOT/'frontend/src/App.tsx'),scenario],capture_output=True,text=True,check=False,timeout=30)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def test_ticket_fetch_aborts_at_eight_seconds_and_retries():
    result=run_scenario('timeout')
    assert result=={'before':1,'aborted':True,'attempts':2,'delay':9000}


def test_reconnects_back_off_to_one_minute_instead_of_tight_loop():
    result=run_scenario('backoff')
    assert result['attemptTimes']==[0,1000,3000,7000,15000,31000,63000,123000,183000]


def test_heartbeat_keeps_connection_healthy_and_resets_backoff_without_redrawing():
    result=run_scenario('heartbeat-reset')
    assert result['attemptTimes']==[0,1000,3000,4000]
    assert result['dashboards']==0 and result['healthy'] is True
    assert result['url']=='wss://libzw8xd976f.ramnaymcloud.com/ws?ticket=synthetic-test-ticket'


def test_one_inflight_ticket_hidden_tab_abort_and_visible_resume():
    result=run_scenario('visibility')
    assert result=={'duplicateAttempts':1,'hiddenAttempts':1,'resumedAttempts':2,'firstAborted':True}


def test_socket_handshake_times_out_and_retries():
    assert run_scenario('opening-timeout')=={'closed':3,'attempts':2}


def test_unmount_closes_socket_and_clears_timers_listeners():
    assert run_scenario('cleanup')=={'closed':3,'attempts':1,'timers':0,'listeners':0,'dashboards':1}
