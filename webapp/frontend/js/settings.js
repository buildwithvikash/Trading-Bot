window.SettingsView = (function () {
  let initialized = false;

  const fmt = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });

  function fillForm(s) {
    document.getElementById('setRisk').value = s.risk_per_trade_pct;
    document.getElementById('setMaxRisk').value = s.max_risk_per_trade_pct;
    document.getElementById('setDailyLoss').value = s.max_daily_loss_pct ?? '';
    document.getElementById('setMaxDD').value = s.max_drawdown_pct ?? '';
    document.getElementById('setMaxOpen').value = s.max_open_positions;
    document.getElementById('setContract').value = s.contract_size;
    document.getElementById('setMinLot').value = s.min_lot;
    document.getElementById('setMaxLot').value = s.max_lot;
    document.getElementById('setSlAtrMult').value = s.default_sl_atr_mult;
    document.getElementById('setTpRr').value = s.default_tp_rr;
  }

  function collectForm() {
    const num = (id) => { const v = document.getElementById(id).value; return v === '' ? null : Number(v); };
    return {
      risk_per_trade_pct: num('setRisk') ?? 0.5,
      max_risk_per_trade_pct: num('setMaxRisk') ?? 2.0,
      max_daily_loss_pct: num('setDailyLoss'),
      max_drawdown_pct: num('setMaxDD'),
      max_open_positions: num('setMaxOpen') ?? 1,
      contract_size: num('setContract') ?? 100,
      min_lot: num('setMinLot') ?? 0.01,
      max_lot: num('setMaxLot') ?? 50,
      default_sl_atr_mult: num('setSlAtrMult') ?? 1.5,
      default_tp_rr: num('setTpRr') ?? 2.0,
    };
  }

  async function loadSettings() {
    const res = await fetch('/api/risk/settings');
    fillForm(await res.json());
  }

  async function saveSettings() {
    const res = await fetch('/api/risk/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(collectForm()),
    });
    fillForm(await res.json());
    const savedEl = document.getElementById('setSaved');
    savedEl.hidden = false;
    setTimeout(() => { savedEl.hidden = true; }, 2000);
  }

  async function runCalc() {
    const equity = Number(document.getElementById('calcEquity').value);
    const riskPct = Number(document.getElementById('calcRiskPct').value);
    const entry = Number(document.getElementById('calcEntry').value);
    const stop = Number(document.getElementById('calcStop').value);
    const resultEl = document.getElementById('calcResult');
    if (!entry || !stop || entry === stop) { resultEl.innerHTML = ''; return; }

    const settingsRes = await fetch('/api/risk/settings');
    const settings = await settingsRes.json();

    const res = await fetch('/api/risk/position-size', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        equity, risk_pct: riskPct, entry_price: entry, stop_price: stop,
        contract_size: settings.contract_size, min_lot: settings.min_lot, max_lot: settings.max_lot,
        max_risk_pct: settings.max_risk_per_trade_pct,
      }),
    });
    const d = await res.json();
    resultEl.className = 'calc-result' + (d.unsizeable ? ' warn' : '');
    resultEl.innerHTML = `
      <div class="cr-tile"><div class="cr-val">${fmt(d.lots, 2)}</div><div class="cr-lbl">Lots</div></div>
      <div class="cr-tile"><div class="cr-val">$${fmt(d.risk_amount_usd, 2)}</div><div class="cr-lbl">Risk amount</div></div>
      <div class="cr-tile"><div class="cr-val">$${fmt(d.risk_per_oz, 2)}</div><div class="cr-lbl">Risk / oz</div></div>
    ` + (d.unsizeable ? '<div class="bt-panel-note" style="grid-column:1/-1;color:var(--bad)">Position rounds to zero — risk % too small, stop too wide, or equity too low for the minimum lot.</div>' : '')
      + (d.capped_by_max_risk ? `<div class="bt-panel-note" style="grid-column:1/-1;color:var(--accent)">Capped at the max risk/trade limit (${d.risk_pct_used}%).</div>` : '');
  }

  async function refreshFeedStatus() {
    const status = await (await fetch('/api/paper/feed/status')).json();
    const el = document.getElementById('liveFeedStatus');
    el.textContent = `source: ${status.source}${status.running ? ' (running)' : ' (stopped)'}`;
    el.className = 'live-feed-status' + (status.source === 'biquote' ? ' on' : '');
    const errEl = document.getElementById('liveFeedError');
    if (status.live_error) { errEl.hidden = false; errEl.textContent = status.live_error; }
    else { errEl.hidden = true; }
  }

  async function connectLive() {
    await fetch('/api/paper/feed/start?source=biquote', { method: 'POST' });
    refreshFeedStatus();
  }

  async function useSimulated() {
    await fetch('/api/paper/feed/stop', { method: 'POST' });
    await fetch('/api/paper/feed/start?source=simulated', { method: 'POST' });
    refreshFeedStatus();
  }

  async function init() {
    await loadSettings();
    document.getElementById('setSave').addEventListener('click', saveSettings);
    ['calcEquity', 'calcRiskPct', 'calcEntry', 'calcStop'].forEach(id => {
      document.getElementById(id).addEventListener('input', runCalc);
    });
    runCalc();

    document.getElementById('liveConnect').addEventListener('click', connectLive);
    document.getElementById('liveUseSim').addEventListener('click', useSimulated);
    refreshFeedStatus();
  }

  function onShow() {
    if (initialized) { loadSettings(); refreshFeedStatus(); return; }
    initialized = true;
    init();
  }

  return { onShow };
})();
