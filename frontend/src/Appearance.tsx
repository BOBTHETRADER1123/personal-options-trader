import { useEffect, useRef, useState } from 'react';
import { Check, Moon, Palette, Sun, X } from 'lucide-react';

const backgrounds = [
 {id:'aurora',label:'Aurora',colors:'#8b7bff, #35c8e8'},
 {id:'grid',label:'Grid glow',colors:'#456eff, #35c8e8'},
 {id:'stars',label:'Starfield',colors:'#5655ba, #b586eb'},
 {id:'sunset',label:'Sunset',colors:'#ff8a3d, #f475b2'},
 {id:'emerald',label:'Emerald',colors:'#25b98a, #42b9ca'},
 {id:'royal',label:'Royal',colors:'#8b5cf6, #5c86ef'},
];

export default function Appearance({theme,onThemeChange}:{theme:string;onThemeChange:(theme:string)=>void}){
 const [open,setOpen]=useState(false);
 const [background,setBackground]=useState(()=>{
  const saved=localStorage.getItem('pot-background');
  return backgrounds.some(item=>item.id===saved)?saved!:'aurora';
 });
 const root=useRef<HTMLDivElement>(null),trigger=useRef<HTMLButtonElement>(null);
 useEffect(()=>{document.documentElement.dataset.background=background;localStorage.setItem('pot-background',background);},[background]);
 useEffect(()=>{
  if(!open)return;
  root.current?.querySelector<HTMLButtonElement>('[aria-pressed]')?.focus();
  function closeOutside(event:PointerEvent){if(!root.current?.contains(event.target as Node))setOpen(false);}
  function closeEscape(event:KeyboardEvent){if(event.key==='Escape'){setOpen(false);trigger.current?.focus();}}
  document.addEventListener('pointerdown',closeOutside);document.addEventListener('keydown',closeEscape);
  return()=>{document.removeEventListener('pointerdown',closeOutside);document.removeEventListener('keydown',closeEscape);};
 },[open]);
 return <div className="appearance" ref={root}>
  <button className="icon-btn theme-toggle" aria-label={`Switch to ${theme==='dark'?'light':'dark'} mode`} onClick={()=>onThemeChange(theme==='dark'?'light':'dark')}>{theme==='dark'?<Sun size={19}/>:<Moon size={19}/>}</button>
  <button className="icon-btn appearance-trigger" ref={trigger} aria-label="Appearance" aria-expanded={open} aria-controls="desk-appearance" onClick={()=>setOpen(!open)}><Palette size={19}/></button>
  {open&&<section id="desk-appearance" className="appearance-menu" aria-label="Desk appearance"><div className="appearance-heading"><strong>Make it your desk</strong><button className="icon-btn" aria-label="Close appearance" onClick={()=>{setOpen(false);trigger.current?.focus();}}><X size={16}/></button></div><p>Choose your background</p><div className="appearance-presets">{backgrounds.map(item=><button key={item.id} aria-pressed={background===item.id} onClick={()=>setBackground(item.id)}><span style={{background:`linear-gradient(135deg, ${item.colors})`}}>{background===item.id&&<Check size={18}/>}</span>{item.label}</button>)}</div><small>Saved on this device. Your trading settings stay the same.</small></section>}
 </div>;
}
