import { Activity, ArrowRight, ChartNoAxesCombined, Layers, Plus, TrendingUp, Wallet } from 'lucide-react';
import { money } from './api';
import { Badge, Empty, LineChart, Panel, Pnl, Stat, TradeTable } from './components';
import type { Row } from './components';
import './AssetViews.css';

export type TradingCategory='stocks'|'fno'|'commodities';
export const categoryLabels:Record<TradingCategory,string>={stocks:'Stocks',fno:'F&O',commodities:'Commodities'};
export function tradeCategory(trade:Row):TradingCategory{
 if(['stocks','fno','commodities'].includes(trade.category))return trade.category;
 const legs:Row[]=trade.legs||[];
 if(legs.some(leg=>leg.exchange==='MCX')||trade.exchange==='MCX')return 'commodities';
 return trade.instrument_type==='stock'||legs.some(leg=>leg.option_type==='EQ')?'stocks':'fno';
}
const value=(row:Row|undefined,key:string)=>typeof row?.[key]==='number'&&Number.isFinite(row[key])?row[key] as number:null;
export function CategoryPnlCards({summary}:{summary?:Row}){
 const cards=[{id:'stocks',label:'Stocks P&L',icon:<TrendingUp size={20}/>},{id:'fno',label:'F&O P&L',icon:<Layers size={20}/>},{id:'commodities',label:'Commodities P&L',icon:<Activity size={20}/>},{id:'total',label:'Total P&L',icon:<Wallet size={20}/>}];
 return <div className="stats-grid category-pnl-cards">{cards.map(card=>{const pnl=value(summary?.[card.id],'net_pnl');return <Stat key={card.id} label={card.label} value={pnl===null?'Unavailable':<Pnl value={pnl}/>} sub={pnl===null?'Waiting for category totals':'Recorded net paper P&L · charges included'} icon={card.icon} positive={pnl!==null&&pnl>=0}/>;})}</div>;
}
export function CategoryDashboard({category,summary,trades,onSelect,onTrade,onChain,onHistory}:{category:TradingCategory;summary?:Row;trades:Row[];onSelect:(trade:Row)=>void;onTrade:()=>void;onChain:()=>void;onHistory:()=>void}){
 const records=trades.filter(trade=>tradeCategory(trade)===category),active=records.filter(trade=>['OPEN','PENDING'].includes(String(trade.status).toUpperCase())),data=summary?.[category];
 return <div className="asset-dashboard"><div className="asset-toolbar"><Badge tone="green">{categoryLabels[category]} · paper account</Badge><button className="btn primary" onClick={onTrade}><Plus size={16}/>Trade from watchlist</button>{category!=='stocks'&&<button className="btn secondary" onClick={onChain}><ChartNoAxesCombined size={16}/>Option chain</button>}<button className="text-btn" onClick={onHistory}>Trade history<ArrowRight size={15}/></button></div><div className="asset-summary"><div><span>Net paper P&L</span><strong>{value(data,'net_pnl')===null?'Unavailable':<Pnl value={data!.net_pnl}/>}</strong></div><div><span>Realized gross P&L</span><strong>{value(data,'realized_pnl')===null?'Unavailable':money(data!.realized_pnl)}</strong></div><div><span>Unrealized gross P&L</span><strong>{value(data,'unrealized_pnl')===null?'Unavailable':money(data!.unrealized_pnl)}</strong></div><div><span>Estimated charges</span><strong>{value(data,'charges')===null?'Unavailable':money(data!.charges)}</strong></div></div>{category==='stocks'&&<p className="asset-record-note">Cash equity shares have no expiry or six-month limit. Hold paper shares for five years or longer until you choose to sell. Dividends, splits and other corporate actions are not automatically adjusted.</p>}<Panel title={`Open ${categoryLabels[category]} positions`} subtitle="Each paper trade belongs to one category. Open positions continue to use the existing recorder and execution rules."><TradeTable trades={active} onSelect={onSelect} onNew={onTrade}/></Panel><p className="asset-record-note">{data?`${data.active_trades??active.length} active · ${data.closed_trades??'—'} closed trades in this category`:'Category totals become available when the updated backend is connected.'} · Historical records are retained in Trade History.</p></div>;
}
export function CombinedAnalytics({summary,timeline}:{summary?:Row;timeline:Row[]}){
 const categories=(['stocks','fno','commodities'] as TradingCategory[]),maximum=Math.max(1,...categories.map(category=>Math.abs(value(summary?.[category],'net_pnl')??0)));
 const graph=timeline.filter(point=>typeof point.net_pnl==='number').map(point=>({x:point.timestamp,y:point.net_pnl}));
 return <><CategoryPnlCards summary={summary}/><Panel title="Performance by category" subtitle="Recorded net results from the backend · every trade is counted in one category"><div className="asset-comparison">{categories.map(category=>{const pnl=value(summary?.[category],'net_pnl');return <div key={category}><strong>{categoryLabels[category]}</strong><div className="asset-comparison-track" aria-hidden="true"><i className={pnl!==null&&pnl<0?'loss':''} style={{width:pnl===null?'0%':`${Math.abs(pnl)/maximum*100}%`}}/></div><span>{pnl===null?'Unavailable':<Pnl value={pnl}/>}</span></div>;})}</div></Panel><Panel title="Combined paper P&L history" subtitle="Available recorded portfolio observations · no reconstructed or invented prices">{graph.length?<div className="performance-content"><LineChart data={graph} height={270}/></div>:<Empty title="No recorded performance yet" description="Your recorded portfolio P&L will appear here when observations are available."/>}</Panel></>;
}
