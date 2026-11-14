import { money, num } from './api';
import { Help, Pnl } from './components';
import type { Row } from './components';
import { breakevenText, riskWarnings } from './tradeMath';
import MarginBreakdown from './MarginBreakdown';

/** Recorded results and the risk of remaining exposure are different scopes. */
export default function TradeRiskDetails({trade}:{trade:Row}){
 const legs:Row[]=trade.legs||[],risk:Row=trade.risk||{};
 const status=String(trade.status||'').toUpperCase(),finished=['CLOSED','CANCELLED'].includes(status);
 const open=legs.filter(leg=>Number(leg.open_lots)>0&&String(leg.status||'').toLowerCase()!=='pending');
 const hasExposure=!finished&&open.length>0;
 const pending=!finished&&!hasExposure&&(status==='PENDING'||legs.some(leg=>String(leg.status||'').toLowerCase()==='pending'));
 const stock=trade.instrument_type==='stock'||legs.some(leg=>leg.option_type==='EQ');
 const supported=risk.supported===true,source={...risk,...trade};
 const recorded=(value:unknown)=>typeof value==='number'&&Number.isFinite(value)?value:null;
 const pnl=(value:unknown)=>{const amount=recorded(value);return amount==null?'Unavailable':<Pnl value={amount}/>;};
 const premium=recorded(risk.net_premium);
 const resultRows=<><div><span>{finished?'Final realized gross P&L':'Recorded realized gross P&L'}</span><strong>{pnl(trade.realized_pnl)}</strong></div><div><span>Recorded charges</span><strong>{recorded(trade.charges)==null?'Unavailable':money(trade.charges)}</strong></div><div><span>{finished?'Final net P&L':'Recorded net P&L'}</span><strong>{pnl(trade.net_pnl)}</strong></div></>;
 if(!hasExposure&&!pending)return <div className="detail-risk">
  <Help>{status==='CANCELLED'?'This paper trade is cancelled. ':finished?'This paper trade is closed. ':''}No remaining exposure. Recorded results are shown below; they are not theoretical expiry limits.</Help>
  {resultRows}
  <div><span>Maximum expiry profit / loss</span><strong>Not applicable · no remaining exposure</strong></div>
  <div><span>Expiry breakevens</span><strong>Not applicable · no remaining exposure</strong></div>
 </div>;
 return <div className="detail-risk">
  {pending&&<><Help>Pending orders are not filled. There is no executed exposure yet. Any potential risk below assumes those pending orders fill; it is not a recorded position or broker margin.</Help>{resultRows}</>}
  {riskWarnings(risk).map((warning:Row|string,index:number)=><Help key={index}>{typeof warning==='string'?warning:warning.message||'Check premium consistency and source timestamps.'}</Help>)}
  {!supported?<Help>{risk.reason||(pending?'Potential risk is unavailable until the pending-order assumptions can be valued. No zero-risk value is assumed.':'Risk estimates are not available for this remaining exposure. No zero-risk value is assumed.')}</Help>:<>
   <div><span>{pending?'Potential maximum expiry profit if filled':'Maximum expiry profit after charges'}</span><strong className="positive">{risk.profit_unlimited?'Unlimited':risk.max_profit!=null?money(risk.max_profit):'Unavailable'}</strong></div>
   <div><span>{pending?'Potential maximum expiry loss if filled':'Maximum expiry loss after charges'}</span><strong className="negative">{risk.loss_unlimited?'Unlimited':risk.max_loss!=null?money(risk.max_loss):'Unavailable'}</strong></div>
  </>}
  {!stock&&<MarginBreakdown source={source}/>}
  {supported&&<><div><span>{pending?'Potential net option premium if filled':'Net option premium · open legs'}</span><strong>{premium==null?'Unavailable':premium===0?'No net premium':`${money(Math.abs(premium))} ${premium>0?'credit':'debit'}`}</strong></div><div><span>{pending?'Potential breakevens if filled':'Breakevens'}</span><strong>{breakevenText(risk.breakevens,risk.breakeven_intervals,value=>num(value,2))}</strong></div><Help>{Array.isArray(risk.assumptions)?risk.assumptions.join(' '):risk.assumptions||(pending?'Potential expiry payoff uses pending-order assumptions. No execution is assumed.':'Expiry payoff includes realized P&L and recorded charges. Broker basket margin is separate and requires verified data.')}</Help></>}
 </div>;
}
