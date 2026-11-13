import type { Row } from './components';

export type WatchCategory='stocks'|'fno'|'commodities';
export const watchCategories:Record<WatchCategory,string>={stocks:'Stocks',fno:'F&O',commodities:'Commodities'};
export const numberValue=(value:unknown):number|null=>value!==null&&value!==undefined&&value!==''&&typeof value!=='boolean'&&Number.isFinite(Number(value))?Number(value):null;
export const contractId=(contract:Row)=>String(contract.instrument_id||contract.id||'');
export function latestWatchQuote(item:Row,quotes:Row[]):Row|null{
 const contract=item.contract||{},id=String(item.instrument_id||contractId(contract));
 const candidates=[item.quote,...quotes].filter((quote):quote is Row=>Boolean(quote&&quote.broker==='kotak'&&quote.instrument_id===id&&quote.demo===false&&Number.isFinite(Date.parse(quote.received_at||''))&&(!quote.exchange||quote.exchange===contract.exchange)&&(!quote.segment||quote.segment===contract.segment)));
 return candidates.sort((a,b)=>Date.parse(b.received_at||'')-Date.parse(a.received_at||''))[0]||null;
}
export function watchQuoteAge(quote:Row|null,now=Date.now()):number|null{
 const stamp=quote?.received_at?Date.parse(quote.received_at):NaN;
 return Number.isFinite(stamp)?Math.max(0,(now-stamp)/1000):null;
}
export function watchQuoteStale(quote:Row|null,staleAfter:number,now=Date.now()):boolean{
 const age=watchQuoteAge(quote,now);
 return !quote||quote.stale===true||age===null||age>staleAfter;
}
export function watchChange(quote:Row|null):{change:number|null;percent:number|null}{
 const price=numberValue(quote?.ltp),close=numberValue(quote?.previous_close);
 const change=numberValue(quote?.change)??(price!==null&&close!==null&&close>0?price-close:null);
 const percent=numberValue(quote?.change_percent)??(change!==null&&close!==null&&close>0?change/close*100:null);
 return {change,percent};
}
export function matchingWatchPositions(trades:Row[],id:string):Row[]{
 return trades.filter(trade=>trade.broker==='kotak'&&trade.demo===false&&['OPEN','PENDING'].includes(String(trade.status).toUpperCase())&&trade.legs?.some((leg:Row)=>leg.instrument_id===id&&(Number(leg.open_lots)>0||String(leg.status).toLowerCase()==='pending')));
}
export function reorderedWatchIds(items:Row[],moving:string,target:string):string[]{
 const ids=items.map(item=>String(item.instrument_id));
 const from=ids.indexOf(moving),to=ids.indexOf(target);
 if(from<0||to<0||from===to)return ids;
 ids.splice(from,1);ids.splice(to,0,moving);return ids;
}
export function successfulPaperTrade(result:Row):Row|null{
 if(result.mode!=='PAPER')return null;
 const confirmed=(result.results||[]).find((row:Row)=>row.broker==='kotak'&&row.success===true&&row.trade_id&&row.trade?.id===row.trade_id);
 return confirmed?.trade||null;
}
