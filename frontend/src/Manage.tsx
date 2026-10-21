import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, Copy, Layers, Loader2, Pencil, Plus, X } from 'lucide-react';
import { api, brokers, money, num, time, uid } from './api';
import { Badge, Help, LineChart, Modal, Pnl } from './components';
import type { Row } from './components';

const names:Record<string,string>={add_lots:'Add lots',partial_exit:'Partial exit',exit_leg:'Exit leg',exit_strategy:'Exit strategy',duplicate:'Duplicate strategy',edit:'Edit paper assumptions',cancel_pending:'Cancel pending simulation'};

export default function Manage({trade,onClose,onChanged,onAddLeg,notify}:{trade:Row;onClose:()=>void;onChanged:()=>void;onAddLeg:(t:Row)=>void;notify:(m:string,error?:boolean)=>void}){
 const [detail,setDetail]=useState<Row>({trade,events:[],timeline:[]});
 const [tab,setTab]=useState('Legs'),[action,setAction]=useState(''),[legId,setLegId]=useState(trade.legs?.[0]?.id||'');
 const [lots,setLots]=useState(1),[price,setPrice]=useState(''),[name,setName]=useState(trade.name),[stopLoss,setStopLoss]=useState(''),[target,setTarget]=useState('');
 const [busy,setBusy]=useState(false),[actionKey,setActionKey]=useState(uid()),[refreshError,setRefreshError]=useState('');
 const mounted=useRef(true),fetching=useRef(false),tabRef=useRef(tab);
 tabRef.current=tab;
 const t=detail.trade||trade,legs:Row[]=t.legs||[],selected=legs.find(l=>l.id===legId);
 const status=String(t.status).toUpperCase(),closed=status==='CLOSED',cancelled=status==='CANCELLED',finished=closed||cancelled,pending=status==='PENDING';
 const hasPending=legs.some(l=>String(l.status).toLowerCase()==='pending');
 const risk=t.risk||{},riskSupported=risk.supported===true;
 const eligible=legs.filter(l=>action==='cancel_pending'?String(l.status).toLowerCase()==='pending':action==='edit'?['open','pending'].includes(String(l.status).toLowerCase()):l.open_lots>0);

 async function refresh(history=true){
  if(fetching.current)return;
  fetching.current=true;
  try{
   const result=await api(`/api/trades/${trade.id}${history?'':'?include_history=false'}`);
   if(mounted.current){setDetail((previous:Row)=>history?result:{...previous,trade:result.trade});setRefreshError('');}
  }catch(e){if(mounted.current)setRefreshError((e as Error).message);}
  finally{fetching.current=false;}
 }
 useEffect(()=>{
  mounted.current=true;refresh();
  const timer=setInterval(()=>refresh(['P&L timeline','Audit trail'].includes(tabRef.current)),3000);
  return()=>{mounted.current=false;clearInterval(timer);};
 },[trade.id]);
 function choose(a:string){
  setAction(a);setActionKey(uid());setLots(1);setPrice('');setStopLoss('');setTarget('');setName(t.name);
  const options=legs.filter(l=>a==='cancel_pending'?String(l.status).toLowerCase()==='pending':a==='edit'?['open','pending'].includes(String(l.status).toLowerCase()):l.open_lots>0);
  setLegId(options.find(l=>l.id===legId)?.id||options[0]?.id||'');
 }
 async function execute(){
  setBusy(true);
  try{
   const body:Row={action,idempotency_key:actionKey,confirmed:true};
   if(['add_lots','partial_exit','exit_leg','edit','cancel_pending'].includes(action)&&legId)body.leg_id=legId;
   if(['add_lots','partial_exit'].includes(action))body.lots=lots;
   if(price)body.price=Number(price);
   if(action==='edit'){body.name=name;if(stopLoss)body.stop_loss=Number(stopLoss);if(target)body.target=Number(target);}
   await api(`/api/trades/${trade.id}/actions`,{method:'POST',body:JSON.stringify(body)});
   notify(`${names[action]} recorded in the paper journal.`);setAction('');onChanged();await refresh();
  }catch(e){notify((e as Error).message,true);}
  finally{setBusy(false);}
 }
 function changeTab(value:string){setTab(value);if(['P&L timeline','Audit trail'].includes(value))refresh();}
 function eventDetails(e:Row){const {id,kind,type,action,timestamp,created_at,...fields}=e;return e.details||e.data||fields;}

 return <Modal title={t.name||t.underlying} subtitle={`${brokers[t.broker]||t.broker} · ${t.underlying} · ${t.id}`} onClose={onClose} wide>
  <div className="trade-detail-status"><Badge tone={finished?'muted':pending?'amber':'green'}><i/>{cancelled?'Cancelled':closed?'Closed':pending?'Pending':'Active'} paper trade</Badge>{t.demo&&<Badge tone="amber">Sample data</Badge>}<span>Opened {time(t.created_at,true)}</span></div>
  {refreshError&&<div className="banner error">Latest trade details are unavailable. Values below are last known. {refreshError}</div>}
  {t.valuation_stale&&!finished&&<Help>Valuation uses last-known quotes. {t.valuation_error||'One or more sources are stale; fresh quotes are required for market fills.'}</Help>}
  <div className="detail-stats"><div><span>Net paper P&L</span><strong><Pnl value={t.net_pnl}/></strong></div><div><span>Max favourable (MFE)</span><strong className="positive">{money(t.mfe||0)}</strong><small>{time(t.mfe_at,true)}</small></div><div><span>Max adverse (MAE)</span><strong className="negative">{money(t.mae||0)}</strong><small>{time(t.mae_at,true)}</small></div><div><span>{closed?'Profit giveback':'Estimated charges'}</span><strong>{money(closed?t.profit_giveback:t.charges)}</strong><small>{finished?`${cancelled?'Cancelled':'Closed'} ${time(t.closed_at,true)}`:'Subtracted from gross P&L'}</small></div></div>
  <div className="detail-tabs">{['Legs','P&L timeline','Audit trail','Risk'].map(a=><button key={a} onClick={()=>changeTab(a)} className={tab===a?'active':''}>{a}</button>)}</div>
  {tab==='Legs'?<div className="table-scroll detail-leg-table"><table><thead><tr><th>Contract</th><th>Side</th><th>Lots</th><th>Average entry</th><th>Current / exit</th><th>Paper P&L</th></tr></thead><tbody>{legs.map(l=>{
   const legPending=String(l.status).toLowerCase()==='pending',valued=l.open_lots>0||legPending,mark=valued?l.current_price:l.exit_price;
   return <tr key={l.id}><td><strong>{num(l.strike,0)} {l.option_type}</strong><small className="cell-note">{l.expiry} · {l.exchange} · lot {l.lot_size}{Number(l.price_multiplier||1)!==1?` · price multiplier ${num(Number(l.price_multiplier),0)}`:''}</small></td><td><Badge tone={l.side==='BUY'?'green':'red'}>{l.side}</Badge></td><td>{l.open_lots} <span className="muted">/ {l.lots}</span>{legPending&&<small className="cell-note">Pending simulation</small>}</td><td>{l.entry_at?money(l.entry_price):'Not filled'}</td><td>{mark!=null?money(mark):'—'}<small className={`cell-note ${l.stale&&valued?'negative':''}`}>{valued?<>{l.stale?'Stale · last known · ':''}{brokers[l.quote_source]||l.quote_source||'Unknown source'} · {time(l.quote_at)}</>:l.exit_at?time(l.exit_at):String(l.status||'Not filled')}</small></td><td><Pnl value={l.net_pnl}/></td></tr>;
  })}</tbody></table></div>:tab==='P&L timeline'?<div className="detail-chart"><LineChart data={(detail.timeline||[]).map((p:Row)=>({x:p.timestamp,y:p.net_pnl}))}/><Help>One-second snapshots include carried-forward quotes. MFE and MAE reflect only values observed while capture was running.</Help></div>:tab==='Audit trail'?<div className="audit-list">{(detail.events||[]).map((e:Row,i:number)=><div key={e.id||i}><span className="audit-dot"/><div><strong>{String(e.kind||e.type||e.action||'Trade event').replaceAll('_',' ')}</strong><details><summary className="text-btn">View event details</summary><pre className="json-report">{JSON.stringify(eventDetails(e),null,2)}</pre></details></div><time>{time(e.timestamp||e.created_at,true)}</time></div>)}{!detail.events?.length&&<p className="muted">{t.archived?'Detailed journal rows were archived after a verified laptop save. Open the retained Excel workbook for the full record.':'No audit events are available for this trade.'}</p>}</div>:!riskSupported?<Help>{risk.reason||'Risk estimates are not available for this trade. No zero-risk value is assumed.'}</Help>:<div className="detail-risk"><div><span>Maximum profit at expiry</span><strong className="positive">{risk.profit_unlimited?'Unlimited':risk.max_profit!=null?money(risk.max_profit):'Unavailable'}</strong></div><div><span>Maximum loss at expiry</span><strong className="negative">{risk.loss_unlimited?'Unlimited':risk.max_loss!=null?money(risk.max_loss):'Unavailable'}</strong></div><div><span>Estimated margin</span><strong>{risk.estimated_margin!=null?money(risk.estimated_margin):'Unavailable'}</strong></div><div><span>Estimated hedge benefit</span><strong>{risk.hedge_benefit!=null?money(risk.hedge_benefit):'Unavailable'}</strong></div><div><span>Breakevens</span><strong>{(risk.breakevens||[]).map((v:number)=>num(v)).join(' / ')||'—'}</strong></div><Help>{Array.isArray(risk.assumptions)?risk.assumptions.join(' '):risk.assumptions||'Expiry payoff assumes the same underlying and expiry. Margin is an estimate.'}</Help></div>}
  {!action?<div className="manage-actions">{!finished&&<><button className="btn secondary" disabled={!legs.some(l=>l.open_lots>0)} onClick={()=>choose('add_lots')}><Plus size={14}/>Add lots</button><button className="btn secondary" onClick={()=>onAddLeg(t)}><Layers size={14}/>Add leg</button><button className="btn secondary" disabled={!legs.some(l=>l.open_lots>0)} onClick={()=>choose('partial_exit')}>Partial exit</button><button className="btn secondary" disabled={!legs.some(l=>l.open_lots>0)} onClick={()=>choose('exit_leg')}>Exit leg</button>{hasPending&&<button className="btn secondary" onClick={()=>choose('cancel_pending')}>Cancel pending</button>}<button className="icon-btn" title="Edit paper assumptions" onClick={()=>choose('edit')}><Pencil size={16}/></button></>}<button className="btn secondary" onClick={()=>choose('duplicate')}><Copy size={14}/>Duplicate</button>{!finished&&<button className="btn danger exit-all" onClick={()=>choose('exit_strategy')}>Exit strategy<ArrowRight size={14}/></button>}</div>:<section className="confirm-action"><div className="panel-head"><h3>{names[action]}</h3><button className="icon-btn" aria-label="Cancel action" onClick={()=>setAction('')}><X size={16}/></button></div><div className="action-inputs">
   {['add_lots','partial_exit','exit_leg','edit','cancel_pending'].includes(action)&&<label>Selected leg<select value={legId} onChange={e=>{setLegId(e.target.value);setPrice('');setStopLoss('');setTarget('');}}>{eligible.map(l=><option key={l.id} value={l.id}>{l.strike} {l.option_type} · {l.side} · {String(l.status).toLowerCase()==='pending'?'pending':`${l.open_lots} open lots`}</option>)}</select></label>}
   {['add_lots','partial_exit'].includes(action)&&<label>Lots<input type="number" min="1" max={action==='partial_exit'?selected?.open_lots:1000} value={lots} onChange={e=>setLots(Math.max(1,Math.trunc(Number(e.target.value))))}/></label>}
   {['partial_exit','exit_leg','exit_strategy','add_lots'].includes(action)&&t.demo&&<label>Paper price override (optional)<input type="number" min="0.05" step="0.05" value={price} placeholder="Use current quote" onChange={e=>setPrice(e.target.value)}/></label>}
   {action==='edit'&&<><label>Strategy name<input value={name} maxLength={100} onChange={e=>setName(e.target.value)}/></label>{selected?.open_lots>0&&<label>Average entry assumption (optional)<input aria-label="Audited average entry assumption" type="number" min="0.01" step="0.01" value={price} onChange={e=>setPrice(e.target.value)} placeholder={`Leave ${num(selected?.entry_price)} unchanged`}/></label>}<label>Stop loss premium<input type="number" min="0.05" value={stopLoss} onChange={e=>setStopLoss(e.target.value)} placeholder="Leave unchanged"/></label><label>Target premium<input type="number" min="0.05" value={target} onChange={e=>setTarget(e.target.value)} placeholder="Leave unchanged"/></label></>}
  </div><Help>{action==='duplicate'?'This creates a separate paper trade with a new identity at the current available quotes.':action==='edit'?'Changes to average entry are paper ledger assumptions and are recorded in the audit trail. Historical fill events remain in the journal.':action==='exit_strategy'?'This closes open legs and cancels pending simulations. A completed trade with filled lots queues its Excel report.':`This action updates only this ${brokers[t.broker]} paper trade and writes an audit event.`}</Help><div className="modal-actions"><button className="btn secondary" onClick={()=>setAction('')}>Cancel</button><button className={`btn ${action.startsWith('exit')||action==='partial_exit'?'danger':'primary'}`} disabled={busy||(finished&&action!=='duplicate')||(['add_lots','partial_exit','exit_leg','cancel_pending'].includes(action)&&!eligible.some(l=>l.id===legId))} onClick={execute}>{busy?<Loader2 className="spin" size={15}/>:<Check size={15}/>}Confirm {names[action].toLowerCase()}</button></div></section>}
 </Modal>;
}
