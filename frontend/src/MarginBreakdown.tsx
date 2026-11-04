import { money, time } from './api';
import { Help } from './components';
import type { Row } from './components';
import { brokerMarginValue } from './tradeMath';

/** Only explicit complete, fresh basket calculations may show portfolio figures. */
export function exchangeMarginValue(source:Row,key:string):number|null{
 const margin=source.exchange_margin;
 if(!margin||margin.verified!==true||!['calculated','verified'].includes(margin.status)||margin.freshness!=='fresh')return null;
 const value=margin[key];
 return typeof value==='number'&&Number.isFinite(value)&&value>=0?value:null;
}
export default function MarginBreakdown({source,layout='detail',includeBroker=true}:{source:Row;layout?:'detail'|'review';includeBroker?:boolean}){
 const exchange=source.exchange_margin||{},broker=source.broker_margin||{};
 const rows:[string,number|null][]=[
  ['Exchange SPAN margin',exchangeMarginValue(source,'span_margin')],
  ['Exchange ELM',exchangeMarginValue(source,'elm')],
  ['Exchange additional / special margin',exchangeMarginValue(source,'additional_margin')],
  ['Total calculated exchange margin',exchangeMarginValue(source,'total_margin')],
  ...(includeBroker?[['Kotak Neo basket margin',brokerMarginValue(source)]] as [string,number|null][]:[]),
  ['Verified hedge benefit',brokerMarginValue(source,'hedge_benefit')],
  ['Final capital required',brokerMarginValue(source,'final_capital_required')],
 ];
 const component=(label:string,value:number|null)=>layout==='review'?<span key={label}>{label}<strong>{value==null?'Unavailable':money(value)}</strong></span>:<div key={label}><span>{label}</span><strong>{value==null?(label==='Kotak Neo basket margin'?'Broker margin unavailable':'Unavailable'):money(value)}</strong></div>;
 return <>
  {layout==='review'?<div className="review-risk">{rows.map(([label,value])=>component(label,value))}</div>:rows.map(([label,value])=>component(label,value))}
  <Help>Exchange source: {exchange.source||'No validated exchange risk calculation'}. Parameter file: {exchange.parameter_version||'Unavailable'}. Data: {exchange.freshness||'unavailable'}. Calculated: {exchange.as_of?`${time(exchange.as_of,true)} IST`:'Not calculated'}. {exchange.reason||'Complete current SPAN, ELM, premium settlement, special and expiry rules must be validated before an exchange total is shown.'}</Help>
  <Help>Broker source: {broker.source||'No verified Kotak Neo basket response'}. Checked: {broker.as_of?`${time(broker.as_of,true)} IST`:'Not checked'}. {broker.reason||'Single-order margin checks do not establish the margin of an isolated paper basket. Expiry loss is separate from margin.'}</Help>
 </>;
}
