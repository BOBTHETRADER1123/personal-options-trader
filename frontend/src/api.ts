export const API_BASE = (import.meta as unknown as {env:Record<string,string>}).env.VITE_API_BASE_URL || '';
let csrf='';
export function setCsrf(token:string){csrf=token;}
export async function api<T=any>(path:string,options:RequestInit={}):Promise<T>{
 const headers:Record<string,string>={'Content-Type':'application/json',...(options.headers as Record<string,string>||{})};
 if(csrf)headers['X-CSRF-Token']=csrf;
 const response=await fetch(API_BASE+path,{...options,headers,credentials:'include'});
 if(!response.ok){const err=await response.json().catch(()=>({detail:response.statusText})); throw new Error(typeof err.detail==='string'?err.detail:JSON.stringify(err.detail||err));}
 if(response.status===204)return undefined as T;
 return response.json();
}
export function uid(){return crypto.randomUUID();}
export const money=(n:number=0,decimals=2)=>new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:decimals,minimumFractionDigits:decimals}).format(Number(n)||0);
export const num=(n:number=0,decimals=2)=>new Intl.NumberFormat('en-IN',{maximumFractionDigits:decimals,minimumFractionDigits:decimals}).format(Number(n)||0);
export const signed=(n:number=0)=>`${n>=0?'+':'−'}${money(Math.abs(n))}`;
export function time(value?:string|null,full=false){if(!value)return '—';const d=new Date(value);return Number.isNaN(d.getTime())?value:new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',...(full?{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'}:{hour:'2-digit',minute:'2-digit',second:'2-digit'}),hour12:false}).format(d);}
export const brokers:Record<string,string>={kotak:'Kotak Neo',fivepaisa:'5paisa'};

export function brokerLabel(id:string){return brokers[id]||(id==='dhan'?'Dhan · legacy history':id);}
