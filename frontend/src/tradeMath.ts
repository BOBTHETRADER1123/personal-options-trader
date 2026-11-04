/** Exact piecewise-linear expiry risk on settlement prices S >= 0. */
export type RiskLeg = Record<string, any>;
const amount=(value:unknown):number=>value==null||value===''||typeof value==='boolean'?NaN:Number(value);
const cents=(value:number)=>Math.sign(value)*Math.round((Math.abs(value)+Number.EPSILON)*100)/100;
const nearZero=(value:number)=>Math.abs(value)<1e-7;
const activeLegs=(legs:RiskLeg[])=>legs.filter(leg=>leg.enabled!==false&&amount(leg.open_lots??leg.lots??1)>0);
const groupKey=(leg:RiskLeg)=>`${leg.broker||''}:${leg.exchange||''}:${leg.underlying||''}:${leg.expiry||''}`;
function validExpiry(value:unknown){if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))return false;const date=new Date(value+'T00:00:00Z');return Number.isFinite(date.getTime())&&date.toISOString().slice(0,10)===value;}
export function monetaryUnits(leg: RiskLeg): number {
 const lots=amount(leg.open_lots??leg.lots??1),lotSize=amount(leg.lot_size);
 const multiplier=leg.price_multiplier==null?(leg.exchange==='MCX'?NaN:1):amount(leg.price_multiplier);
 const result=lots*lotSize*multiplier;
 return Number.isInteger(lots)&&lots>=0&&Number.isInteger(lotSize)&&lotSize>0&&Number.isFinite(multiplier)&&multiplier>0&&Number.isFinite(result)?result:NaN;
}
function validation(legs:RiskLeg[],charges:number,realizedPnl:number):string|null{
 if(!Number.isFinite(charges)||charges<0||!Number.isFinite(realizedPnl))return 'Charges and realized P&L must be finite; charges cannot be negative.';
 for(const leg of legs){
  if(!['BUY','SELL'].includes(leg.side)||!['CE','PE','FUT'].includes(leg.option_type))return 'Choose a valid instrument type and direction.';
  for(const key of ['lots','open_lots'])if(leg[key]!=null&&(!Number.isInteger(amount(leg[key]))||amount(leg[key])<0))return 'Lots must be nonnegative whole numbers.';
  if(leg.lots!=null&&leg.open_lots!=null&&amount(leg.open_lots)>amount(leg.lots))return 'Open lots cannot exceed the recorded total lots.';
  if(!Number.isFinite(monetaryUnits(leg)))return 'Quantities require whole lots, an integer contract lot size and verified monetary units.';
  const strike=amount(leg.strike??0),entry=amount(leg.entry_price??leg.price);
  if(!Number.isFinite(strike)||strike<0||(leg.option_type!=='FUT'&&strike<=0))return 'Option strikes must be finite and positive.';
  if(!Number.isFinite(entry)||entry<0)return 'Entry premiums must be finite and nonnegative.';
  if(!validExpiry(leg.expiry))return 'Use a valid contract expiry date.';
  if(typeof leg.underlying!=='string'||!leg.underlying.trim())return 'The underlying must be identified.';
 }
 return null;
}
function valueAt(legs:RiskLeg[],settlement:number,charges:number,realizedPnl:number){
 return legs.reduce((sum,leg)=>{const terminal=leg.option_type==='FUT'?settlement:leg.option_type==='CE'?Math.max(0,settlement-amount(leg.strike)):Math.max(0,amount(leg.strike)-settlement);return sum+(leg.side==='BUY'?1:-1)*(terminal-amount(leg.entry_price??leg.price))*monetaryUnits(leg);},realizedPnl-charges);
}
export function payoffAt(input:RiskLeg[],settlement:number,charges=0,realizedPnl=0):number{
 const legs=activeLegs(input);
 if(!Number.isFinite(settlement)||settlement<0||validation(input.filter(leg=>leg.enabled!==false),charges,realizedPnl)||new Set(legs.map(groupKey)).size>1)return NaN;
 return valueAt(legs,settlement,charges,realizedPnl);
}
function profile(legs:RiskLeg[],charges:number,realizedPnl:number){
 const knots=[...new Set([0,...legs.filter(leg=>leg.option_type!=='FUT').map(leg=>amount(leg.strike))])].sort((a,b)=>a-b);
 const values=knots.map(s=>valueAt(legs,s,charges,realizedPnl));
 const slope=legs.reduce((sum,leg)=>sum+(['CE','FUT'].includes(leg.option_type)?(leg.side==='BUY'?1:-1)*monetaryUnits(leg):0),0);
 const profitUnlimited=slope>1e-8,lossUnlimited=slope< -1e-8;
 const roots:number[]=[],intervals:{from:number;to:number|null}[]=[];
 for(let i=0;i<knots.length-1;i++){
  const x1=knots[i],x2=knots[i+1],y1=values[i],y2=values[i+1];
  if(nearZero(y1)&&nearZero(y2))intervals.push({from:x1,to:x2});
  else{if(nearZero(y1))roots.push(x1);if(nearZero(y2))roots.push(x2);if(y1*y2<0)roots.push(x1-y1*(x2-x1)/(y2-y1));}
 }
 const last=knots.length-1;
 if(nearZero(slope)){if(nearZero(values[last]))intervals.push({from:knots[last],to:null});}
 else{const root=knots[last]-values[last]/slope;if(root>=knots[last]-1e-8)roots.push(Math.max(knots[last],root));}
 const merged:{from:number;to:number|null}[]=[];
 for(const interval of intervals){const previous=merged[merged.length-1];if(previous&&(previous.to===null||Math.abs(previous.to-interval.from)<1e-7))previous.to=interval.to;else merged.push({...interval});}
 const breakevensUnrounded=[...new Set(roots.filter(root=>root>=0&&!merged.some(interval=>root>=interval.from-1e-7&&(interval.to===null||root<=interval.to+1e-7))).sort((a,b)=>a-b))];
 return {maxProfit:profitUnlimited?Infinity:Math.max(...values),maxLoss:lossUnlimited?Infinity:Math.max(0,-Math.min(...values)),profitUnlimited,lossUnlimited,breakevensUnrounded,breakevens:[...new Set(breakevensUnrounded.map(cents))],breakevenIntervals:merged};
}
export function previewRisk(input:RiskLeg[],reference:number,charges=0,realizedPnl=0){
 void reference; // Chart range/reference must never determine theoretical risk.
 const legs=activeLegs(input),reason=validation(input.filter(leg=>leg.enabled!==false),charges,realizedPnl),groups=new Map<string,RiskLeg[]>();
 for(const leg of legs){const key=groupKey(leg);groups.set(key,[...(groups.get(key)||[]),leg]);}
 const netPremium=reason?NaN:legs.filter(leg=>leg.option_type!=='FUT').reduce((sum,leg)=>sum+(leg.side==='BUY'?-1:1)*amount(leg.entry_price??leg.price)*monetaryUnits(leg),0);
 const unsupported=reason||(groups.size>1?'Combined expiry payoff requires one broker, exchange, underlying and expiry. Independent expiry groups are shown separately.':null);
 const validationWarnings:string[]=[];
 if(!reason)for(const rows of groups.values())for(let i=0;i<rows.length;i++)for(let j=i+1;j<rows.length;j++){const a=rows[i],b=rows[j];if(!['CE','PE'].includes(a.option_type)||a.option_type!==b.option_type||amount(a.strike)===amount(b.strike))continue;const low=amount(a.strike)<amount(b.strike)?a:b,high=low===a?b:a,lp=amount(low.entry_price??low.price),hp=amount(high.entry_price??high.price);if((a.option_type==='CE'&&hp>lp+.005)||(a.option_type==='PE'&&hp<lp-.005))validationWarnings.push(`Check ${low.strike}/${high.strike} ${a.option_type} entry premiums: their strike ordering is inconsistent. Entries may come from different timestamps or wide spreads.`);}
 const base={supported:!unsupported,reason:unsupported,validationWarnings:[...new Set(validationWarnings)],netPremium:cents(netPremium),chargesIncluded:charges,realizedPnl,estimatedMargin:null,hedgeBenefit:null,marginStatus:'unavailable',marginReason:'Broker margin unavailable'};
 if(unsupported)return {...base,maxProfit:NaN,maxLoss:NaN,maxProfitGross:NaN,maxLossGross:NaN,profitUnlimited:false,lossUnlimited:false,breakevens:[] as number[],breakevensGross:[] as number[],breakevensUnrounded:[] as number[],breakevenIntervals:[] as {from:number;to:number|null}[],groups:reason?[]:[...groups.values()].map(rows=>({broker:rows[0].broker,exchange:rows[0].exchange,underlying:rows[0].underlying,expiry:rows[0].expiry,...profile(rows,0,0)}))};
 const net=profile(legs,charges,realizedPnl),gross=profile(legs,0,realizedPnl);
 return {...base,...net,maxProfit:net.profitUnlimited?Infinity:cents(net.maxProfit),maxLoss:net.lossUnlimited?Infinity:cents(net.maxLoss),maxProfitGross:gross.profitUnlimited?Infinity:cents(gross.maxProfit),maxLossGross:gross.lossUnlimited?Infinity:cents(gross.maxLoss),breakevensGross:gross.breakevens,groups:[]};
}
/** Only explicit complete verified basket data can be presented as broker margin. */
export function brokerMarginValue(source:RiskLeg|undefined,key='total_margin'):number|null{
 const margin=source?.broker_margin;
 if(!margin||margin.status!=='verified'||margin.verified!==true||margin.stale===true||margin.freshness!=='fresh'||!['isolated_paper_basket','basket'].includes(margin.scope||margin.calculation_scope))return null;
 const value=amount(margin[key]);return Number.isFinite(value)&&value>=0?value:null;
}
export function breakevenText(values:unknown,intervals:unknown,format:(value:number)=>string):string{
 const points=Array.isArray(values)?values.filter(v=>Number.isFinite(Number(v))).map(v=>format(Number(v))):[];
 if(Array.isArray(intervals))for(const interval of intervals){if(Number.isFinite(Number(interval.from)))points.push(`${format(Number(interval.from))}–${interval.to==null?'∞':format(Number(interval.to))}`);}
 return points.join(' / ')||'—';
}
export function findOwnQuote(quotes: RiskLeg[],contract: RiskLeg|undefined,broker:string):RiskLeg|undefined{
 if(!contract||contract.broker!==broker)return undefined;
 return quotes.find(quote=>quote.broker===broker&&quote.instrument_id===(contract.instrument_id||contract.id)&&(!quote.exchange||quote.exchange===contract.exchange)&&(!quote.segment||quote.segment===contract.segment));
}

/** Preserve warning provenance without relying on one historical payload key. */
export function riskWarnings(source:RiskLeg|undefined):string[]{
 return [...new Set([...(source?.validation_warnings||[]),...(source?.premium_warnings||[])].map(warning=>typeof warning==='string'?warning:warning?.message).filter((warning):warning is string=>typeof warning==='string'&&warning.length>0))];
}
