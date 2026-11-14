import { useEffect, useRef, useState } from 'react';
import { api, money, num, time } from './api';
import { Help } from './components';
import type { Row } from './components';

type Product='NRML'|'MIS';
export type KotakMarginLeg={key:string;instrumentId:string;symbol:string;side:'BUY'|'SELL';lots:number;lotSize:number;quantity:number;price:number;token:string;segment:string;expiry:string};
const positive=(value:unknown):number|null=>{if(value==null||value===''||typeof value==='boolean')return null;const amount=Number(value);return Number.isFinite(amount)&&amount>0?amount:null;};
const context=(source:Row):Row=>source.trade||source;
const sourceLegs=(source:Row):Row[]=>source.legs||source.trade?.legs||source.risk?.legs||source.analysis?.legs||[];

/** Selection only. The server resolves and validates the current master again. */
export function kotakMarginLegs(source:Row,now=Date.now()):KotakMarginLeg[]{
 const trade=context(source),status=String(trade.status||'').toUpperCase();
 if(source.demo===true||trade.demo===true||trade.demo_mode===true||(trade.broker&&trade.broker!=='kotak')||source.broker_retired||trade.broker_retired||['CLOSED','CANCELLED'].includes(status))return [];
 const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(now);
 return sourceLegs(source).flatMap((raw,index)=>{
  const leg={...raw.contract,...raw},broker=leg.broker||trade.broker||(trade.brokers?.length===1?trade.brokers[0]:'');
  const lots=positive(leg.open_lots??leg.lots),lotSize=positive(leg.lot_size),price=positive(leg.current_price)??positive(leg.entry_price);
  const instrumentId=leg.instrument_id||raw.contract?.instrument_id||raw.contract?.id||(String(leg.id||'').startsWith('kotak:')?leg.id:'');
  const token=String(leg.token||''),segment=String(leg.segment||''),expiry=String(leg.expiry||'');
  if(broker!=='kotak'||leg.demo===true||leg.enabled===false||leg.broker_retired||String(leg.status||'').toLowerCase()==='pending'||leg.pendingLimit===true||!['CE','PE','FUT'].includes(leg.option_type)||!['BUY','SELL'].includes(leg.side)||!['nse_fo','bse_fo','mcx_fo'].includes(segment)||!instrumentId||!/^\d+$/.test(token)||Number(token)<=0||!/^\d{4}-\d{2}-\d{2}$/.test(expiry)||!Number.isFinite(Date.parse(expiry))||expiry<today||lots==null||!Number.isInteger(lots)||lots>1000||lotSize==null||!Number.isInteger(lotSize)||lots*lotSize>100000000||price==null||price>1e9)return [];
  return [{key:String(raw.id||`${instrumentId}:${index}`),instrumentId:String(instrumentId),symbol:String(leg.symbol||`${leg.underlying||''} ${leg.strike||''} ${leg.option_type}`),side:leg.side as 'BUY'|'SELL',lots,lotSize,quantity:lots*lotSize,price,token,segment,expiry}];
 });
}

/** All exposure edits invalidate a check; only the selected live price is bound. */
export function kotakMarginContextFingerprint(source:Row,selected:KotakMarginLeg|undefined,product:Product):string{
 const trade=context(source);
 const exposure=sourceLegs(source).map(raw=>{const leg={...raw.contract,...raw};return [leg.instrument_id||raw.contract?.id||leg.id,leg.broker,leg.segment,leg.token,leg.expiry,leg.option_type,leg.side,leg.enabled,leg.status,leg.open_lots??leg.lots,leg.lot_size,leg.price_multiplier,leg.entry_price,leg.limit_price,leg.order_type];}).map(row=>JSON.stringify(row)).sort();
 return JSON.stringify([trade.id,trade.status,source.demo,trade.demo,source.broker_retired,trade.broker_retired,exposure,selected,product]);
}

/** Account-aware single-order amounts must never pass the basket-margin guard. */
export function visibleKotakMarginAmounts(result:Row|null,leg:KotakMarginLeg|undefined,product:Product,now=Date.now()):{ordMrgn:number;reqdMrgn:number}|null{
 if(!result||!leg||result.broker!=='kotak'||result.status!=='single_order_response'||result.verified!==true||result.scope!=='single_order_real_account_diagnostic'||result.paper_portfolio_verified!==false||result.provider_request_performed!==true||!(/^[a-f0-9]{64}$/.test(result.request_fingerprint||'')))return null;
 const request=result.request||{},age=now-Date.parse(result.as_of||'');
 if(!Number.isFinite(age)||age< -5000||age>=60000||request.exchange_segment!==leg.segment||String(request.instrument_token)!==leg.token||request.transaction_type!==(leg.side==='BUY'?'B':'S')||request.product!==product||request.order_type!=='L'||Number(request.quantity)!==leg.quantity||Number(request.price)!==leg.price)return null;
 const ordMrgn=result.order_margin,reqdMrgn=result.required_margin;
 return typeof ordMrgn==='number'&&Number.isFinite(ordMrgn)&&ordMrgn>=0&&typeof reqdMrgn==='number'&&Number.isFinite(reqdMrgn)&&reqdMrgn>=0?{ordMrgn,reqdMrgn}:null;
}

