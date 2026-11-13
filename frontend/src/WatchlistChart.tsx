import { useId } from 'react';
import { Empty, Modal } from './components';
import type { Row } from './components';
import { money, time } from './api';
import { numberValue } from './watchlistData';

export type WatchObservation={at:string;price:number};
export default function WatchlistChart({contract,observations,quote,onClose}:{contract:Row;observations:WatchObservation[];quote:Row|null;onClose:()=>void}){
 const id=useId().replace(/:/g,'');
 const data=observations.filter(point=>Number.isFinite(point.price)&&point.price>0&&Number.isFinite(Date.parse(point.at)));
 const prices=data.map(point=>point.price),low=Math.min(...prices),high=Math.max(...prices),pad=data.length?Math.max(.01,(high-low)*.12,high*.001):1;
 const lo=low-pad,hi=high+pad,W=900,H=350,L=72,R=22,T=22,B=36;
 const start=data.length?Date.parse(data[0].at):0,end=data.length?Date.parse(data[data.length-1].at):1;
 const x=(at:string)=>L+(Date.parse(at)-start)/(end-start||1)*(W-L-R),y=(price:number)=>T+(hi-price)/(hi-lo||1)*(H-T-B);
 return <Modal title={`${contract.symbol||contract.underlying} · price chart`} subtitle={`${contract.exchange} · Kotak Neo quotes received in this Watchlist session`} onClose={onClose} wide><div className="watch-chart"><div className="watch-chart-summary"><strong>{numberValue(quote?.ltp)!==null?money(Number(quote?.ltp)):'Price unavailable'}</strong><span>Last received {time(quote?.received_at,true)} IST</span></div>{data.length<2?<Empty title="Waiting for price observations" description="The chart needs at least two received quotes. It never creates a synthetic price history."/>:<svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${contract.symbol||contract.underlying} received price history`}><title>Received prices only. Horizontal distance reflects the original quote timestamps.</title><defs><clipPath id={id}><rect x={L} y={T} width={W-L-R} height={H-T-B}/></clipPath></defs>{[0,1,2,3,4].map(index=>{const price=lo+(hi-lo)*index/4,py=y(price);return <g key={index}><line x1={L} x2={W-R} y1={py} y2={py} stroke="var(--border)" strokeDasharray="3 5"/><text x={L-9} y={py+4} textAnchor="end">{money(price)}</text></g>;})}<polyline clipPath={`url(#${id})`} points={data.map(point=>`${x(point.at)},${y(point.price)}`).join(' ')} fill="none" stroke="var(--accent-secondary)" strokeWidth="2.5"/>{[0,1,2,3,4].map(index=>{const point=data[Math.round((data.length-1)*index/4)];return <text key={index} x={x(point.at)} y={H-7} textAnchor={index===0?'start':index===4?'end':'middle'}>{time(point.at)}</text>;})}</svg>}<p className="muted small">Received quote observations since opening this Watchlist, up to 300 points per instrument. This is not an exchange historical-data service. Missing periods are not backfilled; the line only connects received observations.</p></div></Modal>;
}
