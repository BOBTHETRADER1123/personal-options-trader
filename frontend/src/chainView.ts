/** A fixed user-selected strike window. Quote changes never change its anchor. */
export function visibleStrikeRows<T extends {strike:number;atm?:boolean}>(rows:T[],radius:number|null,anchor:number|null):T[]{
 if(radius===null||anchor===null||!Number.isFinite(anchor)||!rows.length)return rows;
 const count=Math.max(0,Math.trunc(radius));
 let index=0;
 for(let i=1;i<rows.length;i++)if(Math.abs(rows[i].strike-anchor)<Math.abs(rows[index].strike-anchor))index=i;
 return rows.slice(Math.max(0,index-count),Math.min(rows.length,index+count+1));
}
/** Centre only the inner chain viewport, without scrolling the surrounding page. */
export function centerChainViewport(viewport:HTMLElement,row:HTMLElement):void{
 const container=viewport.getBoundingClientRect(),target=row.getBoundingClientRect();
 viewport.scrollTop=Math.max(0,viewport.scrollTop+target.top-container.top-viewport.clientHeight/2+target.height/2);
 viewport.scrollLeft=Math.max(0,(viewport.scrollWidth-viewport.clientWidth)/2);
}

/** Never show a sample response in the market-only chain, even after a stale request. */
export function requireMarketChain<T extends {demo?:boolean;mode?:string}>(response:T):T{
 if(response.demo===true||response.mode==='sample')throw new Error('Kotak market data is unavailable. Sample prices are not displayed in this chain.');
 return response;
}
