import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, Check, ChevronDown, ChevronUp, Clock3, DownloadCloud, Layers, Loader2, Minus, Plus, RefreshCw, Search, Settings2, ShieldCheck, Trash2, X } from 'lucide-react';
import { api, money, num, time, uid } from './api';
import type { Row } from './components';
import './OptionChain.css';

export type ChainSelection = {
  nonce: string;
  broker: string;
  underlying: string;
  name: string;
  mode: 'sample' | 'live';
  legs: Row[];
  contracts: Row[];
  quotes?: Row[];
};

type Underlying = { key: string; symbol: string; name: string; exchange: string; kind: string; expiries?: string[]; lot_size?: number; contract_count?: number };
type ChainLeg = Row & { instrument_id: string; contract?: Row; ltp?: number | null; option_type: string; strike: number; expiry: string; lot_size: number };
type StrikeRow = { strike: number; pcr?: number | null; atm?: boolean; itm_call?: boolean; itm_put?: boolean; ce?: ChainLeg | null; pe?: ChainLeg | null };
type ChainResponse = Row & { underlying: Underlying; rows: StrikeRow[]; instruments: Row[]; quotes?: Row[]; summary: Row; expiries: string[]; expiry: string };
type BasketLeg = { key: string; side: 'BUY' | 'SELL'; lots: number; contract: Row; quote?: Row; data: ChainLeg };
type BasketContext = { key: string; underlying: string; label: string; broker: string; mode: 'sample' | 'live'; tradingEnabled: boolean };
type Column = { key: string; label: string; title: string; width: number; format: 'quantity' | 'percent' | 'number' | 'greek' | 'buildup' };
const COLUMNS: Column[] = [
  { key: 'buildup', label: 'Buildup', title: 'Price and open-interest direction', width: 130, format: 'buildup' },
  { key: 'oi', label: 'OI', title: 'Open interest', width: 98, format: 'quantity' },
  { key: 'oi_change_percent', label: 'OI chg (%)', title: 'Open-interest change from the supplied prior value', width: 95, format: 'percent' },
  { key: 'volume', label: 'Volume', title: 'Traded volume', width: 98, format: 'quantity' },
  { key: 'volume_change_percent', label: 'Vol chg (%)', title: 'Volume change from the supplied prior volume', width: 98, format: 'percent' },
  { key: 'iv', label: 'IV', title: 'Broker-supplied implied volatility (%)', width: 70, format: 'number' },
  { key: 'delta', label: 'Delta', title: 'Broker-supplied delta', width: 75, format: 'greek' },
  { key: 'gamma', label: 'Gamma', title: 'Broker-supplied gamma', width: 82, format: 'greek' },
  { key: 'theta', label: 'Theta', title: 'Broker-supplied theta', width: 82, format: 'greek' },
  { key: 'vega', label: 'Vega', title: 'Broker-supplied vega', width: 82, format: 'greek' },
  { key: 'bid', label: 'Bid', title: 'Best bid premium', width: 82, format: 'number' },
  { key: 'ask', label: 'Ask', title: 'Best ask premium', width: 82, format: 'number' },
  { key: 'ltp_change_percent', label: 'LTP chg (%)', title: 'Premium change from the supplied previous close', width: 95, format: 'percent' },
];
const DEFAULT_COLUMNS = COLUMNS.slice(0, 7).map(c => c.key);
const finite = (value: unknown): value is number => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
function valueText(value: unknown, digits = 2) { return finite(value) ? num(Number(value), digits) : '—'; }
function percentText(value: unknown) { return finite(value) ? `${Number(value) > 0 ? '+' : ''}${num(Number(value), 2)}%` : '—'; }
function changeTone(value: unknown) { return !finite(value) || Number(value) === 0 ? '' : Number(value) > 0 ? 'oc-positive' : 'oc-negative'; }
function compact(value: unknown) {
  if (!finite(value)) return '—';
  const n = Number(value), a = Math.abs(n);
  if (a >= 10_000_000) return `${num(n / 10_000_000, 2)} Cr`;
  if (a >= 100_000) return `${num(n / 100_000, 2)} L`;
  if (a >= 1_000) return `${num(n / 1_000, 2)} K`;
  return num(n, Number.isInteger(n) ? 0 : 2);
}
function expiryLabel(expiry: string, year = false) {
  const d = new Date(`${expiry}T00:00:00+05:30`);
  return Number.isNaN(d.getTime()) ? expiry : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', ...(year ? { year: '2-digit' } : {}) });
}
function buildupTone(value: unknown) {
  const s = String(value || '').toLowerCase();
  return s.includes('long buildup') || s.includes('short covering') ? 'oc-positive' : s.includes('short buildup') || s.includes('long unwinding') ? 'oc-negative' : '';
}
function instrumentId(contract: Row) { return String(contract.instrument_id || contract.id || ''); }

