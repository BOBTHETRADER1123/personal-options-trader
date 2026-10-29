/** Display-only estimate of the existing paper fill model. The server owns fills. */
export type PreviewRow = Record<string, any>;
export type ExecutionPreview = {
  fillPrice: number | null; previewPrice: number | null; basePrice: number | null;
  ltp: number | null; bid: number | null; ask: number | null;
  wideSpread: boolean; spreadPercent: number | null; fallbackToLtp: boolean;
  missingBidAsk: boolean; quantity: number | null; estimatedCharges: number | null;
  stale: boolean; quoteAgeSeconds: number | null; pendingLimit: boolean;
  limitPrice: number | null; slippagePerUnit: number | null; slippageCost: number | null;
  valid: boolean; reason: string | null; notices: string[];
};
const finite = (value: unknown): number | null => {
  if (value == null || value === '' || typeof value === 'boolean') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};
const positive = (value: unknown): number | null => {
  const number = finite(value);
  return number != null && number > 0 ? number : null;
};
function timestampMs(value: unknown): number | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  // The backend treats a naive ISO quote timestamp as UTC.
  const text = value.trim();
  const parsed = Date.parse(/T/.test(text) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(text) ? `${text}Z` : text);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * MARKET ignores leg.price. LIMIT waits when the configured reference has not
 * crossed; a crossed limit caps the slipped fill at that limit. A pending
 * order has no fill or entry charges: previewPrice is only its limit assumption.
 * Prices remain unrounded, matching backend execution_price/charges arithmetic.
 */
