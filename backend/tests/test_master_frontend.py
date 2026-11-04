"""Run the actual contract-refresh helpers and page handlers with fake HTTP/time.

These are browser-behavior tests, not authenticated Kotak/network tests.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
const fs=require('node:fs'),vm=require('node:vm'),ts=require(process.argv[1]);
const root=process.argv[2],scenario=process.argv[3],page=process.argv[4];
const chain=fs.readFileSync(root+'/frontend/src/OptionChain.tsx','utf8');
const pages=fs.readFileSync(root+'/frontend/src/pages.tsx','utf8');
function declaration(text,name){
 const file=ts.createSourceFile('test.tsx',text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 let result;
 function visit(node){if(ts.isFunctionDeclaration(node)&&node.name?.text===name)result=node;else ts.forEachChild(node,visit);}
 visit(file);if(!result)throw Error('Missing actual function '+name);return result.getText(file);
}
const functions=['contractRequest','contractMasterBlocked','contractMasterDiagnostic','contractMasterSources'].map(name=>declaration(chain,name)).join('\n');
const handler=page==='connections'?'reload':'updateMaster';
const code=functions+'\n'+declaration(page==='connections'?pages:chain,handler);
const compiled=ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
let now=0,nextId=0,calls=[],notifications=[],loading=[],masters=[],revisions=0,refreshes=0;
const timers=new Map(),masterInFlight={current:false},masterController={current:null};
const context={exports:{},AbortController,Error,Number,Object,Array,Promise,
 setTimeout:(fn,delay)=>{const id=++nextId;timers.set(id,{fn,due:now+delay});return id;},clearTimeout:id=>timers.delete(id),
 masterInFlight,masterController,broker:'kotak',
 setMasterLoading:value=>loading.push(value),setBusy:value=>loading.push(value),
 setMaster:value=>masters.push(value),setMasterStatus:value=>masters.push(value),setMasterStatusError:()=>{},
 setCatalogRevision:()=>{revisions++;},onRefresh:()=>{refreshes++;},
 onNotify:(message,error)=>notifications.push({message,error:Boolean(error)}),notify:(message,error)=>notifications.push({message,error:Boolean(error)}),
 api:(path,options={})=>{
  calls.push({path,method:options.method||'GET',signal:options.signal});
  const post=options.method==='POST';
  if(scenario==='status-error'&&!post)return Promise.reject(Error('Status unavailable'));
  if(!post)return Promise.resolve({stale:true,refresh_in_progress:scenario==='already-running'});
  if(['timeout','duplicate','request-timeout','external-abort'].includes(scenario))return new Promise((resolve,reject)=>{
   options.signal.addEventListener('abort',()=>{const error=Error('Aborted');error.name='AbortError';reject(error);},{once:true});
  });
  if(scenario==='failure')return Promise.resolve({ok:false,master:{stale:true,last_error:{stage:'csv_columns',segment:'mcx_fo',code:'missing_columns',row_number:17}}});
  return Promise.resolve({ok:true,master:{stale:false,last_successful_refresh_at:'2026-11-04T03:35:00Z'}});
 }};
vm.createContext(context);vm.runInContext(compiled,context);
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
const advance=async ms=>{const target=now+ms;while(true){let selected;for(const [id,timer]of timers)if(timer.due<=target&&(!selected||timer.due<selected[1].due))selected=[id,timer];if(!selected)break;now=selected[1].due;timers.delete(selected[0]);selected[1].fn();await flush();}now=target;await flush();};
(async()=>{
 let result;
 if(scenario==='request-timeout'){
  let failure;const work=context.contractRequest('/api/option-chain/master/refresh',{method:'POST'},90000).catch(error=>{failure=error.message;});
  await advance(89999);const early=calls[0].signal.aborted;await advance(1);await work;
  result={early,aborted:calls[0].signal.aborted,failure,timers:timers.size};
 }else if(scenario==='external-abort'){
  const owner=new AbortController();let failure;const work=context.contractRequest('/api/option-chain/master/refresh',{method:'POST',signal:owner.signal},90000).catch(error=>{failure=error.name;});
  owner.abort();await work;result={failure,aborted:calls[0].signal.aborted,timers:timers.size};
 }else if(scenario==='diagnostics'){
  result={unknownBlocked:context.contractMasterBlocked({}),staleBlocked:context.contractMasterBlocked({stale:true}),currentAllowed:!context.contractMasterBlocked({stale:false}),
   diagnostic:context.contractMasterDiagnostic({last_error:{stage:'csv_columns',segment:'mcx_fo',code:'missing_columns',row_number:17,message:'PRIVATE RESPONSE',traceback:'PRIVATE TRACE'}}),
   rejectedUnsafe:context.contractMasterDiagnostic({last_error:{stage:'unsafe/?secret',segment:'mcx_fo',code:'bad_columns'}}),
   mapping:context.contractMasterSources({segment_status:{mcx_fo:{file_date:'2026-11-04'}}}),list:context.contractMasterSources({sources:[{segment:'nse_fo',source_date:'2026-11-04',url:'private-url'}]})};
 }else{
  const work=context[handler]();await flush();
  if(scenario==='duplicate'){context[handler]();await flush();}
  if(scenario==='timeout'||scenario==='duplicate')await advance(90000);
  await work;result={posts:calls.filter(c=>c.method==='POST').length,gets:calls.filter(c=>c.method==='GET').length,notifications,loading,masters,revisions,refreshes,busy:masterInFlight.current,timers:timers.size,paths:calls.map(c=>c.path)};
 }
 console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exit(1);});
"""


