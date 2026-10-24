/** Expiry preview uses nonnegative settlement prices; live marks are separate. */
export type RiskLeg = Record<string, any>;
export function monetaryUnits(leg: RiskLeg): number {
  const multiplier=leg.price_multiplier==null?(leg.exchange==='MCX'?NaN:1):Number(leg.price_multiplier);
  const result=Number(leg.open_lots??leg.lots??1)*Number(leg.lot_size)*multiplier;
  return Number.isFinite(result)&&result>=0?result:NaN;
}
export function payoffAt(legs: RiskLeg[], settlement: number, charges=0): number {
  return legs.filter(l=>l.enabled!==false).reduce((sum,leg)=>{
    const terminal=leg.option_type==='FUT'?settlement:leg.option_type==='CE'?Math.max(0,settlement-Number(leg.strike)):Math.max(0,Number(leg.strike)-settlement);
    return sum+(leg.side==='BUY'?1:-1)*(terminal-Number(leg.entry_price??leg.price??0))*monetaryUnits(leg);
  },-charges);
}
export function previewRisk(input: RiskLeg[], reference: number, charges=0) {
  const legs=input.filter(l=>l.enabled!==false&&Number(l.open_lots??l.lots??1)>0);
  const supported=legs.length>0&&new Set(legs.map(l=>`${l.exchange}:${l.underlying}:${l.expiry}`)).size===1&&legs.every(l=>Number.isFinite(monetaryUnits(l)));
  const points=[...new Set([0,...legs.map(l=>Number(l.option_type==='FUT'?(l.entry_price??l.price??0):l.strike))])].sort((a,b)=>a-b);
  const values=points.map(x=>payoffAt(legs,x,charges));
  const slope=legs.reduce((sum,l)=>sum+(['CE','FUT'].includes(l.option_type)?(l.side==='BUY'?1:-1)*monetaryUnits(l):0),0);
  const breakevens:number[]=[];
  for(let i=0;i<points.length;i++){
    if(Math.abs(values[i])<1e-8)breakevens.push(points[i]);
    if(i<points.length-1&&values[i]*values[i+1]<0)breakevens.push(points[i]-values[i]*(points[i+1]-points[i])/(values[i+1]-values[i]));
  }
  if(slope&&values.length&&-values[values.length-1]/slope>0)breakevens.push(points[points.length-1]-values[values.length-1]/slope);
  const maxProfit=slope>0?Infinity:Math.max(...values),maxLoss=slope<0?Infinity:Math.max(0,-Math.min(...values));
  const netPremium=legs.filter(l=>l.option_type!=='FUT').reduce((sum,l)=>sum+(l.side==='BUY'?-1:1)*Number(l.entry_price??l.price??0)*monetaryUnits(l),0);
  const standalone=legs.reduce((sum,l)=>sum+(l.option_type==='FUT'?Number(l.entry_price??l.price??reference)*.2:l.side==='BUY'?Number(l.entry_price??l.price??0):reference*.2)*monetaryUnits(l),0);
  const estimatedMargin=supported&&Number.isFinite(maxLoss)&&legs.some(l=>l.side==='SELL')?Math.min(standalone,maxLoss+Math.max(0,netPremium)):standalone;
  return {supported,maxProfit,maxLoss,breakevens:[...new Set(breakevens.map(v=>Math.round(v*100)/100))],netPremium,estimatedMargin,hedgeBenefit:Math.max(0,standalone-estimatedMargin)};
}
export function findOwnQuote(quotes: RiskLeg[],contract: RiskLeg|undefined,broker: string): RiskLeg|undefined {
  if(!contract||contract.broker!==broker)return undefined;
  return quotes.find(q=>q.broker===broker&&q.instrument_id===(contract.instrument_id||contract.id)&&(!q.exchange||q.exchange===contract.exchange)&&(!q.segment||q.segment===contract.segment));
}
