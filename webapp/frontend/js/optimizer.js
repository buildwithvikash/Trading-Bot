(function () {
  'use strict';
  const runsEl = document.getElementById('optRuns');
  const reportEl = document.getElementById('optReport');
  const enabledEl = document.getElementById('optEnabled');
  const runBtn = document.getElementById('optRunBtn');
  let selected = null;

  const esc = (t) => String(t == null ? '' : t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmtTime = (iso) => new Date(iso).toLocaleString();
  const ACTION_LABEL = { none: 'No change', deferred: 'Deferred', adjust_rr: 'Take-profit RR changed', adjust_risk: 'Risk per trade changed', pause: 'Strategy paused' };
  const PARAM_LABEL = { rr: 'take-profit RR', 'spec.take_profit.rr': 'take-profit RR', risk_pct: 'risk per trade %' };

  async function loadStatus() {
    const s = await (await fetch('/api/optimizer/status')).json();
    enabledEl.checked = s.enabled;
  }

  async function loadRuns() {
    const runs = await (await fetch('/api/optimizer/reports')).json();
    if (!runs.length) {
      runsEl.innerHTML = '<p class="opt-empty">No reviews yet. The first one runs automatically after the daily break, or click "Run review now".</p>';
      return;
    }
    runsEl.innerHTML = runs.map(r => `
      <button type="button" class="opt-run${r.id === selected ? ' active' : ''}" data-id="${r.id}">
        <strong>${esc(fmtTime(r.run_at))}</strong>
        <span>${esc(r.trigger)} &middot; ${r.changes_applied} change(s)</span>
      </button>`).join('');
    runsEl.querySelectorAll('.opt-run').forEach(b => b.addEventListener('click', () => showReport(Number(b.dataset.id))));
    if (selected === null) showReport(runs[0].id);
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
    reportEl.innerHTML = `<h3 class="opt-report-title">${esc(fmtTime(r.run_at))} &mdash; ${esc(r.summary)}</h3>${cards || '<p class="opt-empty">No saved strategies were running.</p>'}`;
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

  window.OptimizerView = { onShow() { loadStatus(); loadRuns(); } };
})();
