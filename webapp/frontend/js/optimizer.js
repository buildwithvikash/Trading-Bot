(function () {
  'use strict';
  const runsEl = document.getElementById('optRuns');
  const reportEl = document.getElementById('optReport');
  const enabledEl = document.getElementById('optEnabled');
  const runBtn = document.getElementById('optRunBtn');
  const rangeEl = document.getElementById('optRange');
  const customEl = document.getElementById('optCustom');
  const fromEl = document.getElementById('optFilterFrom');
  const toEl = document.getElementById('optFilterTo');
  const triggerEl = document.getElementById('optFilterTrigger');
  const changedEl = document.getElementById('optFilterChanged');
  const clearBtn = document.getElementById('optFilterClear');
  const countEl = document.getElementById('optRunCount');

  let selected = null;
  let current = null;          // the report currently displayed, with its actions
  let range = 'all';           // '1' | '7' | '30' | 'all' | 'custom'
  // Per-report filters survive switching between runs, so you can step
  // through days looking at e.g. only paused strategies.
  const view = { kind: 'all', search: '', sort: 'default' };
  // Guards against a slow earlier response overwriting a newer one.
  let runsSeq = 0, reportSeq = 0;

  const esc = (t) => String(t == null ? '' : t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const DT_OPTS = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false };
  const fmtZone = (iso, timeZone) => new Date(iso).toLocaleString('en-GB', { ...DT_OPTS, timeZone }).replace(',', '');
  const fmtDay = (iso) => new Date(iso).toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric', timeZone: 'Asia/Kolkata' });
  const fmtTime = (iso) => new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' });

  const ACTION_LABEL = { none: 'No change', deferred: 'Deferred', adjust_rr: 'Take-profit RR changed', adjust_risk: 'Risk per trade changed', pause: 'Strategy paused' };
  const PARAM_LABEL = { rr: 'take-profit RR', 'spec.take_profit.rr': 'take-profit RR', risk_pct: 'risk per trade %' };
  const KINDS = [
    { id: 'all', label: 'All' },
    { id: 'changed', label: 'Changed' },
    { id: 'pause', label: 'Paused' },
    { id: 'deferred', label: 'Deferred' },
    { id: 'none', label: 'No change' },
  ];
  const kindOf = (a) => (a.action === 'adjust_rr' || a.action === 'adjust_risk') ? 'changed'
    : (a.action === 'pause' || a.action === 'deferred') ? a.action : 'none';

  const fmtPct = (v) => v == null ? '—' : `${(v * 100).toFixed(0)}%`;
  const fmtPf = (v) => v == null ? '—' : v >= 99 ? '∞' : v.toFixed(2);
  const fmtR = (v) => v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`;
  const fmtUsd = (v) => v == null ? '—' : `${v < 0 ? '-' : '+'}$${Math.abs(v).toFixed(2)}`;
  const cap = (t) => { const v = String(t || ''); return v.charAt(0).toUpperCase() + v.slice(1); };
  const signCls = (v) => v == null || v === 0 ? '' : v > 0 ? 'pos' : 'neg';

  // <input type=datetime-local> has no timezone of its own — its value is
  // treated as IST wall-clock time (matching the "From/To (IST)" labels),
  // then converted to a UTC ISO string to compare against run_at, which is
  // always stored in UTC.
  function istLocalToUtcIso(value) {
    if (!value) return null;
    const [datePart, timePart] = value.split('T');
    const [y, mo, d] = datePart.split('-').map(Number);
    const [h, mi] = timePart.split(':').map(Number);
    const utcMs = Date.UTC(y, mo - 1, d, h, mi) - (5.5 * 60 * 60 * 1000);
    return new Date(utcMs).toISOString().replace('Z', '+00:00');
  }

  function buildQuery() {
    const params = new URLSearchParams();
    let start = null, end = null;
    if (range === 'custom') {
      start = istLocalToUtcIso(fromEl.value);
      end = istLocalToUtcIso(toEl.value);
    } else if (range !== 'all') {
      start = new Date(Date.now() - Number(range) * 86400000).toISOString().replace('Z', '+00:00');
    }
    if (start) params.set('start', start);
    if (end) params.set('end', end);
    if (triggerEl.value) params.set('trigger', triggerEl.value);
    if (changedEl.checked) params.set('changed_only', 'true');
    return params.toString();
  }

  async function loadStatus() {
    const s = await (await fetch('/api/optimizer/status')).json();
    enabledEl.checked = s.enabled;
  }

  async function loadRuns() {
    const seq = ++runsSeq;
    const qs = buildQuery();
    const runs = await (await fetch(`/api/optimizer/reports${qs ? '?' + qs : ''}`)).json();
    if (seq !== runsSeq) return;
    countEl.textContent = `${runs.length} review${runs.length === 1 ? '' : 's'}`;
    if (!runs.length) {
      runsEl.innerHTML = '<p class="opt-empty">No reviews match these filters. The first one runs automatically after the daily break, or click "Run review now".</p>';
      reportEl.innerHTML = '<p class="opt-empty">Select a report.</p>';
      selected = null;
      current = null;
      return;
    }
    runsEl.innerHTML = runs.map(r => `
      <button type="button" class="opt-run${r.id === selected ? ' active' : ''}" data-id="${r.id}">
        <span class="opt-run-top">
          <strong>${esc(fmtDay(r.run_at))}</strong>
          <span class="opt-run-time num">${esc(fmtTime(r.run_at))} IST</span>
        </span>
        <span class="opt-run-utc num">${fmtZone(r.run_at, 'UTC')} UTC</span>
        <span class="opt-run-tags">
          <span class="opt-pill">${esc(cap(r.trigger))}</span>
          <span class="opt-pill">${r.strategies_reviewed} reviewed</span>
          <span class="opt-pill${r.changes_applied > 0 ? ' accent' : ''}">${r.changes_applied} change${r.changes_applied === 1 ? '' : 's'}</span>
        </span>
      </button>`).join('');
    runsEl.querySelectorAll('.opt-run').forEach(b => b.addEventListener('click', () => showReport(Number(b.dataset.id))));
    if (!runs.some(r => r.id === selected)) showReport(runs[0].id);
  }

  async function showReport(id) {
    const seq = ++reportSeq;
    selected = id;
    runsEl.querySelectorAll('.opt-run').forEach(b => b.classList.toggle('active', Number(b.dataset.id) === id));
    const r = await (await fetch(`/api/optimizer/reports/${id}`)).json();
    if (seq !== reportSeq) return;
    current = r;
    renderReport();
  }

  function renderReport() {
    const r = current;
    const acts = r.actions;
    const counts = { all: acts.length, changed: 0, pause: 0, deferred: 0, none: 0 };
    acts.forEach(a => { counts[kindOf(a)]++; });
    // A sticky outcome filter that this run has nothing for would just show
    // an empty list behind a disabled chip — fall back to All instead.
    if (counts[view.kind] === 0) view.kind = 'all';
    const totalTrades = acts.reduce((s, a) => s + (a.trades || 0), 0);
    const totalPnl = acts.reduce((s, a) => s + (a.net_pnl || 0), 0);

    const kpi = (label, value, cls = '') => `<div class="opt-kpi"><span class="opt-kpi-label">${label}</span><span class="opt-kpi-value num ${cls}">${value}</span></div>`;

    reportEl.innerHTML = `
      <header class="opt-rep-head">
        <div>
          <h3 class="opt-rep-when">${esc(fmtDay(r.run_at))} <span class="num">${esc(fmtTime(r.run_at))} IST</span></h3>
          <div class="opt-rep-utc num">${fmtZone(r.run_at, 'UTC')} UTC &middot; ${fmtZone(r.run_at, 'Asia/Kolkata')} IST</div>
        </div>
        <span class="opt-pill">${esc(cap(r.trigger))} run</span>
      </header>
      <p class="opt-report-summary">${esc(r.summary)}</p>
      <div class="opt-kpis">
        ${kpi('Reviewed', r.strategies_reviewed)}
        ${kpi('Changes', r.changes_applied, r.changes_applied > 0 ? 'accent' : '')}
        ${kpi('Paused', counts.pause, counts.pause > 0 ? 'neg' : '')}
        ${kpi('Deferred', counts.deferred)}
        ${kpi('Trades evaluated', totalTrades)}
        ${kpi('Net P&amp;L (window)', fmtUsd(acts.length ? totalPnl : null), signCls(totalPnl))}
      </div>
      ${acts.length ? `
      <div class="opt-rep-tools">
        <div class="opt-chips" role="group" aria-label="Filter by outcome">
          ${KINDS.map(k => `<button type="button" class="opt-chip${view.kind === k.id ? ' active' : ''}" data-kind="${k.id}" aria-pressed="${view.kind === k.id}"${counts[k.id] === 0 && k.id !== 'all' ? ' disabled' : ''}>${k.label}<span class="num">${counts[k.id]}</span></button>`).join('')}
        </div>
        <div class="opt-tools-right">
          <input type="search" class="opt-search" id="optSearch" placeholder="Search strategy…" value="${esc(view.search)}" aria-label="Search strategy">
          <select class="bt-select" id="optSort" aria-label="Sort strategies">
            <option value="default">Sort: review order</option>
            <option value="pnl">Net P&amp;L</option>
            <option value="win_rate">Win rate</option>
            <option value="expectancy_r">Avg R</option>
            <option value="trades">Trades</option>
          </select>
        </div>
      </div>
      <div id="optCards"></div>` : '<p class="opt-empty">No saved strategies were running.</p>'}`;

    if (!acts.length) return;
    reportEl.querySelector('#optSort').value = view.sort;
    reportEl.querySelectorAll('.opt-chip').forEach(b => b.addEventListener('click', () => {
      view.kind = b.dataset.kind;
      reportEl.querySelectorAll('.opt-chip').forEach(c => {
        const on = c === b;
        c.classList.toggle('active', on);
        c.setAttribute('aria-pressed', on);
      });
      renderCards();
    }));
    reportEl.querySelector('#optSearch').addEventListener('input', (e) => { view.search = e.target.value; renderCards(); });
    reportEl.querySelector('#optSort').addEventListener('change', (e) => { view.sort = e.target.value; renderCards(); });
    renderCards();
  }

  function renderCards() {
    const box = reportEl.querySelector('#optCards');
    if (!box || !current) return;
    const q = view.search.trim().toLowerCase();
    let list = current.actions.filter(a =>
      (view.kind === 'all' || kindOf(a) === view.kind) &&
      (!q || `${a.wallet_key} ${a.strategy_id}`.toLowerCase().includes(q)));
    if (view.sort !== 'default') {
      const key = view.sort === 'pnl' ? 'net_pnl' : view.sort;
      // Missing values (too few trades) always sink to the bottom.
      list = [...list].sort((x, y) => (y[key] ?? -Infinity) - (x[key] ?? -Infinity));
    }
    if (!list.length) {
      box.innerHTML = '<p class="opt-empty">No strategies match these filters.</p>';
      return;
    }
    box.innerHTML = list.map(a => {
      const change = a.applied && a.action !== 'pause' && a.old_value !== null
        ? `<div class="opt-change"><span>${esc(PARAM_LABEL[a.param] || a.param)}</span><b class="num">${esc(a.old_value)}</b><span aria-hidden="true">&rarr;</span><b class="num">${esc(a.new_value)}</b></div>` : '';
      const sub = a.strategy_id && a.strategy_id !== a.wallet_key ? `<span class="opt-strat-id">${esc(a.strategy_id)}</span>` : '';
      return `
      <article class="opt-card ${esc(kindOf(a))}">
        <div class="opt-card-head">
          <div class="opt-strat-wrap"><span class="opt-strat">${esc(a.wallet_key)}</span>${sub}</div>
          <span class="opt-badge ${esc(a.action)}">${esc(ACTION_LABEL[a.action] || a.action)}</span>
        </div>
        <dl class="opt-metrics">
          <div><dt>Trades</dt><dd class="num">${a.trades ?? '—'}</dd></div>
          <div><dt>Win rate</dt><dd class="num">${fmtPct(a.win_rate)}</dd></div>
          <div><dt>Profit factor</dt><dd class="num">${fmtPf(a.profit_factor)}</dd></div>
          <div><dt>Avg R</dt><dd class="num ${signCls(a.expectancy_r)}">${fmtR(a.expectancy_r)}</dd></div>
          <div><dt>Net P&amp;L</dt><dd class="num ${signCls(a.net_pnl)}">${fmtUsd(a.trades ? a.net_pnl : null)}</dd></div>
        </dl>
        ${change}
        <div class="opt-notes">
          <div class="opt-row"><label>What it found</label><p>${esc(a.finding)}</p></div>
          <div class="opt-row"><label>Why</label><p>${esc(a.reason)}</p></div>
          <div class="opt-row"><label>What to expect</label><p>${esc(a.expected_effect)}</p></div>
        </div>
      </article>`;
    }).join('');
  }

  function setRange(value) {
    range = value;
    rangeEl.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.range === value));
    customEl.hidden = value !== 'custom';
  }

  enabledEl.addEventListener('change', async () => {
    await fetch('/api/optimizer/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: enabledEl.checked }) });
  });
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    try {
      const res = await (await fetch('/api/optimizer/run', { method: 'POST' })).json();
      selected = res.run_id;
      await loadRuns();
      await showReport(res.run_id);
    } finally { runBtn.disabled = false; }
  });

  rangeEl.querySelectorAll('button').forEach(b => b.addEventListener('click', () => { setRange(b.dataset.range); loadRuns(); }));
  // 'change' only fires once the datetime-local value is complete, so a
  // half-typed date never triggers a query.
  fromEl.addEventListener('change', loadRuns);
  toEl.addEventListener('change', loadRuns);
  triggerEl.addEventListener('change', loadRuns);
  changedEl.addEventListener('change', loadRuns);
  clearBtn.addEventListener('click', () => {
    fromEl.value = ''; toEl.value = ''; triggerEl.value = ''; changedEl.checked = false;
    setRange('all');
    view.kind = 'all'; view.search = ''; view.sort = 'default';
    if (current) renderReport();
    loadRuns();
  });

  window.OptimizerView = { onShow() { loadStatus(); loadRuns(); } };
})();
