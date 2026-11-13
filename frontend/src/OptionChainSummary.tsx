import { useMemo } from 'react';
import { money, num } from './api';
import type { Row } from './components';
import './OptionChainSummary.css';

type Props = {
  summary: Row;
  rows: Row[];
  selected: Row | null;
  trades?: Row[];
  mode: 'sample' | 'live';
  broker: string;
};

type Tone = 'positive' | 'negative' | 'neutral';
const suppliedNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const identity = (value: unknown) => typeof value === 'string' ? value.trim().toUpperCase() : '';
const pnlTone = (value: number | null): Tone => value === null || value === 0 ? 'neutral' : value > 0 ? 'positive' : 'negative';
const currency = (value: number | null) => value === null ? '—' : money(value);

function SummaryCard({ label, value, note, description, tone = 'neutral' }: {
  label: string;
  value: string;
  note?: string;
  description?: string;
  tone?: Tone;
}) {
  return <div className="ocs-card" title={description || note}>
    <span className="ocs-label">{label}</span>
    <strong className={`ocs-value ocs-${tone}`}>{value}</strong>
    {note && <small className="ocs-note">{note}</small>}
  </div>;
}

export default function OptionChainSummary({ summary, selected, trades, mode, broker }: Props) {
  const symbol = identity(selected?.symbol);
  const exchange = identity(selected?.exchange);
  const scopeReady = Boolean(symbol && exchange);
  const recordsLoaded = Array.isArray(trades);
  const accountReady = scopeReady && recordsLoaded;
  const records = useMemo(() => {
    if (!scopeReady || !Array.isArray(trades)) return [];
    return trades.filter(trade => {
      if (trade.broker !== broker || trade.demo !== (mode === 'sample') || identity(trade.underlying) !== symbol) return false;
      // Aggregate backend trade figures only when the entire trade matches this exchange.
      const legExchanges = Array.isArray(trade.legs) ? trade.legs.map((leg: Row) => identity(leg.exchange)) : [];
      if (legExchanges.length) return legExchanges.every((value: string) => value === exchange);
      return identity(trade.exchange) === exchange;
    });
  }, [scopeReady, trades, broker, mode, symbol, exchange]);

  const account = useMemo(() => {
    function sumRecorded(key: string): number | null {
      if (!accountReady) return null;
      let total = 0;
      for (const trade of records) {
        if (!suppliedNumber(trade[key])) return null;
        total += trade[key];
      }
      return Number.isFinite(total) ? total : null;
    }
    function countOpenPositions(): number | null {
      if (!accountReady) return null;
      let total = 0;
      for (const trade of records) {
        if (['CLOSED', 'CANCELLED'].includes(identity(trade.status))) continue;
        if (!Array.isArray(trade.legs) || trade.legs.some((leg: Row) => !suppliedNumber(leg.open_lots))) return null;
        total += trade.legs.filter((leg: Row) => leg.open_lots > 0).length;
      }
      return total;
    }
    const closed = records.filter(trade => identity(trade.status) === 'CLOSED' && suppliedNumber(trade.net_pnl));
    const wins = closed.filter(trade => trade.net_pnl > 0).length;
    return {
      unrealized: sumRecorded('unrealized_pnl'),
      realized: sumRecorded('realized_pnl'),
      premium: sumRecorded('net_premium'),
      openPositions: countOpenPositions(),
      winRate: accountReady && closed.length ? wins / closed.length * 100 : null,
      wins,
      valuedClosed: closed.length,
      stale: records.some(trade => trade.valuation_stale || trade.valuation_error),
    };
  }, [accountReady, records]);

  const metrics = [
    { label: 'ATM IV', value: suppliedNumber(summary.atm_iv) ? `${num(summary.atm_iv)}%` : '—' },
    { label: 'Days to expiry', value: suppliedNumber(summary.days_to_expiry) ? num(summary.days_to_expiry, 0) : '—' },
    { label: 'Market lot', value: suppliedNumber(summary.lot_size) ? num(summary.lot_size, 0) : '—' },
    { label: 'Put / call ratio', value: suppliedNumber(summary.pcr) ? num(summary.pcr) : '—' },
  ];
  const premiumDirection = account.premium === null ? 'Unavailable from loaded records'
    : `${account.premium > 0 ? 'received' : account.premium < 0 ? 'paid' : 'No net premium'} · futures excluded`;

  return <div className="oc-reference-summary">
    <section className="ocs-metrics" aria-label="Option chain metrics">
      {metrics.map(metric => <SummaryCard key={metric.label} label={metric.label} value={metric.value}
        note={metric.value === '—' ? 'Unavailable from source' : undefined} />)}
      {['Max pain', 'Resistance', 'Support'].map(label => <SummaryCard key={label} label={label} value="—"
        note="Not supplied by broker" description="Not supplied by broker" />)}
    </section>
    <section className="ocs-paper-account" aria-label="Selected underlying paper account">
      <p className="ocs-scope">Selected underlying · loaded paper records
        {scopeReady && <span>{selected?.symbol} · {selected?.exchange} · {mode === 'sample' ? 'Sample' : 'Market-data paper'}</span>}
      </p>
      <div className="ocs-accounts">
        <SummaryCard label="Unrealized gross P&L" value={currency(account.unrealized)} tone={pnlTone(account.unrealized)}
          note={account.unrealized === null ? 'Unavailable from loaded records' : account.stale ? 'Last-known valuations · before charges' : 'Before charges'} />
        <SummaryCard label="Realized gross P&L" value={currency(account.realized)} tone={pnlTone(account.realized)}
          note={account.realized === null ? 'Unavailable from loaded records' : 'Loaded records · before charges'} />
        <SummaryCard label="Open positions" value={account.openPositions === null ? '—' : num(account.openPositions, 0)}
          note={account.openPositions === null ? 'Unavailable from loaded records' : 'Open legs in loaded trades'} />
        <SummaryCard label="Net option premium" value={account.premium === null ? '—' : money(Math.abs(account.premium))}
          note={premiumDirection} description="Sum of supplied option-premium cash flows in these loaded paper trades. Futures are excluded." />
        <SummaryCard label="Recent win rate" value={account.winRate === null ? '—' : `${num(account.winRate, 0)}%`}
          note="Loaded closed trades only"
          description={account.winRate === null ? 'No loaded closed trades with a supplied net P&L.' : `${account.wins} profitable of ${account.valuedClosed} loaded closed trades with a supplied net P&L; after charges.`} />
      </div>
    </section>
  </div>;
}