export default function OptionChain({ initialMode = 'sample', onBuildStrategy, onNotify }: { initialMode?: 'sample' | 'live'; onBuildStrategy: (selection: ChainSelection) => void; onNotify: (message: string, error?: boolean) => void }) {
  const [mode, setMode] = useState<'sample' | 'live'>(initialMode);
  const [broker, setBroker] = useState('kotak');
  const [exchange, setExchange] = useState('all');
  const [query, setQuery] = useState(''), [searchTerm, setSearchTerm] = useState('');
  const [universe, setUniverse] = useState<Underlying[]>([]), [total, setTotal] = useState(0), [master, setMaster] = useState<Row>({});
  const [selected, setSelected] = useState<Underlying | null>(null), [expiry, setExpiry] = useState('');
  const [searchOpen, setSearchOpen] = useState(false), [searchIndex, setSearchIndex] = useState(0);
  const [universeLoading, setUniverseLoading] = useState(false), [chainLoading, setChainLoading] = useState(false), [masterLoading, setMasterLoading] = useState(false);
  const [universeError, setUniverseError] = useState(''), [chainError, setChainError] = useState('');
  const [chain, setChain] = useState<ChainResponse | null>(null), [catalogRevision, setCatalogRevision] = useState(0);
  const [columns, setColumns] = useState<string[]>(DEFAULT_COLUMNS), [settingsOpen, setSettingsOpen] = useState(false);
  const [showBuildup, setShowBuildup] = useState(true), [inLots, setInLots] = useState(false), [autoRefresh, setAutoRefresh] = useState(true);
  const [basket, setBasket] = useState<BasketLeg[]>([]), [basketContext, setBasketContext] = useState<BasketContext | null>(null), [basketOpen, setBasketOpen] = useState(true);
  const [clock, setClock] = useState(Date.now());
  const searchRef = useRef<HTMLDivElement>(null), settingsRef = useRef<HTMLDivElement>(null), tableRef = useRef<HTMLDivElement>(null), atmRef = useRef<HTMLTableRowElement>(null);
  const selectedRef = useRef<Underlying | null>(null), universeRequest = useRef(0), chainRequest = useRef(0), lastRefresh = useRef(0);
  const scopeRef = useRef('sample:kotak'), initialScroll = useRef('');
  selectedRef.current = selected;

  useEffect(() => { const timer = setTimeout(() => setSearchTerm(query.trim()), 250); return () => clearTimeout(timer); }, [query]);
  useEffect(() => { const timer = setInterval(() => setClock(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => {
    const close = (event: PointerEvent) => {
      const target = event.target as Node;
      if (searchRef.current && !searchRef.current.contains(target)) setSearchOpen(false);
      if (settingsRef.current && !settingsRef.current.contains(target)) setSettingsOpen(false);
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, []);

  const chooseUnderlying = useCallback((row: Underlying) => {
    selectedRef.current = row;
    setSelected(row); setExpiry(row.expiries?.[0] || ''); setSearchOpen(false); setQuery(''); setChain(null); setChainError('');
  }, []);

  useEffect(() => {
    const controller = new AbortController(), request = ++universeRequest.current;
    setUniverseLoading(true); setUniverseError('');
    const params = new URLSearchParams({ q: searchTerm, mode, broker });
    api(`/api/option-chain/underlyings?${params}`, { signal: controller.signal }).then((result: Row) => {
      if (request !== universeRequest.current) return;
      const items: Underlying[] = (result.underlyings || []).filter((item:Underlying)=>exchange==='all'||item.exchange===exchange);
      setUniverse(items); setTotal(result.total ?? items.length); setMaster(result.master || {}); setSearchIndex(0);
      if (!selectedRef.current && !searchTerm && items.length) chooseUnderlying(items.find(i => i.symbol === 'NIFTY') || items.find(i => i.kind === 'index') || items[0]);
    }).catch(error => { if (error.name !== 'AbortError' && request === universeRequest.current) setUniverseError(error.message); }).finally(() => { if (request === universeRequest.current) setUniverseLoading(false); });
    return () => controller.abort();
  }, [mode, broker, exchange, searchTerm, catalogRevision, chooseUnderlying]);

  const loadChain = useCallback(async (manual = false) => {
    if (!selected?.key) return;
    const now = Date.now();
    if (manual && now - lastRefresh.current < 3000) return;
    lastRefresh.current = now;
    const request = ++chainRequest.current, scope = `${mode}:${broker}:${selected.key}:${expiry}`;
    scopeRef.current = scope;
    setChainLoading(true); setChainError('');
    const params = new URLSearchParams({ underlying: selected.key, mode, broker });
    if (expiry) params.set('expiry', expiry);
    try {
      const result = await api<ChainResponse>(`/api/option-chain?${params}`);
      if (request !== chainRequest.current || scopeRef.current !== scope) return;
      setChain(result);
      if (!expiry && result.expiry) setExpiry(result.expiry);
      const latest = new Map((result.rows || []).flatMap(r => [r.ce, r.pe]).filter((l): l is ChainLeg => Boolean(l)).map(l => [l.instrument_id, l]));
      setBasket(previous => previous.map(item => {
        const updated = latest.get(instrumentId(item.contract));
        if (!updated || item.contract.broker !== broker) return item;
        return { ...item, data: updated, quote: result.quotes?.find(q => q.instrument_id === updated.instrument_id && q.broker === broker) || item.quote };
      }));
    } catch (error) { if (request === chainRequest.current) setChainError((error as Error).message); }
    finally { if (request === chainRequest.current) setChainLoading(false); }
  }, [selected?.key, expiry, mode, broker]);

  useEffect(() => { setChain(null); void loadChain(); return () => { ++chainRequest.current; }; }, [loadChain]);
  useEffect(() => {
    if (!autoRefresh || !selected) return;
    if (mode === 'live' && chain?.status === 'credentials_required') return;
    const interval = chainError || ['unavailable', 'rate_limited'].includes(chain?.status || '') ? 30000 : Math.max(5000, Number(chain?.refresh_after_seconds || 5) * 1000);
    const timer = setInterval(() => { if (document.visibilityState === 'visible') void loadChain(); }, interval);
    return () => clearInterval(timer);
  }, [autoRefresh, selected?.key, loadChain, chain?.status, chain?.refresh_after_seconds, chainError, mode]);
  useEffect(() => {
    const key = `${mode}:${selected?.key}:${expiry}`;
    if (!chain?.rows.length || initialScroll.current === key || !atmRef.current || !tableRef.current) return;
    const timer = setTimeout(() => {
      const viewport = tableRef.current, row = atmRef.current;
      if (viewport && row) { viewport.scrollTop = Math.max(0, row.offsetTop - viewport.clientHeight / 2 + row.clientHeight / 2); viewport.scrollLeft = Math.max(0, (viewport.scrollWidth - viewport.clientWidth) / 2); }
      initialScroll.current = key;
    }, 50);
    return () => clearTimeout(timer);
  }, [chain?.rows.length, mode, selected?.key, expiry]);

  function changeScope(nextMode: 'sample' | 'live', nextBroker = broker) {
    ++chainRequest.current; ++universeRequest.current;
    setMode(nextMode); setBroker(nextBroker); setSelected(null); selectedRef.current = null; setExpiry(''); setChain(null); setQuery(''); setSearchTerm(''); setUniverse([]); setSearchOpen(false); setChainError(''); setUniverseError('');
  }
  async function updateMaster() {
    setMasterLoading(true);
    try { const result=await api(`/api/option-chain/master/refresh?broker=${broker}`, { method: 'POST' }); setCatalogRevision(v => v + 1); onNotify(result.master?.message||'The selected broker’s contracts have been refreshed.',result.ok===false); }
    catch (error) { onNotify((error as Error).message, true); }
    finally { setMasterLoading(false); }
  }
  function addLeg(leg: ChainLeg | null | undefined, side: 'BUY' | 'SELL') {
    if (!leg?.instrument_id || !selected || !chain) return;
    if (basketContext && (basketContext.key !== selected.key || basketContext.mode !== mode || basketContext.broker !== broker)) { onNotify('Clear the basket before adding a different underlying or data source.', true); setBasketOpen(true); return; }
    const contract = leg.contract || chain.instruments?.find(c => instrumentId(c) === leg.instrument_id && c.broker === broker);
    if (!contract) { onNotify('This contract has not been verified in the instrument master.', true); return; }
    if (basket.length >= 20 && !basket.some(item => instrumentId(item.contract) === leg.instrument_id && item.side === side)) { onNotify('A strategy can contain up to 20 legs.', true); return; }
    if (!basketContext) setBasketContext({ key: selected.key, underlying: contract.underlying || selected.symbol, label: selected.symbol, broker, mode, tradingEnabled: chain.trading_enabled !== false && chain.mode_matches_workspace !== false });
    setBasket(previous => {
      const existing = previous.find(item => instrumentId(item.contract) === leg.instrument_id && item.side === side);
      if (existing) return previous.map(item => item === existing ? { ...item, lots: Math.min(1000, item.lots + 1) } : item);
      return [...previous, { key: uid(), side, lots: 1, contract, data: leg, quote: chain.quotes?.find(q => q.instrument_id === leg.instrument_id && q.broker === broker) }];
    });
    setBasketOpen(true);
  }
  function removeLeg(key: string) { setBasket(previous => { const next = previous.filter(item => item.key !== key); if (!next.length) setBasketContext(null); return next; }); }
  function changeLots(key: string, lots: number) { setBasket(previous => previous.map(item => item.key === key ? { ...item, lots: Math.min(1000, Math.max(1, Math.trunc(lots || 1))) } : item)); }
  function build() {
    if (!basketContext || !basket.length) return;
    if(basketContext.mode==='live'&&basket.some(item=>item.data.stale||!finite(item.data.ltp))){onNotify('Refresh every selected leg with its own broker’s fresh quote before building.',true);return;}
    if (!basketContext.tradingEnabled || (chain && basketContext.key === selected?.key && chain.mode_matches_workspace === false)) { onNotify(`Choose ${basketContext.mode === 'sample' ? 'sample' : 'market-data'} mode in workspace Settings before building from this chain.`, true); return; }
    const contracts = basket.map(item => item.contract);
    const quotes = basket.flatMap(item => item.quote ? [item.quote] : []);
    onBuildStrategy({ nonce: uid(), broker: basketContext.broker, underlying: basketContext.underlying, name: `${basketContext.label} · option chain strategy`, mode: basketContext.mode, contracts, quotes,
      legs: basket.map(item => ({ ...item.contract, instrument_id: instrumentId(item.contract), side: item.side, lots: item.lots, enabled: true, price: finite(item.data.ltp) ? Number(item.data.ltp) : 0 })) });
  }

  const activeColumns = useMemo(() => COLUMNS.filter(c => columns.includes(c.key) && (showBuildup || c.key !== 'buildup')), [columns, showBuildup]);
  const rows = chain?.rows || [], summary = chain?.summary || {}, legColumnCount = activeColumns.length + 2;
  const maxOi = useMemo(() => Math.max(1, ...rows.flatMap(r => [r.ce?.oi, r.pe?.oi]).filter(finite).map(Number)), [rows]);
  const updatedAt = chain?.updated_at || chain?.as_of;
  const updatedMs = updatedAt ? Date.parse(updatedAt) : NaN;
  const age = Number.isFinite(updatedMs) ? Math.max(0, Math.floor((clock - updatedMs) / 1000)) : null;
  const stale = Boolean(chain?.stale) || (mode === 'live' && age !== null && age > 15);
  const canNormalize = chain?.quantity_unit === 'units';
  const premiumKnown = basket.length > 0 && basket.every(item => finite(item.data.ltp)&&(!item.contract.demo?Boolean(item.contract.price_multiplier)||item.contract.exchange!=='MCX':true));
  const premium = basket.reduce((sum, item) => sum + (item.side === 'BUY' ? -1 : 1) * Number(item.data.ltp || 0) * item.lots * Number(item.contract.lot_size || 1) * Number(item.contract.price_multiplier || 1), 0);
  const basketBuildEnabled = Boolean(basketContext?.tradingEnabled) && (mode==='sample'||basket.every(item=>!item.data.stale&&finite(item.data.ltp))) && (!chain || basketContext?.key !== selected?.key || chain.mode_matches_workspace !== false);

  function renderCell(leg: ChainLeg | null | undefined, column: Column, side: 'ce' | 'pe') {
    const raw = leg?.[column.key], value = column.format === 'quantity' && inLots && canNormalize && finite(raw) && finite(leg?.lot_size) ? Number(raw) / Number(leg?.lot_size) : raw;
    if (column.format === 'buildup') return <td key={`${side}-${column.key}`} className="oc-buildup-cell"><span className={`oc-buildup ${buildupTone(raw)}`}>{raw ? String(raw).replace(/_/g, ' ') : '—'}</span></td>;
    return <td key={`${side}-${column.key}`} className={`${column.format === 'percent' ? changeTone(value) : ''} ${column.key === 'oi' ? 'oc-oi-cell' : ''}`} title={finite(value) ? `${num(Number(value), 4)}${column.format === 'percent' ? '%' : ''}` : 'Not supplied by this source'}>
      {column.format === 'quantity' ? compact(value) : column.format === 'percent' ? percentText(value) : valueText(value, column.format === 'greek' ? 4 : 2)}
      {column.key === 'oi' && finite(raw) && <i className={`oc-oi-bar ${side}`} style={{ width: `${Math.max(0, Math.min(100, Number(raw) / maxOi * 100))}%` }} />}
    </td>;
  }
  function actions(leg: ChainLeg | null | undefined, type: 'CE' | 'PE') {
    return <td className="oc-action-cell"><div className="oc-leg-actions">{(['BUY', 'SELL'] as const).map(side => {
      const present = basketContext?.key === selected?.key && basketContext?.mode === mode && basketContext?.broker === broker && basket.some(item => instrumentId(item.contract) === leg?.instrument_id && item.side === side);
      return <button key={side} className={`${side === 'BUY' ? 'buy' : 'sell'} ${present ? 'selected' : ''}`} disabled={!leg?.instrument_id||(mode==='live'&&(Boolean(leg?.stale)||!finite(leg?.ltp)))} aria-label={`Add ${side.toLowerCase()} ${type === 'CE' ? 'call' : 'put'} at strike ${leg?.strike ?? 'unavailable'}`} title={`Add ${side.toLowerCase()} ${type}`} aria-pressed={Boolean(present)} onClick={() => addLeg(leg, side)}>{present ? <Check size={11} /> : side === 'BUY' ? 'B' : 'S'}</button>;
    })}</div></td>;
  }
  function ltpCell(leg: ChainLeg | null | undefined) {
    return <td className="oc-ltp-cell" title={leg?.quote_at?`Received ${time(leg.quote_at,true)} IST`:"No quote received"}><strong>{valueText(leg?.ltp)}</strong>{mode==='live'&&leg?.stale&&<small className="oc-negative">Stale</small>}<small className={changeTone(leg?.ltp_change_percent)}>{percentText(leg?.ltp_change_percent)}</small></td>;
  }

  return <div className="option-chain-page">
    <section className="oc-toolbar" aria-label="Option chain controls">
      <div className="oc-instrument-search" ref={searchRef}>
        <Search size={17} />
        <input aria-label="Search F&O stocks and indices" role="combobox" aria-expanded={searchOpen} aria-controls="oc-underlying-results" aria-autocomplete="list" aria-activedescendant={searchOpen && universe[searchIndex] ? `oc-result-${searchIndex}` : undefined} value={searchOpen ? query : selected?.symbol || ''} placeholder="Search F&O stocks & indices" onFocus={() => { setSearchOpen(true); setQuery(''); }} onChange={e => { setQuery(e.target.value); setSearchOpen(true); setSearchIndex(0); }} onKeyDown={event => {
          if (event.key === 'Escape') { setSearchOpen(false); return; }
          if (event.key === 'ArrowDown') { event.preventDefault(); setSearchOpen(true); setSearchIndex(i => Math.min(universe.length - 1, i + 1)); }
          if (event.key === 'ArrowUp') { event.preventDefault(); setSearchIndex(i => Math.max(0, i - 1)); }
          if (event.key === 'Enter' && searchOpen && universe[searchIndex]) { event.preventDefault(); chooseUnderlying(universe[searchIndex]); }
        }} />
        {universeLoading ? <Loader2 className="oc-spin" size={15} /> : <button aria-label="Show underlying list" onClick={() => { setSearchOpen(!searchOpen); setQuery(''); }}><ChevronDown size={16} /></button>}
        {searchOpen && <div className="oc-search-results" id="oc-underlying-results" role="listbox" aria-label="F&O underlyings">
          <div className="oc-search-heading"><span>{mode === 'sample' ? 'Fictional practice contracts' : 'Current instrument master'}</span><small>{total} matches</small></div>
          {universeLoading && !universe.length ? <p className="oc-list-message"><Loader2 className="oc-spin" size={16} />Loading contracts…</p> : universe.length ? universe.map((item, index) => <button id={`oc-result-${index}`} role="option" aria-selected={item.key === selected?.key} className={index === searchIndex ? 'highlighted' : ''} key={item.key} onMouseEnter={() => setSearchIndex(index)} onClick={() => chooseUnderlying(item)}><span><strong>{item.symbol}</strong><small>{item.name || item.symbol}</small></span><span><i>{item.kind === 'index' ? 'INDEX' : item.kind === 'commodity'?'COMMODITY':'STOCK'}</i><small>{item.exchange}</small></span></button>) : <p className="oc-list-message">{universeError || 'No matching option contracts. Try the symbol or company name.'}</p>}
        </div>}
      </div>
      <div className="oc-mode-switch" aria-label="Chain data mode"><button aria-pressed={mode === 'sample'} className={mode === 'sample' ? 'active' : ''} onClick={() => changeScope('sample')}>Sample chain</button><button aria-pressed={mode === 'live'} className={mode === 'live' ? 'active' : ''} onClick={() => changeScope('live')}>Market chain</button></div>
      <label className="oc-source-select"><span>Exchange</span><select aria-label="Option chain exchange" value={exchange} onChange={e=>{setExchange(e.target.value);changeScope(mode,broker);}}><option value="all">All exchanges</option><option value="NSE">NSE</option><option value="BSE">BSE</option><option value="MCX">MCX</option></select></label><label className="oc-source-select"><span>Source</span><select aria-label="Option chain source" value={broker} onChange={e => changeScope(mode, e.target.value)}><option value="kotak">Kotak Neo</option><option value="fivepaisa">5paisa</option></select></label>
      {mode === 'live' && <button className="oc-master-button" onClick={updateMaster} disabled={masterLoading} title="Refresh the official instrument master">{masterLoading ? <Loader2 className="oc-spin" size={15} /> : <DownloadCloud size={15} />}<span>Update contracts</span></button>}
    </section>

    <section className="oc-chain-panel">
      <div className="oc-chain-heading"><div><div className="oc-title-row"><h2>{selected?.symbol || 'Options'} <span>Option Chain</span></h2><span className={`oc-data-tag ${mode === 'sample' ? 'sample' : stale ? 'stale' : ''}`}><i />{mode === 'sample' ? 'SYNTHETIC SAMPLE' : stale ? 'STALE DATA' : chain?.status === 'connected' || chain?.status === 'live' ? 'MARKET DATA' : 'MARKET CATALOG'}</span></div><p>{selected ? `${selected.name || selected.symbol} · ${selected.exchange}` : 'Choose a stock or index to inspect its calls and puts.'}{mode === 'live' && master.updated_at ? ` · Master ${time(master.updated_at, true)}` : ''}</p></div><div className="oc-spot"><span>{mode === 'sample' ? 'Sample reference' : 'Underlying spot'}</span><strong>{valueText(chain?.spot)}</strong></div></div>
      <div className="oc-expiry-row"><span className="oc-control-label">Expiry</span><div className="oc-expiry-tabs">{(chain?.expiries || selected?.expiries || []).slice(0, 3).map(date => <button key={date} className={expiry === date ? 'active' : ''} aria-pressed={expiry === date} onClick={() => setExpiry(date)}>{expiryLabel(date)}</button>)}</div><label className="oc-next-expiry"><select aria-label="Select option expiry" value={expiry} disabled={!selected} onChange={e => setExpiry(e.target.value)}>{!(chain?.expiries || selected?.expiries || []).length && <option value="">Choose expiry</option>}{(chain?.expiries || selected?.expiries || []).map(date => <option key={date} value={date}>{expiryLabel(date, true)}</option>)}</select><ChevronDown size={13} /></label><div className="oc-freshness"><Clock3 size={12} />{chainLoading ? 'Refreshing…' : updatedAt ? `Updated ${time(updatedAt)}${age !== null ? ` · ${age}s ago` : ''}` : 'Awaiting data'}</div></div>
      <div className="oc-summary-row"><div><span>ATM IV</span><strong>{finite(summary.atm_iv) ? `${valueText(summary.atm_iv)}%` : '—'}</strong></div><div><span>Days to expiry</span><strong>{valueText(summary.days_to_expiry, 0)}</strong></div><div><span>Market lot</span><strong>{valueText(summary.lot_size || selected?.lot_size, 0)}</strong></div><div><span>Put / call ratio</span><strong>{valueText(summary.pcr)}</strong></div><div className="oc-display-controls"><label title={canNormalize ? 'Divide OI and volume by each contract’s lot size' : 'This source does not declare quantity units'}><input type="checkbox" checked={inLots} disabled={!canNormalize} onChange={e => setInLots(e.target.checked)} />OI & volume in lots</label><label><input type="checkbox" checked={showBuildup} onChange={e => setShowBuildup(e.target.checked)} />Buildup</label><label><input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} />Auto refresh</label></div><div className="oc-action-tools"><button aria-label="Refresh option chain" title="Refresh option chain; minimum three seconds between requests" disabled={chainLoading || !selected || clock - lastRefresh.current < 3000} onClick={() => void loadChain(true)}><RefreshCw className={chainLoading ? 'oc-spin' : ''} size={16} /></button><div className="oc-settings" ref={settingsRef}><button aria-label="Choose option chain columns" aria-expanded={settingsOpen} title="Choose columns" onClick={() => setSettingsOpen(!settingsOpen)}><Settings2 size={17} /></button>{settingsOpen && <div className="oc-column-popover"><strong>Chain columns</strong><p>LTP, strikes and buy/sell controls stay visible.</p>{COLUMNS.map(column => <label key={column.key}><input type="checkbox" checked={columns.includes(column.key)} onChange={e => setColumns(current => e.target.checked ? [...current, column.key] : current.filter(c => c !== column.key))} />{column.label}</label>)}<button onClick={() => setColumns(DEFAULT_COLUMNS)}>Reset columns</button></div>}</div></div></div>

      {(mode === 'sample' || chain?.message || chain?.mode_matches_workspace === false || universeError || chainError) && <div className={`oc-notice ${chainError || universeError ? 'error' : ''}`} role={chainError || universeError ? 'alert' : 'status'}><ShieldCheck size={15} /><span>{chainError || universeError || chain?.message || (mode === 'sample' ? 'Fictional practice contracts and prices. Sample metrics are not exchange observations.' : '')}{chain?.mode_matches_workspace === false && <strong> Switch the workspace data mode in Settings before building a strategy from this chain.</strong>}</span>{chainError && <button onClick={() => void loadChain(true)}>Retry</button>}</div>}
      {chainLoading && !chain ? <div className="oc-empty"><Loader2 className="oc-spin" size={26} /><h3>Loading the option chain</h3><p>Matching contracts, strikes and available market fields.</p></div> : !rows.length ? <div className="oc-empty"><Layers size={27} /><h3>{universeLoading ? 'Loading F&O contracts' : selected ? 'No option chain available yet' : 'Choose an F&O stock or index'}</h3><p>{chainError || universeError || chain?.message || 'Search the instrument list, select an expiry, and the calls and puts will appear here.'}</p>{mode === 'live' && <button className="oc-secondary" onClick={updateMaster} disabled={masterLoading}>{masterLoading ? 'Updating…' : 'Refresh contract list'}</button>}</div> : <div className="oc-table-scroll" ref={tableRef} tabIndex={0} aria-label="Scrollable call and put option chain">
        <table className="oc-table"><colgroup>{activeColumns.map(c => <col key={`ce-${c.key}`} style={{ width: c.width }} />)}<col style={{ width: 94 }} /><col style={{ width: 80 }} /><col style={{ width: 108 }} /><col style={{ width: 80 }} /><col style={{ width: 94 }} />{[...activeColumns].reverse().map(c => <col key={`pe-${c.key}`} style={{ width: c.width }} />)}</colgroup><thead><tr className="oc-side-head"><th colSpan={legColumnCount} className="oc-call-heading"><span>CALLS <i>CE</i></span><small>Call OI {compact(summary.total_call_oi)}</small></th><th rowSpan={2} className="oc-strike-heading">Strike price<small>Strike PCR</small></th><th colSpan={legColumnCount} className="oc-put-heading"><span>PUTS <i>PE</i></span><small>Put OI {compact(summary.total_put_oi)}</small></th></tr><tr className="oc-column-head">{activeColumns.map(c => <th key={`ce-${c.key}`} title={c.title}>{c.label}</th>)}<th>LTP <small>chg (%)</small></th><th>Buy / Sell</th><th>Buy / Sell</th><th>LTP <small>chg (%)</small></th>{[...activeColumns].reverse().map(c => <th key={`pe-${c.key}`} title={c.title}>{c.label}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.strike} className={row.atm ? 'oc-atm-row' : ''} ref={row.atm ? atmRef : undefined}>{activeColumns.map(c => renderCell(row.ce, c, 'ce'))}{ltpCell(row.ce)}{actions(row.ce, 'CE')}<th scope="row" className={`oc-strike-cell ${row.atm ? 'atm' : ''}`}><strong>{num(row.strike, row.strike % 1 ? 2 : 0)}</strong><small>{row.atm ? <span>ATM {finite(chain?.spot) ? `· ${num(Number(chain?.spot))}` : ''}</span> : valueText(row.pcr)}</small></th>{actions(row.pe, 'PE')}{ltpCell(row.pe)}{[...activeColumns].reverse().map(c => renderCell(row.pe, c, 'pe'))}</tr>)}</tbody></table>
      </div>}
      <div className="oc-table-foot"><span><i className="oc-legend-call" />Calls <i className="oc-legend-put" />Puts <span className="oc-foot-divider">·</span> {rows.length} strikes {expiry ? `· ${expiryLabel(expiry, true)}` : ''}</span><span>— = unavailable from source · LTP is premium per unit · {inLots && canNormalize ? 'OI / volume shown in lots' : 'OI / volume shown as received'}</span></div>
    </section>

    <section className={`oc-basket ${basket.length ? 'has-legs' : ''}`} aria-label="Option chain strategy basket"><div className="oc-basket-head"><button className="oc-basket-toggle" onClick={() => setBasketOpen(!basketOpen)} aria-expanded={basketOpen}><Layers size={18} /><strong>Strategy basket</strong><span>{basket.length} {basket.length === 1 ? 'leg' : 'legs'}</span>{basket.length > 0 && (basketOpen ? <ChevronDown size={14} /> : <ChevronUp size={14} />)}</button>{basket.length ? <div className="oc-basket-actions"><div><small>Indicative premium · fees excluded</small><strong>{premiumKnown ? `${money(Math.abs(premium))} ${premium >= 0 ? 'credit' : 'debit'}` : 'Awaiting prices'}</strong></div><button className="oc-basket-clear" onClick={() => { setBasket([]); setBasketContext(null); }}><Trash2 size={14} />Clear</button><button className="oc-build-button" disabled={!basketBuildEnabled} onClick={build}>Build strategy<ArrowRight size={16} /></button></div> : <p>Use <b>B</b> or <b>S</b> beside a call or put to build a paper strategy.</p>}</div>{basket.length > 0 && basketOpen && <div className="oc-basket-content">{basket.map(item => <div className="oc-basket-leg" key={item.key}><button className={`oc-side-toggle ${item.side === 'BUY' ? 'buy' : 'sell'}`} title="Switch paper leg side" aria-label={`Switch ${item.data.strike} ${item.data.option_type} side from ${item.side}`} onClick={() => setBasket(current => current.map(l => l.key === item.key ? { ...l, side: l.side === 'BUY' ? 'SELL' : 'BUY' } : l))}>{item.side === 'BUY' ? 'B' : 'S'}</button><div className="oc-basket-contract"><strong>{basketContext?.label} {num(Number(item.contract.strike), 0)} <i className={item.contract.option_type === 'CE' ? 'ce' : 'pe'}>{item.contract.option_type}</i></strong><small>{expiryLabel(item.contract.expiry, true)} · {item.contract.lot_size} units / lot · {basketContext?.mode === 'sample' ? 'Sample' : 'Market data'}</small></div><div className="oc-lot-stepper"><button aria-label={`Reduce lots for ${item.contract.strike} ${item.contract.option_type}`} disabled={item.lots <= 1} onClick={() => changeLots(item.key, item.lots - 1)}><Minus size={12} /></button><input type="number" aria-label={`Lots for ${item.contract.strike} ${item.contract.option_type}`} min={1} max={1000} value={item.lots} onChange={e => changeLots(item.key, Number(e.target.value))} /><button aria-label={`Increase lots for ${item.contract.strike} ${item.contract.option_type}`} disabled={item.lots >= 1000} onClick={() => changeLots(item.key, item.lots + 1)}><Plus size={12} /></button></div><strong className="oc-basket-ltp">{valueText(item.data.ltp)}</strong><button className="oc-remove-leg" aria-label={`Remove ${item.contract.strike} ${item.contract.option_type} from basket`} onClick={() => removeLeg(item.key)}><X size={15} /></button></div>)}{!basketBuildEnabled && <p className="oc-basket-warning">Switch workspace Settings to {basketContext?.mode === 'sample' ? 'sample' : 'market-data'} mode to review these legs. Browsing an option chain does not change your workspace mode.</p>}</div>}</section>
    <p className="oc-safety-note"><ShieldCheck size={13} />Read-only market data. Every strategy and order in this workspace is a paper simulation.</p>
  </div>;
}