export function safeKotakMarginError(error:unknown):string{
 const status=(error as {status?:number})?.status;
 if((error as {name?:string})?.name==='AbortError')return 'The single-order check timed out. Try again shortly; no paper position was changed.';
 if(status===401||status===403)return 'Sign in again before requesting a read-only margin check.';
 if(status===429)return 'Margin checks are rate limited. Wait a minute before trying again.';
 if(status===409||status===400||status===422)return 'This margin check is unavailable. Check the selected contract and market-feed connection, then try again.';
 return 'Kotak single-order margin is unavailable. Check the market-feed connection and try again later.';
}

/** Bind the server digest to its exact, already validated non-secret SDK inputs. */
export async function kotakMarginRequestDigest(request:Row):Promise<string>{
 const sorted=Object.fromEntries(Object.keys(request).sort().map(key=>[key,request[key]]));
 const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(sorted)));
 return Array.from(new Uint8Array(digest),value=>value.toString(16).padStart(2,'0')).join('');
}

export default function KotakMarginCheck({source}:{source:Row}){
 const legs=kotakMarginLegs(source),[selectedKey,setSelectedKey]=useState(''),[product,setProduct]=useState<Product>('NRML');
 const selected=legs.find(leg=>leg.key===selectedKey)||legs[0],fingerprint=kotakMarginContextFingerprint(source,selected,product);
 const [result,setResult]=useState<{value:Row;fingerprint:string;receivedAt:number}|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[now,setNow]=useState(Date.now());
 const running=useRef<AbortController|null>(null),currentFingerprint=useRef(fingerprint);currentFingerprint.current=fingerprint;
 useEffect(()=>{running.current?.abort();running.current=null;setResult(null);setError('');setBusy(false);return()=>{running.current?.abort();};},[fingerprint]);
 useEffect(()=>{
  if(!result)return;
  const expires=Math.min(Date.parse(result.value.as_of||'')+60000,result.receivedAt+60000);
  const timer=setTimeout(()=>setNow(Date.now()),Math.max(0,Math.min(60000,expires-Date.now())));
  return()=>clearTimeout(timer);
 },[result]);
 const amounts=result?.fingerprint===fingerprint&&now-result.receivedAt<60000?visibleKotakMarginAmounts(result.value,selected,product,now):null;
 async function check(){
  if(!selected||busy)return;
  const requested=fingerprint,controller=new AbortController();running.current=controller;setBusy(true);setError('');setResult(null);
  const timeout=setTimeout(()=>controller.abort(),30000);
  try{
   const value=await api<Row>('/api/brokers/kotak/margin-required',{method:'POST',signal:controller.signal,body:JSON.stringify({instrument_id:selected.instrumentId,side:selected.side,lots:selected.lots,price:selected.price,product})});
   if(controller.signal.aborted||currentFingerprint.current!==requested)return;
   const checkedAt=Date.now();setNow(checkedAt);
   if(!visibleKotakMarginAmounts(value,selected,product,checkedAt)){setError('Kotak did not return a current recognized single-order margin response. Broker basket margin remains unavailable.');return;}
   const digest=await kotakMarginRequestDigest(value.request);
   if(controller.signal.aborted||currentFingerprint.current!==requested)return;
   if(digest!==value.request_fingerprint){setError('The margin response did not match its request verification. No diagnostic amounts are shown.');return;}
   setResult({value,fingerprint:requested,receivedAt:checkedAt});
  }catch(caught){if(currentFingerprint.current===requested&&running.current===controller)setError(safeKotakMarginError(caught));}
  finally{clearTimeout(timeout);if(running.current===controller){running.current=null;setBusy(false);}}
 }
 return <section className="confirm-action kotak-margin-check">
  <h3>Kotak read-only single-order check</h3>
  <Help>This checks one order against your real Kotak account. Existing account positions and funds can affect the answer. Never add these amounts across legs or use them as basket margin or hedge benefit. No real order is placed.</Help>
  {!selected?<Help>Select a genuine, unexpired Kotak derivative with an open or proposed quantity. Sample, retired, closed, cancelled and unfilled pending legs cannot be checked here.</Help>:<>
   <div className="action-inputs"><label>Selected contract<select aria-label="Margin check contract" value={selected.key} onChange={event=>setSelectedKey(event.target.value)}>{legs.map(leg=><option key={leg.key} value={leg.key}>{leg.symbol} · {leg.side} · {leg.lots} lots</option>)}</select></label><label>Product<select aria-label="Margin check product" value={product} onChange={event=>setProduct(event.target.value as Product)}><option value="NRML">NRML</option><option value="MIS">MIS</option></select></label></div>
   <p>{selected.side} · {num(selected.lots,0)} lots × {num(selected.lotSize,0)} broker units = {num(selected.quantity,0)} units · Reference price {money(selected.price)} · {product} · {selected.segment} · Expiry {selected.expiry}</p>
  </>}
  <button type="button" className="btn secondary" disabled={!selected||busy} onClick={check}>{busy?'Checking single order…':'Check single order'}</button>
  {error&&<Help>{error}</Help>}
  {result&&<><Help>{amounts?`Single-order response · ${time(result.value.as_of,true)} IST · valid for at most 60 seconds for these exact inputs.`:'Not current: the check expired or the selected inputs changed. Check again to display current diagnostic amounts.'}</Help>{amounts&&<div className="detail-risk"><div><span>ordMrgn · single-order API field</span><strong>{money(amounts.ordMrgn)}</strong></div><div><span>reqdMrgn · single-order API field</span><strong>{money(amounts.reqdMrgn)}</strong></div><Help>These are account-aware API amounts, not SPAN/ELM components, verified isolated paper-basket margin or final capital required.</Help></div>}</>}
 </section>;
}
