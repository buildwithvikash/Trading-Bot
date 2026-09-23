(function () {
  'use strict';
  const runsEl = document.getElementById('optRuns');
  const reportEl = document.getElementById('optReport');
  const enabledEl = document.getElementById('optEnabled');
  const runBtn = document.getElementById('optRunBtn');
  const fromEl = document.getElementById('optFilterFrom');
  const toEl = document.getElementById('optFilterTo');
  const applyBtn = document.getElementById('optFilterApply');
  const clearBtn = document.getElementById('optFilterClear');
  let selected = null;

  const esc = (t) => String(t == null ? '' : t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const DT_OPTS = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false };
  const fmtZone = (iso, timeZone) => new Date(iso).toLocaleString('en-GB', { ...DT_OPTS, timeZone }).replace(',', '');
  // "actual" = UTC, the timestamp as stored/generated on the server, alongside the IST wall-clock equivalent.
  const fmtBoth = (iso) => `${fmtZone(iso, 'UTC')} UTC &nbsp;/&nbsp; ${fmtZone(iso, 'Asia/Kolkata')} IST`;

  const ACTION_LABEL = { none: 'No change', deferred: 'Deferred', adjust_rr: 'Take-profit RR changed', adjust_risk: 'Risk per trade changed', pause: 'Strategy paused' };
  const PARAM_LABEL = { rr: 'take-profit RR', 'spec.take_profit.rr': 'take-profit RR', risk_pct: 'risk per trade %' };

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
    const start = istLocalToUtcIso(fromEl.value);
    const end = istLocalToUtcIso(toEl.value);
    if (start) params.set('start', start);
    if (end) params.set('end', end);
    return params.toString();
  }

  async function loadStatus() {
    const s = await (await fetch('/api/optimizer/status')).json();
    enabledEl.checked = s.enabled;
  }

  async function loadRuns() {
    const qs = buildQuery();
    const runs = await (await fetch(`/api/optimizer/reports${qs ? '?' + qs : ''}`)).json();
    if (!runs.length) {
      runsEl.innerHTML = '<p class="opt-empty">No reviews in this range. The first one runs automatically after the daily break, or click "Run review now".</p>';
      reportEl.innerHTML = '<p class="opt-empty">Select a report.</p>';
      selected = null;
      return;
    }
    runsEl.innerHTML = runs.map(r => `
      <button type="button" class="opt-run${r.id === selected ? ' active' : ''}" data-id="${r.id}">
        <strong>${fmtZone(r.run_at, 'Asia/Kolkata')} IST</strong>
        <span class="opt-run-utc">${fmtZone(r.run_at, 'UTC')} UTC</span>
        <span>${esc(r.trigger)} &middot; ${r.changes_applied} change(s)</span>
      </button>`).join('');
    runsEl.querySelectorAll('.opt-run').forEach(b => b.addEventListener('click', () => showReport(Number(b.dataset.id))));
    if (!runs.some(r => r.id === selected)) showReport(runs[0].id);
  }

  async function showReport(id) {
    selected = id;
    runsEl.querySelectorAll('.opt-run').forEach(b => b.classList.toggle('active', Number(b.dataset.id) === id));
    const r = await (await fetch(`/api/optimizer/reports/${id}`)).json();
    const cards = r.actions.map(a => {
      const change = a.applied && a.action !== 'pause' && a.old_value !== null
        ? `<div class="opt-change">${esc(PARAM_LABEL[a.param] || a.param)}: <b>${esc(a.old_value)}</b> &rarr; <b>${esc(a.new_value)}</b></div>` : '';
      return `
      <div class="opt-card ${esc(a.action)}">
        <div class="opt-card-head"><span class="opt-strat">${esc(a.wallet_key)}</span><span class="opt-badge ${esc(a.action)}">${esc(ACTION_LABEL[a.action] || a.action)}</span></div>
        <div class="opt-row"><label>What it found</label><p>${esc(a.finding)}</p></div>
        ${change}
        <div class="opt-row"><label>Why</label><p>${esc(a.reason)}</p></div>
        <div class="opt-row"><label>What to expect</label><p>${esc(a.expected_effect)}</p></div>
      </div>`;
    }).join('');
    reportEl.innerHTML = `<h3 class="opt-report-title">${fmtBoth(r.run_at)}</h3><p class="opt-report-summary">${esc(r.summary)}</p>${cards || '<p class="opt-empty">No saved strategies were running.</p>'}`;
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
  applyBtn.addEventListener('click', () => loadRuns());
  clearBtn.addEventListener('click', () => { fromEl.value = ''; toEl.value = ''; loadRuns(); });

  window.OptimizerView = { onShow() { loadStatus(); loadRuns(); } };
})();