export function executionPreview(
  contract: PreviewRow | null | undefined, leg: PreviewRow,
  quote: PreviewRow | null | undefined, settings: PreviewRow = {}, nowMs = Date.now(),
): ExecutionPreview {
  const side = leg.side;
  const orderType = leg.order_type ?? 'MARKET';
  const ltp = positive(quote?.ltp), bid = positive(quote?.bid), ask = positive(quote?.ask);
  const basis = settings.fill_basis ?? 'bid_ask';
  const sidePrice = side === 'BUY' ? ask : bid;
  const fallbackToLtp = basis === 'bid_ask' && sidePrice == null && ltp != null;
  const basePrice = basis === 'bid_ask' ? sidePrice ?? ltp : ltp;
  const missingBidAsk = bid == null || ask == null;
  const mid = bid != null && ask != null ? (bid + ask) / 2 : null;
  const spreadPercent = mid != null && mid > 0 ? Math.max(0, (ask! - bid!) / mid * 100) : null;
  const wideSpread = spreadPercent != null && spreadPercent >= 5;
  const receivedMs = timestampMs(quote?.received_at);
  const quoteAgeSeconds = receivedMs == null || !Number.isFinite(nowMs) ? null : Math.max(0, (nowMs - receivedMs) / 1000);
  const staleAfter = finite(settings.stale_after_seconds ?? 10);
  const stale = quoteAgeSeconds == null || staleAfter == null || quoteAgeSeconds > staleAfter;
  const limitPrice = orderType === 'LIMIT' ? positive(leg.price ?? leg.limit_price) : null;
  const pendingLimit = orderType === 'LIMIT' && basePrice != null && limitPrice != null &&
    (side === 'BUY' ? basePrice > limitPrice : basePrice < limitPrice);
  const lotSize = positive(contract?.lot_size), lots = positive(leg.lots ?? 1);
  const multiplier = positive(contract?.price_multiplier ?? 1);
  const demo = Boolean(settings.demo_mode ?? contract?.demo);
  const mcxVerified = contract?.exchange !== 'MCX' || demo ||
    (positive(contract?.price_multiplier) != null && Boolean(contract?.monetary_conversion_source));
  const units = lotSize != null && lots != null && multiplier != null &&
    Number.isInteger(lotSize) && Number.isInteger(lots) && mcxVerified ? lotSize * lots * multiplier : null;
  const quantity = units != null && Number.isFinite(units) ? units : null;
  const slippageBps = finite(settings.slippage_bps ?? 0);
  const brokerage = finite(settings.brokerage_per_order ?? 20);
  const chargesPercent = finite(settings.charges_percent ?? 0.05);
  const notices: string[] = [];
  if (fallbackToLtp) notices.push(`${side === 'BUY' ? 'Ask' : 'Bid'} unavailable; the configured paper model falls back to LTP.`);
  else if (missingBidAsk) notices.push('Bid or ask is unavailable; spread cannot be fully assessed.');
  if (wideSpread) notices.push(`Wide bid/ask spread (${spreadPercent!.toFixed(2)}% of midpoint); a paper fill can differ substantially from LTP.`);
  if (bid != null && ask != null && bid > ask) notices.push('The received bid exceeds ask; check the broker quote.');
  if (pendingLimit) notices.push('Limit has not crossed; the order would remain pending with no entry fill or charges.');
  let reason: string | null = null;
  const expectedId = contract?.instrument_id ?? contract?.id;
  if (!contract) reason = 'Select a contract from the broker master.';
  else if (contract.broker && contract.broker !== 'kotak') reason = 'New paper fills use Kotak Neo only.';
  else if (typeof settings.demo_mode === 'boolean' && typeof contract.demo === 'boolean' && settings.demo_mode !== contract.demo) reason = 'The contract does not match the current sample or market-data workspace.';
  else if (!['BUY', 'SELL'].includes(side) || !['MARKET', 'LIMIT'].includes(orderType)) reason = 'Choose a valid paper order direction and type.';
  else if (!quote || ltp == null || basePrice == null) reason = 'A valid quote is required for this paper estimate.';
  else if ((contract.broker && quote.broker !== contract.broker) ||
    (expectedId && quote.instrument_id !== expectedId) ||
    (quote.exchange && quote.exchange !== contract.exchange) || (quote.segment && quote.segment !== contract.segment) ||
    (typeof contract.demo === 'boolean' && typeof quote.demo === 'boolean' && quote.demo !== contract.demo)) reason = 'The quote does not belong to the selected broker contract.';
  else if (orderType === 'LIMIT' && limitPrice == null) reason = 'Enter a positive limit price.';
  else if (quantity == null) reason = mcxVerified ? 'Contract quantity is unavailable or invalid.' : 'MCX monetary units require verified contract metadata.';
  else if (slippageBps == null || slippageBps < 0 || brokerage == null || brokerage < 0 || chargesPercent == null || chargesPercent < 0 || !['ltp', 'bid_ask'].includes(basis)) reason = 'The paper execution settings are invalid.';
  else if (stale) reason = 'The selected market-data quote is stale; wait for a fresh quote before estimating a fill.';
  else if (!demo && contract.expiry && String(contract.expiry).slice(0, 10) < new Date(nowMs + 19800000).toISOString().slice(0, 10)) reason = 'This contract has expired; a new paper entry is unavailable.';
  let fillPrice: number | null = null;
  let slippagePerUnit: number | null = null;
  let estimatedCharges: number | null = null;
  if (!reason && basePrice != null && quantity != null) {
    if (pendingLimit) estimatedCharges = 0;
    else {
      const slip = basePrice * slippageBps! / 10000;
      fillPrice = Math.max(0.01, basePrice + (side === 'BUY' ? slip : -slip));
      if (limitPrice != null) fillPrice = side === 'BUY' ? Math.min(fillPrice, limitPrice) : Math.max(fillPrice, limitPrice);
      slippagePerUnit = Math.abs(fillPrice - basePrice);
      estimatedCharges = brokerage! + fillPrice * quantity * chargesPercent! / 100;
    }
  }
  return {fillPrice, previewPrice: !reason ? (pendingLimit ? limitPrice : fillPrice) : null,
    basePrice, ltp, bid, ask, wideSpread, spreadPercent, fallbackToLtp, missingBidAsk,
    quantity, estimatedCharges, stale, quoteAgeSeconds, pendingLimit, limitPrice,
    slippagePerUnit, slippageCost: slippagePerUnit != null && quantity != null ? slippagePerUnit * quantity : null,
    valid: reason == null, reason, notices};
}

/** Display re-confirmation guard only; the server still validates every fill. */
export function reviewNeedsAnotherConfirmation(previous: PreviewRow, latest: PreviewRow): boolean {
  if (['fill_basis','slippage_bps','brokerage_per_order','charges_percent'].some(key=>previous[key]!==latest[key])) return true;
  const before: PreviewRow[] = previous.previews || [], after: PreviewRow[] = latest.previews || [];
  if (before.length !== after.length) return true;
  return after.some((item,index) => {
    const old = before[index];
    if (!old || item.contract?.id !== old.contract?.id || item.side !== old.side || item.quantity !== old.quantity || item.pendingLimit !== old.pendingLimit) return true;
    if ((item.wideSpread && !old.wideSpread) || (item.fallbackToLtp && !old.fallbackToLtp)) return true;
    const price = Number(item.previewPrice), oldPrice = Number(old.previewPrice);
    return !Number.isFinite(price) || !Number.isFinite(oldPrice) || Math.abs(price-oldPrice)>Math.max(.01,Math.abs(oldPrice)*.01);
  });
}
