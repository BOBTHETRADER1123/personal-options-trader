import { useMemo } from 'react';
import { money, num } from './api';
import { breakevenText, payoffAt, previewRisk } from './tradeMath';
import type { Row } from './components';
import './ChainBasketPreview.css';

export default function ChainBasketPreview({legs,reference,stale=false}:{legs:Row[];reference?:number|null;stale?:boolean}){
 const result=useMemo(()=>{
  const risk=previewRisk(legs,reference??0,0);
  if(!risk.supported)return {risk,path:'',zero:0};
  const strikes=legs.map(leg=>Number(leg.strike)).filter(Number.isFinite),middle=reference!=null&&Number.isFinite(reference)&&reference>0?reference:strikes[Math.floor(strikes.length/2)];
  const lo=Math.min(...strikes,middle),hi=Math.max(...strikes,middle),padding=Math.max((hi-lo)*.4,Math.abs(middle)*.025,1),left=Math.max(0,lo-padding),right=hi+padding;
  const points=Array.from({length:81},(_,i)=>({x:left+(right-left)*i/80,y:payoffAt(legs,left+(right-left)*i/80)}));
  const minimum=Math.min(0,...points.map(point=>point.y)),maximum=Math.max(0,...points.map(point=>point.y)),height=maximum-minimum||1;
  return {risk,path:points.map((point,i)=>`${i?'L':'M'}${(i/80*238+6).toFixed(2)},${(76-(point.y-minimum)/height*68).toFixed(2)}`).join(' '),zero:76-(0-minimum)/height*68};
 },[legs,reference]);
 const risk=result.risk;
 return <div className="oc-payoff-preview"><div className="oc-payoff-graph">{risk.supported?<svg viewBox="0 0 250 84" role="img" aria-label="Indicative expiry payoff; chart range does not determine theoretical limits"><line x1="6" x2="244" y1={result.zero} y2={result.zero} stroke="var(--oc-muted)" strokeDasharray="3 4" opacity=".55"/><path d={result.path} stroke="var(--oc-mint)" fill="none" strokeWidth="2.3" strokeLinejoin="round"/></svg>:<span>Payoff unavailable</span>}<small>Indicative expiry payoff · LTP basis · fees excluded</small></div><div className="oc-payoff-values">{risk.supported?<><span>Maximum profit <strong className="oc-positive">{risk.profitUnlimited?'Unlimited':money(risk.maxProfit)}</strong></span><span>Maximum loss <strong className="oc-negative">{risk.lossUnlimited?'Unlimited':money(risk.maxLoss)}</strong></span><span>Breakevens <strong>{breakevenText(risk.breakevens,risk.breakevenIntervals,value=>num(value))}</strong></span></>:<p>{risk.reason||'Verified premiums and contract quantities are required.'}</p>}{stale&&<p>Last-known quotes · refresh every leg before a new paper trade.</p>}</div></div>;
}
