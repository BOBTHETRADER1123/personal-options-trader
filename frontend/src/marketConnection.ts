/** Reconcile market-data subscriptions when an authenticated browser opens or returns. */
export type MarketConnectionResult = { status?: string; ok?: boolean; message?: string; retry_after_seconds?: number };
type ConnectionOptions = {
  request: (signal: AbortSignal) => Promise<MarketConnectionResult>;
  visible: () => boolean;
  subscribe: (listener: () => void) => () => void;
  onResult?: (result: MarketConnectionResult) => void;
  onFailure?: () => void;
  onAuthRequired?: () => void;
};

const MIN_ATTEMPT_INTERVAL = 15_000;
const REQUEST_TIMEOUT = 15_000;
const MAX_RETRY_DELAY = 60_000;
const MAX_ATTEMPTS = 3;

export function startMarketAutoConnect(options: ConnectionOptions): () => void {
  let stopped = false;
  let authRequired = false;
  let visible = options.visible();
  let attempts = 0;
  let nextAttemptAt = 0;
  let pending: AbortController | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let requestTimer: ReturnType<typeof setTimeout> | undefined;

  function clearRetry() {
    clearTimeout(retryTimer);
    retryTimer = undefined;
  }

  function retry(retryAfterSeconds?: number) {
    const requestedDelay = (retryAfterSeconds ?? 0) * 1000;
    const delay = Math.max(
      MIN_ATTEMPT_INTERVAL * 2 ** (attempts - 1),
      Number.isFinite(requestedDelay) ? requestedDelay : 0,
    );
    nextAttemptAt = Math.max(nextAttemptAt, Date.now() + delay);
    if (!visible) return;
    if (attempts >= MAX_ATTEMPTS || delay > MAX_RETRY_DELAY) {
      options.onFailure?.();
      return;
    }
    void connect();
  }

  async function connect() {
    if (stopped || authRequired || !visible || pending || attempts >= MAX_ATTEMPTS) return;
    clearRetry();
    const remaining = nextAttemptAt - Date.now();
    if (remaining > 0) {
      // Keep the server cooldown across visibility changes, including long cooldowns.
      if (remaining <= MAX_RETRY_DELAY) retryTimer = setTimeout(() => { void connect(); }, remaining);
      return;
    }

    attempts += 1;
    nextAttemptAt = Date.now() + MIN_ATTEMPT_INTERVAL;
    const controller = new AbortController();
    pending = controller;
    let timedOut = false;
    requestTimer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT);
    let outcome: { result: MarketConnectionResult } | { error: unknown };
    try {
      // The request reconciles subscriptions without forcing a healthy feed restart.
      outcome = { result: await options.request(controller.signal) };
    } catch (error) {
      outcome = { error };
    } finally {
      clearTimeout(requestTimer);
      requestTimer = undefined;
      pending = undefined;
    }

    if (stopped) return;
    if (timedOut) {
      retry();
      return;
    }
    if ('result' in outcome) {
      const result = outcome.result;
      if (result?.status === 'rate_limited') retry(result.retry_after_seconds);
      else if (visible) {
        if (result) options.onResult?.(result);
        else options.onFailure?.();
      }
      return;
    }

    const error = outcome.error as { status?: number; retryAfterSeconds?: number } | undefined;
    const status = error?.status;
    if (status === 401 || status === 403) {
      authRequired = true;
      (options.onAuthRequired ?? options.onFailure)?.();
      return;
    }
    if (status === 408 || status === 429 || (typeof status === 'number' && status >= 500 && status < 600)
        || (status === undefined && outcome.error instanceof TypeError)) {
      retry(error?.retryAfterSeconds);
    } else if (visible) options.onFailure?.();
  }

  const unsubscribe = options.subscribe(() => {
    const nextVisible = options.visible();
    if (nextVisible === visible) return;
    visible = nextVisible;
    if (!visible) {
      clearRetry();
      return;
    }
    // If a request is in flight, let it settle before using the new visit's budget.
    attempts = pending ? 1 : 0;
    void connect();
  });
  void connect();
  return () => {
    stopped = true;
    clearRetry();
    clearTimeout(requestTimer);
    pending?.abort();
    unsubscribe();
  };
}
