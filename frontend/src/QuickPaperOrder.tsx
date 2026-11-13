import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Loader2, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, money, num, time, uid } from './api';
import { Badge, Help, Modal } from './components';
import type { Row } from './components';
import { executionPreview, reviewNeedsAnotherConfirmation } from './executionPreview';
import { brokerMarginValue } from './tradeMath';
import { contractId, numberValue, successfulPaperTrade } from './watchlistData';

type Props={contract:Row;side:'BUY'|'SELL';quote:Row|null;settings:Row;positions:Row[];onClose:()=>void;onSuccess:()=>void;onManage:(trade:Row)=>void;notify:(message:string,error?:boolean)=>void};
export default function QuickPaperOrder({contract,side,quote:initialQuote,settings,positions,onClose,onSuccess,onManage,notify}:Props){
 const [direction,setDirection]=useState(side),[lots,setLots]=useState(1),[orderType,setOrderType]=useState<'MARKET'|'LIMIT'>('MARKET'),[limit,setLimit]=useState('');
 const [quote,setQuote]=useState<Row|null>(initialQuote),[assumptions,setAssumptions]=useState<Row>(settings),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[acknowledged,setAcknowledged]=useState(false),[clock,setClock]=useState(Date.now());
 const [pending,setPending]=useState<Row|null>(null),requestKey=useRef(uid()),mounted=useRef(true),controller=useRef<AbortController|null>(null);
 const stock=contract.option_type==='EQ'||contract.instrument_type==='stock';
 const leg=useMemo(()=>({instrument_id:contractId(contract),side:direction,lots,order_type:orderType,...(orderType==='LIMIT'?{price:Number(limit)}:{})}),[contract,direction,lots,orderType,limit]);
 const estimate=executionPreview(contract,leg,quote,assumptions,clock);
 const caution=estimate.wideSpread||estimate.fallbackToLtp;
 const margin=brokerMarginValue(contract);
 useEffect(()=>{mounted.current=true;const timer=setInterval(()=>{if(document.visibilityState==='visible')setClock(Date.now());},1000);return()=>{mounted.current=false;clearInterval(timer);controller.current?.abort();};},[]);
 useEffect(()=>{setAcknowledged(false);if(!pending)requestKey.current=uid();},[direction,lots,orderType,limit]);
 useEffect(()=>{
  if(busy||pending||!initialQuote||initialQuote.broker!=='kotak'||initialQuote.instrument_id!==contractId(contract)||initialQuote.demo!==false)return;
  const nextStamp=Date.parse(initialQuote.received_at||'');
  setQuote(previous=>Number.isFinite(nextStamp)&&(!previous||!Number.isFinite(Date.parse(previous.received_at||''))||nextStamp>Date.parse(previous.received_at))?initialQuote:previous);
 },[initialQuote,busy,pending,contract]);
 async function fresh(){
  controller.current?.abort();const abort=new AbortController();controller.current=abort;const timeout=setTimeout(()=>abort.abort(),15000);
  try{
   const current:Row=await api('/api/settings',{signal:abort.signal});
   if(current.demo_mode!==false)throw new Error('Choose market-data paper mode in Settings before entering a genuine Kotak paper trade.');
   const result:Row=await api('/api/quotes/selection',{method:'POST',body:JSON.stringify({instruments:[{broker:'kotak',instrument_id:contractId(contract)}]}),signal:abort.signal});
   const own=(result.quotes||[]).find((item:Row)=>item.broker==='kotak'&&item.instrument_id===contractId(contract)&&item.demo===false)||null;
   if(mounted.current){setQuote(own);setAssumptions(current);setClock(Date.now());}
   return {quote:own,settings:current};
  }finally{clearTimeout(timeout);}
 }
 async function refresh(){if(busy)return;setBusy(true);setMessage('');try{await fresh();}catch(error){if(mounted.current)setMessage((error as Error).message);}finally{if(mounted.current)setBusy(false);}}
 useEffect(()=>{void refresh();},[]);
 const reviewShape=(preview:Row,current:Row)=>({...current,previews:[{...preview,contract:{...contract,id:contract.id||contractId(contract)},side:direction}]});
 async function submit(){
  if(busy)return;setBusy(true);setMessage('');
  try{
   let body=pending;
   if(!body){
    if(!Number.isInteger(lots)||lots<1||lots>1000)throw new Error(`Enter ${stock?'a whole share quantity':'whole lots'} between 1 and 1,000.`);
    const latest=await fresh(),next=executionPreview(contract,leg,latest.quote,latest.settings,Date.now());
    if(!next.valid)throw new Error(next.reason||'A fresh valid quote is required.');
    if(reviewNeedsAnotherConfirmation(reviewShape(estimate,assumptions),reviewShape(next,latest.settings))){setAcknowledged(false);setMessage('Prices or paper assumptions changed. Review the updated estimate, then confirm again.');return;}
    if((next.wideSpread||next.fallbackToLtp)&&!acknowledged){setMessage('Review and acknowledge the bid/ask spread warning before confirming.');return;}
    body={name:`${contract.symbol||contract.underlying} · watchlist paper ${direction.toLowerCase()}`,underlying:contract.underlying,brokers:['kotak'],legs:[leg],idempotency_key:requestKey.current,confirmed:true};
    setPending(body);
   }
   const abort=new AbortController();controller.current=abort;const timeout=setTimeout(()=>abort.abort(),25000);
   let result:Row;
   try{result=await api('/api/trades',{method:'POST',body:JSON.stringify(body),signal:abort.signal});}finally{clearTimeout(timeout);}
   const trade=successfulPaperTrade(result);
   if(!trade){setPending(null);requestKey.current=uid();throw new Error((result.results||[]).find((item:Row)=>item.error)?.error||'The server did not confirm a recorded paper trade. No success is assumed.');}
   notify(`Paper ${String(trade.status).toUpperCase()==='PENDING'?'limit recorded as pending':'trade recorded'}.`);onSuccess();onClose();
  }catch(error){
   const failure=error as Error&{status?:number};
   if(failure.status&&failure.status>=400&&failure.status<500&&failure.status!==408){setPending(null);requestKey.current=uid();}
   if(mounted.current)setMessage(failure.message||'The paper submission could not be verified.');
  }finally{if(mounted.current)setBusy(false);}
 }
 const value=estimate.previewPrice!==null&&estimate.quantity!==null?estimate.previewPrice*estimate.quantity:null;
 const canConfirm=pending!==null||(estimate.valid&&assumptions.demo_mode===false&&(!caution||acknowledged));
 return <Modal title={`New paper ${direction.toLowerCase()} order`} subtitle="Kotak Neo market data · simulation only" onClose={()=>{if(!busy)onClose();}} wide>
  <div className="quick-order">
   <div className="quick-instrument"><div><h3>{contract.symbol||contract.underlying}</h3><p>{contract.name||contract.company_name||contract.underlying} · {contract.exchange}{contract.expiry?` · ${contract.expiry}`:''}{!stock?` · ${contract.option_type} · ${num(Number(contract.strike||0),0)}`:''}</p></div><Badge tone="green">PAPER ONLY</Badge></div>
   {positions.length>0&&<div className="watch-position-note"><strong>{positions.length} existing open {positions.length===1?'trade':'trades'} in this instrument</strong><p>This panel creates a separate new paper trade. Use position management to add to, partially close or fully close an existing position.</p><button className="text-btn" disabled={busy||!!pending} onClick={()=>{onClose();onManage(positions[0]);}}>Manage existing position</button></div>}
   <div className="quick-form"><label>Direction<select value={direction} disabled={busy||!!pending} onChange={event=>setDirection(event.target.value as 'BUY'|'SELL')}><option value="BUY">BUY</option><option value="SELL">SELL</option></select></label><label>{stock?'Quantity · shares':'Number of lots'}<input aria-label={stock?'Share quantity':'Number of lots'} type="number" min={1} max={1000} step={1} value={lots} disabled={busy||!!pending} onChange={event=>setLots(Number(event.target.value))}/></label><label>Entry type<select value={orderType} disabled={busy||!!pending} onChange={event=>setOrderType(event.target.value as 'MARKET'|'LIMIT')}><option value="MARKET">Market simulation</option><option value="LIMIT">Limit simulation</option></select></label>{orderType==='LIMIT'&&<label>Limit price<input aria-label="Limit price" type="number" min="0.01" step="0.01" value={limit} disabled={busy||!!pending} onChange={event=>setLimit(event.target.value)}/></label>}</div>
   {!stock&&<p className="muted small">Lot size {num(Number(contract.lot_size||0),0)} · {estimate.quantity===null?'Verified quantity unavailable':`${num(estimate.quantity,0)} monetary units`}</p>}
   <div className="quick-quote"><span>Current LTP<strong>{numberValue(quote?.ltp)!==null?money(Number(quote?.ltp)):'Unavailable'}</strong></span><span>Bid / ask<strong>{numberValue(quote?.bid)!==null?money(Number(quote?.bid)):'—'} / {numberValue(quote?.ask)!==null?money(Number(quote?.ask)):'—'}</strong></span><span>Quote received · IST<strong>{time(quote?.received_at,true)}</strong></span><button className="btn secondary small-btn" onClick={refresh} disabled={busy||!!pending}><RefreshCw size={15}/>Refresh quote</button></div>
   <div className="quick-estimates"><div><span>Estimated {stock?'trade':'premium / contract'} value</span><strong>{value===null?'Unavailable':money(value)}</strong></div><div><span>Estimated entry charges</span><strong>{estimate.estimatedCharges===null?'Unavailable':money(estimate.estimatedCharges)}</strong></div><div><span>{stock?'Margin':'Broker margin'}</span><strong>{stock?'Cash shares · cost applies':margin===null?'Broker margin unavailable':money(margin)}</strong></div><div><span>Estimated fill</span><strong>{estimate.pendingLimit?'Pending limit':estimate.fillPrice===null?'Unavailable':money(estimate.fillPrice)}</strong></div></div>
   {estimate.notices.length>0&&<div className="watch-caution">{estimate.notices.map(note=><p key={note}>{note}</p>)}{caution&&<label><input type="checkbox" checked={acknowledged} onChange={event=>setAcknowledged(event.target.checked)} disabled={busy||!!pending}/>I have reviewed the estimated spread and fill assumptions.</label>}</div>}
   {pending&&<div className="watch-caution" role="alert">This submission is awaiting a verified result. Retry uses the same request identifier and unchanged order. Check positions before creating another order if you close this panel.</div>}
   {(message||!estimate.valid)&&<div className="inline-error" role="status">{message||estimate.reason}</div>}
   <Help><ShieldCheck size={15}/>The existing paper engine checks the current contract, market session, quote freshness, buying power and final fill. {stock?'Cash equity shares have no expiry. Hold until you choose to sell; months and years are allowed.':'Margin is separate from expiry risk.'} Estimates use your configured {assumptions.fill_basis==='ltp'?'LTP':'bid/ask'} basis and {num(Number(assumptions.slippage_bps||0))} bps slippage. No real broker order is sent.</Help>
   <div className="modal-actions"><button className="btn secondary" onClick={onClose} disabled={busy}>Cancel</button><button className="btn primary" disabled={busy||!canConfirm} onClick={submit}>{busy?<Loader2 size={16} className="spin"/>:<Check size={16}/>} {pending?'Retry paper submission':'Confirm Paper Trade'}</button></div>
  </div>
 </Modal>;
}