def run_scenario(scenario, page="chain"):
    node = shutil.which("node")
    bundled = Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    node = node or (str(bundled) if bundled.is_file() else None)
    if not node or not (ROOT/"frontend/node_modules/typescript").is_dir():
        pytest.skip("Node and installed frontend dependencies are required.")
    completed = subprocess.run([node, "-e", SCRIPT, str(ROOT/"frontend/node_modules/typescript"), str(ROOT), scenario, page], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_browser_refresh_timeout_is_bounded_and_does_not_claim_server_cancellation():
    result = run_scenario("request-timeout")
    assert result["early"] is False and result["aborted"] is True and result["timers"] == 0
    assert "server may still be working" in result["failure"]
    assert "Check refresh status before retrying" in result["failure"]


def test_unmount_abort_is_preserved_and_timeout_timer_is_cleared():
    assert run_scenario("external-abort") == {"failure": "AbortError", "aborted": True, "timers": 0}


def test_only_safe_structured_master_diagnostics_and_source_dates_are_displayed():
    result = run_scenario("diagnostics")
    assert result["unknownBlocked"] and result["staleBlocked"] and result["currentAllowed"]
    assert "csv_columns / mcx_fo / missing_columns / CSV row 17" in result["diagnostic"]
    assert "PRIVATE" not in result["diagnostic"] and "secret" not in result["rejectedUnsafe"]
    assert result["mapping"] == "mcx_fo: 2026-11-04"
    assert result["list"] == "nse_fo: 2026-11-04"


@pytest.mark.parametrize("page", ["chain", "connections"])
@pytest.mark.parametrize("scenario", ["timeout", "duplicate", "already-running", "status-error", "failure", "success"])
def test_actual_refresh_handlers_do_not_duplicate_jobs_or_restart_feeds(page, scenario):
    result = run_scenario(scenario, page)
    assert not result["busy"] and result["timers"] == 0
    assert result["loading"][-1] in [False, ""]
    assert not any("/instruments/reload" in path for path in result["paths"])
    if scenario in ["already-running", "status-error"]:
        assert result["posts"] == 0
    else:
        assert result["posts"] == 1
    if scenario in ["timeout", "duplicate"]:
        assert result["gets"] == 2  # Status read after timeout, never another POST.
        assert any("server may still be working" in row["message"] for row in result["notifications"])
    if scenario != "success":
        assert result["revisions"] == 0 and result["refreshes"] == 0
    else:
        assert result["revisions"] == (1 if page == "chain" else 0)
        assert result["refreshes"] == (1 if page == "connections" else 0)
        assert result["masters"][-1]["stale"] is False
