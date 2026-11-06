"""Run the actual browser auto-connect controller with fake requests and timers."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require(process.argv[1]);
const source = fs.readFileSync(process.argv[2], 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
}).outputText;
const flush = () => new Promise(resolve => setImmediate(resolve));
const connected = () => ({status: 'connected', ok: true});
const httpError = (status, retryAfterSeconds) => () => {
  throw Object.assign(new Error('request failed'), {status, retryAfterSeconds});
};
const waitForAbort = signal => new Promise((resolve, reject) => {
  signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), {once: true});
});

function harness(respond, initiallyVisible = true) {
  let now = 0, timerSequence = 0, visible = initiallyVisible, listener;
  let auth = 0, failures = 0;
  const timers = new Map(), calls = [], results = [];
  const context = {
    exports: {}, AbortController, TypeError,
    Date: class extends Date { static now() { return now; } },
    setTimeout: (callback, delay) => {
      const id = ++timerSequence;
      timers.set(id, {callback, due: now + delay});
      return id;
    },
    clearTimeout: id => timers.delete(id),
  };
  vm.runInNewContext(compiled, context);
  const stop = context.exports.startMarketAutoConnect({
    request: async signal => {
      calls.push({at: now, signal});
      return respond(signal, calls.length);
    },
    visible: () => visible,
    subscribe: handler => {
      listener = handler;
      return () => { listener = undefined; };
    },
    onAuthRequired: () => auth++,
    onFailure: () => failures++,
    onResult: result => results.push(result.status),
  });
  return {
    calls, results, stop,
    get auth() { return auth; },
    get failures() { return failures; },
    get timerCount() { return timers.size; },
    get subscribed() { return Boolean(listener); },
    visit: next => { visible = next; listener?.(); },
    times: () => calls.map(call => call.at),
    advance: async duration => {
      const until = now + duration;
      await flush();
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.due <= until)
          .sort((a, b) => a[1].due - b[1].due)[0];
        if (!due) break;
        const [id, timer] = due;
        now = timer.due;
        timers.delete(id);
        timer.callback();
        await flush();
      }
      now = until;
      await flush();
    },
  };
}

const scenarios = {
  async healthy() {
    const h = harness(connected);
    h.visit(true);
    await h.advance(60_000);
    assert.deepEqual(h.times(), [0]);
    assert.deepEqual(h.results, ['connected']);
    assert.equal(h.timerCount, 0);
    h.visit(false);
    h.visit(true);
    await flush();
    assert.deepEqual(h.times(), [0, 60_000]);
    h.stop();
  },
  async closed() {
    const h = harness(() => ({status: 'market_closed', ok: false}), false);
    await h.advance(1_000);
    assert.deepEqual(h.times(), []);
    h.visit(true);
    await h.advance(120_000);
    assert.deepEqual(h.times(), [1_000]);
    assert.deepEqual(h.results, ['market_closed']);
    h.stop();
  },
  async transient() {
    const h = harness((signal, attempt) => {
      if (attempt === 1) return httpError(503)();
      if (attempt === 2) throw new TypeError('network unavailable');
      return connected();
    });
    await h.advance(120_000);
    assert.deepEqual(h.times(), [0, 15_000, 45_000]);
    assert.equal(h.failures, 0);
    assert.deepEqual(h.results, ['connected']);
    h.stop();
  },
  async cooldown() {
    const h = harness(() => ({status: 'rate_limited', retry_after_seconds: 20}));
    await h.advance(120_000);
    assert.deepEqual(h.times(), [0, 20_000, 50_000]);
    assert.equal(h.failures, 1);
    assert.equal(h.timerCount, 0);
    h.stop();
  },
  async foregroundCooldown() {
    const h = harness((signal, attempt) => attempt === 1 ? httpError(429, 35)() : connected());
    await h.advance(1_000);
    h.visit(false);
    await h.advance(1_000);
    h.visit(true);
    h.visit(true);
    await h.advance(32_999);
    assert.deepEqual(h.times(), [0]);
    await h.advance(1);
    assert.deepEqual(h.times(), [0, 35_000]);
    h.stop();
  },
  async longCooldown() {
    const h = harness((signal, attempt) => attempt === 1 ? httpError(429, 120)() : connected());
    await h.advance(1_000);
    assert.equal(h.timerCount, 0);
    h.visit(false);
    h.visit(true);
    await h.advance(119_000);
    assert.deepEqual(h.times(), [0]);
    h.visit(false);
    h.visit(true);
    await flush();
    assert.deepEqual(h.times(), [0, 120_000]);
    h.stop();
  },
  async auth() {
    for (const status of [401, 403]) {
      const h = harness(httpError(status));
      await h.advance(60_000);
      h.visit(false);
      h.visit(true);
      await h.advance(60_000);
      assert.deepEqual(h.times(), [0]);
      assert.equal(h.auth, 1);
      assert.equal(h.failures, 0);
      h.stop();
    }
  },
  async permanent() {
    const h = harness(httpError(404));
    await h.advance(120_000);
    assert.deepEqual(h.times(), [0]);
    assert.equal(h.auth, 0);
    assert.equal(h.failures, 1);
    h.stop();
  },
  async timeout() {
    const h = harness((signal, attempt) => attempt === 1 ? waitForAbort(signal) : connected());
    await h.advance(14_999);
    assert.equal(h.calls[0].signal.aborted, false);
    await h.advance(1);
    assert.equal(h.calls[0].signal.aborted, true);
    await h.advance(15_000);
    assert.deepEqual(h.times(), [0, 30_000]);
    assert.deepEqual(h.results, ['connected']);
    h.stop();
  },
  async disposal() {
    const h = harness(waitForAbort);
    h.visit(false);
    h.visit(true);
    await flush();
    assert.deepEqual(h.times(), [0]);
    h.stop();
    await h.advance(120_000);
    assert.equal(h.calls[0].signal.aborted, true);
    assert.deepEqual(h.times(), [0]);
    assert.equal(h.failures, 0);
    assert.equal(h.timerCount, 0);
    assert.equal(h.subscribed, false);
  },
};
scenarios[process.argv[3]]().catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.parametrize("scenario", [
    "healthy", "closed", "transient", "cooldown", "foregroundCooldown",
    "longCooldown", "auth", "permanent", "timeout", "disposal",
])
def test_market_auto_connect_is_bounded_and_classifies_failures(scenario):
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    node = shutil.which("node") or (str(bundled) if bundled.is_file() else None)
    typescript = ROOT / "frontend/node_modules/typescript"
    if not node or not typescript.is_dir():
        pytest.skip("Installed Node/frontend dependencies needed")
    result = subprocess.run(
        [node, "-e", SCRIPT, str(typescript), str(ROOT / "frontend/src/marketConnection.ts"), scenario],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
