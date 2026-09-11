window.BacktestView = (function () {
  let initialized = false;
  let schemas = [];
  let marketMeta = null;
  let priceChart = null, equityChart = null, drawdownChart = null;
  let lastTrades = [];
  let selectedRow = null;
  let savedConfigs = [];
  let loadedConfigId = null;
  // Set only while a saved config whose strategy_class has no flat param
  // schema (e.g. "custom_rule" — a JSON rule spec, not dataclass fields) is
  // loaded. Its own class is excluded from /api/strategies/classes, so
  // btStrategy's <select> has no matching <option> for it and the normal
  // schema-driven param form can't represent it — run/save/update fall
  // back to this raw {strategy_class, params} instead of the form.
  let loadedCustomRuleParams = null;

  const TIMEFRAMES = [
    { id: '1min', label: '1m' }, { id: '5min', label: '5m' }, { id: '15min', label: '15m' },
    { id: '30min', label: '30m' }, { id: '1h', label: '1h' }, { id: '4h', label: '4h' }, { id: '1d', label: '1D' },
  ];
  const SESSIONS = [
    { id: 'all', label: 'All' }, { id: 'london', label: 'London' },
    { id: 'ny', label: 'New York' }, { id: 'overlap', label: 'Overlap' },
  ];
  let currentTf = '15min';
  let currentSession = 'all';

  // Module-level (not inside init()) so both the schema-driven strategy
  // picker and the saved-config loader can reach it — the timeframe pill
  // auto-snaps to whatever the selected strategy declares as its own
  // working timeframe, same principle now applied to live/auto-trade
  // (Strategy.timeframe / spec.timeframe), while staying a normal pill the
  // user can still click to override afterward.
  function pickTf(id) {
    currentTf = id;
    renderPills(document.getElementById('btTfPills'), TIMEFRAMES, currentTf, pickTf);
  }

  function timeframeFor(strategyClass, params) {
    if (strategyClass === 'custom_rule') return ((params || {}).spec || {}).timeframe || '15min';
    return (params || {}).timeframe || '15min';
  }

  function pickTfForSchema(schema) {
    if (!schema) return;
    const tfParam = schema.params.find(p => p.name === 'timeframe');
    pickTf(tfParam ? tfParam.default : '15min');
  }

  const RANGES = [
    { id: '1m', label: '1M', months: 1 },
    { id: '3m', label: '3M', months: 3 },
    { id: '6m', label: '6M', months: 6 },
    { id: '1y', label: '1Y', months: 12 },
    { id: '2y', label: '2Y', months: 24 },
    { id: '5y', label: '5Y', months: 60 },
    { id: 'all', label: 'All', months: null },
    { id: 'custom', label: 'Custom', months: undefined },
  ];
  let currentRange = '1y';

  // Presets anchor to wall-clock TODAY, per explicit request — even though
  // a static/synthetic dataset usually doesn't reach today, so a preset can
  // legitimately request a window with little or no actual data in it yet.
  // The note below the fields says so plainly instead of failing silently.
  function applyRange(id) {
    currentRange = id;
    renderPills(document.getElementById('btRangePills'), RANGES, currentRange, applyRange);
    const fromEl = document.getElementById('btFrom'), toEl = document.getElementById('btTo');
    const datasetStart = marketMeta.start.slice(0, 10);
    const datasetEnd = marketMeta.end.slice(0, 10);
    const today = new Date().toISOString().slice(0, 10);

    if (id === 'all') {
      fromEl.value = datasetStart;
      toEl.value = datasetEnd;
    } else if (id !== 'custom') {
      const range = RANGES.find(r => r.id === id);
      const end = new Date(today + 'T00:00:00Z');
      const start = new Date(end);
      start.setUTCMonth(start.getUTCMonth() - range.months);
      fromEl.value = start.toISOString().slice(0, 10);
      toEl.value = today;
    }

    const noteEl = document.getElementById('btRangeNote');
    const toVal = toEl.value;
    if (id === 'custom') {
      noteEl.textContent = '';
    } else if (toVal > datasetEnd) {
      const fromVal = fromEl.value;
      noteEl.textContent = fromVal > datasetEnd
        ? `Data only goes through ${datasetEnd} — this whole range (${fromVal} to ${toVal}) is beyond it, so expect zero trades until current data is loaded.`
        : `Data only goes through ${datasetEnd} — this range extends to ${toVal}, so results only reflect trading up to ${datasetEnd}.`;
    } else {
      noteEl.textContent = '';
    }
  }

  const fmt = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtSign = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : (n > 0 ? '+' : '') + fmt(n, d);

  // ---------------- param form ----------------
  function renderParamForm(schema, customLabel) {
    const el = document.getElementById('btParams');
    el.innerHTML = '';
    if (!schema) {
      const note = document.createElement('div');
      note.className = 'bt-panel-note';
      note.textContent = `Custom rule strategy (${customLabel || 'custom_rule'}) — its rule spec has no flat parameter form here; it runs as saved. Edit the rules from the Strategies tab.`;
      el.appendChild(note);
      return;
    }
    schema.params.forEach(p => {
      const row = document.createElement('div');
      row.className = 'bt-param';
      const label = document.createElement('label');
      label.textContent = p.name.replace(/_/g, ' ');
      label.setAttribute('for', `param_${p.name}`);
      row.appendChild(label);
      let input;
      if (p.type === 'bool') {
        input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = !!p.default;
      } else if (p.type === 'tuple') {
        input = document.createElement('input');
        input.type = 'text';
        input.value = Array.isArray(p.default) ? p.default.join(',') : (p.default ?? '');
      } else if (p.type === 'float' || p.type === 'int') {
        input = document.createElement('input');
        input.type = 'number';
        input.step = p.type === 'float' ? 'any' : '1';
        if (p.default !== null && p.default !== undefined) input.value = p.default;
        input.placeholder = p.default === null ? 'default' : '';
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.value = p.default ?? '';
      }
      input.id = `param_${p.name}`;
      input.dataset.paramName = p.name;
      input.dataset.paramType = p.type;
      row.appendChild(input);
      el.appendChild(row);
    });
  }

  function applyParamsToForm(params) {
    document.querySelectorAll('#btParams [data-param-name]').forEach(input => {
      const name = input.dataset.paramName;
      if (!(name in params)) return;
      const val = params[name];
      const type = input.dataset.paramType;
      if (type === 'bool') { input.checked = !!val; return; }
      if (type === 'tuple') { input.value = Array.isArray(val) ? val.join(',') : (val ?? ''); return; }
      input.value = (val === null || val === undefined) ? '' : val;
    });
  }

  function collectParams() {
    const out = {};
    document.querySelectorAll('#btParams [data-param-name]').forEach(input => {
      const name = input.dataset.paramName;
      const type = input.dataset.paramType;
      if (type === 'bool') { out[name] = input.checked; return; }
      if (type === 'tuple') {
        const v = input.value.trim();
        out[name] = v ? v.split(',').map(s => s.trim()) : null;
        return;
      }
      if (type === 'float' || type === 'int') {
        const v = input.value.trim();
        out[name] = v === '' ? null : Number(v);
        return;
      }
      out[name] = input.value.trim() || null;
    });
    return out;
  }

  // ---------------- pills ----------------
  function renderPills(container, options, current, onPick) {
    container.innerHTML = '';
    options.forEach(opt => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pill-btn' + (opt.id === current ? ' active' : '');
      btn.textContent = opt.label;
      btn.addEventListener('click', () => onPick(opt.id));
      container.appendChild(btn);
    });
  }

  // ---------------- saved configs ----------------
  async function refreshSavedConfigs() {
    const res = await fetch('/api/strategies');
    savedConfigs = await res.json();
    const sel = document.getElementById('btSavedConfig');
    const currentVal = sel.value;
    sel.innerHTML = '<option value="">— none (ad-hoc) —</option>';
    savedConfigs.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = `${c.name} (${c.strategy_class})`;
      sel.appendChild(opt);
    });
    if (savedConfigs.some(c => String(c.id) === currentVal)) sel.value = currentVal;
  }

  function setLoadedConfig(cfg) {
    loadedConfigId = cfg ? cfg.id : null;
    const updateRow = document.getElementById('btUpdateRow');
    const nameInput = document.getElementById('btConfigName');
    if (cfg) {
      updateRow.hidden = false;
      document.getElementById('btUpdateName').textContent = cfg.name;
      nameInput.value = cfg.name;
    } else {
      updateRow.hidden = true;
      nameInput.value = '';
      loadedCustomRuleParams = null;
      const stratSel = document.getElementById('btStrategy');
      stratSel.disabled = false;
      renderParamForm(schemas.find(s => s.id === stratSel.value));
    }
  }

  // Active strategy id / params for run+save+update — the saved config's
  // own values while a builder-driven (schema-less) config is loaded,
  // otherwise whatever the ad-hoc form currently holds.
  function currentStrategyId() {
    return loadedCustomRuleParams ? loadedCustomRuleParams.strategy_class : document.getElementById('btStrategy').value;
  }

  function currentRunParams() {
    return loadedCustomRuleParams ? loadedCustomRuleParams.params : collectParams();
  }

  function wireSavedConfigControls() {
    const sel = document.getElementById('btSavedConfig');
    sel.addEventListener('change', () => {
      if (!sel.value) { setLoadedConfig(null); return; }
      const cfg = savedConfigs.find(c => String(c.id) === sel.value);
      if (!cfg) return;
      const schema = schemas.find(s => s.id === cfg.strategy_class);
      const stratSel = document.getElementById('btStrategy');
      if (schema) {
        loadedCustomRuleParams = null;
        stratSel.disabled = false;
        stratSel.value = cfg.strategy_class;
        renderParamForm(schema);
        applyParamsToForm(cfg.params);
      } else {
        // builder-driven strategy_class (e.g. custom_rule): no <option> in
        // btStrategy for it and no flat schema to build a form from — lock
        // the type select and run/save/update with the saved spec as-is.
        loadedCustomRuleParams = { strategy_class: cfg.strategy_class, params: cfg.params };
        stratSel.disabled = true;
        renderParamForm(null, cfg.strategy_class);
      }
      pickTf(timeframeFor(cfg.strategy_class, cfg.params));
      setLoadedConfig(cfg);
    });

    document.getElementById('btStrategy').addEventListener('change', () => {
      document.getElementById('btSavedConfig').value = '';
      setLoadedConfig(null);
    });

    document.getElementById('btSaveConfig').addEventListener('click', async () => {
      const name = document.getElementById('btConfigName').value.trim();
      if (!name) { alert('Name this configuration first.'); return; }
      const res = await fetch('/api/strategies', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, strategy_class: currentStrategyId(), params: currentRunParams() }),
      });
      const cfg = await res.json();
      if (cfg.detail) { alert(cfg.detail); return; }
      await refreshSavedConfigs();
      document.getElementById('btSavedConfig').value = cfg.id;
      setLoadedConfig(cfg);
    });

    document.getElementById('btUpdateConfig').addEventListener('click', async () => {
      if (!loadedConfigId) return;
      const name = document.getElementById('btConfigName').value.trim() || 'Untitled';
      const res = await fetch(`/api/strategies/${loadedConfigId}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, strategy_class: currentStrategyId(), params: currentRunParams() }),
      });
      const cfg = await res.json();
      if (cfg.detail) { alert(cfg.detail); return; }
      await refreshSavedConfigs();
      document.getElementById('btSavedConfig').value = cfg.id;
      setLoadedConfig(cfg);
    });

    document.getElementById('btDeleteConfig').addEventListener('click', async () => {
      if (!loadedConfigId) return;
      if (!confirm('Delete this saved configuration?')) return;
      await fetch(`/api/strategies/${loadedConfigId}`, { method: 'DELETE' });
      await refreshSavedConfigs();
      document.getElementById('btSavedConfig').value = '';
      setLoadedConfig(null);
    });
  }

  // ---------------- init ----------------
  async function init() {
    const [metaRes, stratRes] = await Promise.all([
      fetch('/api/market/meta'), fetch('/api/strategies/classes'),
    ]);
    marketMeta = await metaRes.json();
    schemas = await stratRes.json();

    const fromEl = document.getElementById('btFrom'), toEl = document.getElementById('btTo');
    const startDate = marketMeta.start.slice(0, 10);
    const today = new Date().toISOString().slice(0, 10);
    // max reaches today, not just the dataset's own end — presets anchor to
    // today by request, and the field should let you pick that manually too
    fromEl.min = startDate; fromEl.max = today;
    toEl.min = startDate; toEl.max = today;
    applyRange('1y'); // default: trailing 1 year from today
    [fromEl, toEl].forEach(el => el.addEventListener('input', () => {
      if (currentRange !== 'custom') applyRange('custom');
    }));

    const sel = document.getElementById('btStrategy');
    sel.innerHTML = '';
    schemas.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id; opt.textContent = s.label;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', () => {
      const schema = schemas.find(s => s.id === sel.value);
      renderParamForm(schema);
      pickTfForSchema(schema);
    });
    renderParamForm(schemas[0]);
    pickTfForSchema(schemas[0]); // also renders the timeframe pills

    function pickSession(id) { currentSession = id; renderPills(document.getElementById('btSessionPills'), SESSIONS, currentSession, pickSession); }
    renderPills(document.getElementById('btSessionPills'), SESSIONS, currentSession, pickSession);

    priceChart = createTerminalChart(document.getElementById('btPriceChart'));
    equityChart = createLineChart(document.getElementById('btEquityChart'), { area: true, color: '#3987e5' });
    drawdownChart = createLineChart(document.getElementById('btDrawdownChart'), { area: true, color: '#e4685f' });

    document.getElementById('btRun').addEventListener('click', runBacktest);

    await refreshSavedConfigs();
    wireSavedConfigControls();
  }

  // ---------------- run ----------------
  async function runBacktest() {
    const btn = document.getElementById('btRun');
    btn.disabled = true;
    btn.textContent = 'Running…';
    try {
      const body = {
        strategy: currentStrategyId(),
        params: currentRunParams(),
        timeframe: currentTf,
        session: currentSession,
        start: document.getElementById('btFrom').value || null,
        end: document.getElementById('btTo').value || null,
        initial_equity: Number(document.getElementById('btEquity').value) || 10000,
        risk_per_trade_pct: Number(document.getElementById('btRisk').value) || 0.5,
        base_spread: Number(document.getElementById('btSpread').value) || 0.30,
        slippage_per_side: Number(document.getElementById('btSlippage').value) || 0.10,
        commission_per_lot_roundtrip: Number(document.getElementById('btCommission').value) || 0,
        max_trades_per_day: Number(document.getElementById('btMaxTrades').value) || 3,
        optimistic_bars: document.getElementById('btOptimistic').checked,
        max_daily_loss_pct: document.getElementById('btMaxDailyLoss').value === '' ? null : Number(document.getElementById('btMaxDailyLoss').value),
        max_drawdown_pct: document.getElementById('btMaxDrawdown').value === '' ? null : Number(document.getElementById('btMaxDrawdown').value),
      };
      const res = await fetch('/api/backtest/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.error) { alert(data.error); return; }
      renderResults(data, body);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Run Backtest';
    }
  }

  // ---------------- render results ----------------
  function statTile(val, label, cls) {
    return `<div class="stat-tile"><div class="st-val ${cls || ''}">${val}</div><div class="st-lbl">${label}</div></div>`;
  }

  function renderStats(stats, trades) {
    const wins = trades.filter(t => t.net_pnl > 0).length;
    const losses = trades.length - wins;
    const avgPnl = trades.length ? trades.reduce((a, t) => a + t.net_pnl, 0) / trades.length : null;
    const el = document.getElementById('btStats');
    el.innerHTML = [
      statTile(stats.trades, 'Trades'),
      statTile(`${wins} / ${losses}`, 'Win / Loss'),
      statTile(fmt(stats.win_rate_pct, 1) + '%', 'Win Rate'),
      statTile(fmt(stats.profit_factor, 2), 'Profit Factor'),
      statTile(stats.rr_ratio === null ? '—' : fmt(stats.rr_ratio, 2), 'R : R'),
      statTile(fmtSign(stats.expectancy_R, 3) + 'R', 'Expectancy', stats.expectancy_R >= 0 ? 'pos' : 'neg'),
      statTile(fmtSign(avgPnl, 2), 'Avg P&L / trade', (avgPnl !== null && avgPnl >= 0) ? 'pos' : 'neg'),
      statTile(fmt(stats.max_drawdown_pct, 1) + '%', 'Max Drawdown', 'neg'),
      statTile(fmt(stats.sharpe_daily_ann, 2), 'Sharpe (ann.)'),
      statTile(fmtSign(stats.total_return_pct, 1) + '%', 'Total Return', stats.total_return_pct >= 0 ? 'pos' : 'neg'),
    ].join('');
  }

  function renderSplitTable(tableEl, rows, keyLabel) {
    if (!rows.length) { tableEl.innerHTML = '<tbody><tr><td style="padding:12px;color:var(--text-faint)">No trades.</td></tr></tbody>'; return; }
    const head = `<thead><tr><th>${keyLabel}</th><th class="num">Trades</th><th class="num">Win %</th><th class="num">Expectancy R</th><th class="num">Net P&amp;L</th></tr></thead>`;
    const body = rows.map(r => `<tr>
      <td>${r.key}</td><td class="num">${r.trades}</td><td class="num">${fmt(r.win_rate_pct, 1)}%</td>
      <td class="num ${r.expectancy_R >= 0 ? 'pos' : 'neg'}">${fmtSign(r.expectancy_R, 3)}</td>
      <td class="num ${r.net_pnl >= 0 ? 'pos' : 'neg'}">${fmtSign(r.net_pnl, 2)}</td>
    </tr>`).join('');
    tableEl.innerHTML = head + `<tbody>${body}</tbody>`;
  }

  function renderBestWorst(bw) {
    const el = document.getElementById('btBestWorst');
    if (!bw) { el.innerHTML = '<div class="bt-panel-note">Not enough trades to compute.</div>'; return; }
    const row = (cls, label, d) => `<div class="bw-row ${cls}">
      <div><div class="bw-period">${d.period}</div><div class="bw-detail">${label}</div></div>
      <div class="bw-detail num ${d.net_pnl >= 0 ? 'pos' : 'neg'}">${fmtSign(d.net_pnl, 2)} &middot; ${d.trades} trades</div>
    </div>`;
    el.innerHTML = row('best', 'Best month', bw.best) + row('worst', 'Worst month', bw.worst);
  }

  function buildMarkers(trades) {
    const markers = [];
    trades.forEach(t => {
      const entrySec = Math.floor(new Date(t.entry_time).getTime() / 1000);
      const exitSec = Math.floor(new Date(t.exit_time).getTime() / 1000);
      markers.push({
        time: entrySec, position: t.direction === 'long' ? 'belowBar' : 'aboveBar',
        color: t.direction === 'long' ? '#3ecb3e' : '#e4685f',
        shape: t.direction === 'long' ? 'arrowUp' : 'arrowDown',
      });
      markers.push({
        time: exitSec, position: t.direction === 'long' ? 'aboveBar' : 'belowBar',
        color: t.net_pnl >= 0 ? '#3ecb3e' : '#e4685f', shape: 'circle',
      });
    });
    markers.sort((a, b) => a.time - b.time);
    return markers;
  }

  function renderTradeTable(trades) {
    const countEl = document.getElementById('btTradeCount');
    countEl.textContent = `${trades.length} trades`;
    const tableEl = document.getElementById('btTradeTable');
    if (!trades.length) { tableEl.innerHTML = '<tbody><tr><td style="padding:12px;color:var(--text-faint)">No trades in this run.</td></tr></tbody>'; return; }
    const head = `<thead><tr>
      <th>Entry</th><th>Side</th><th class="num">Entry px</th><th class="num">SL</th><th class="num">TP</th>
      <th class="num">Lots</th><th class="num">R</th><th class="num">Net P&amp;L</th><th>Exit</th>
    </tr></thead>`;
    const body = trades.map((t, idx) => `<tr class="trade-row" data-idx="${idx}">
      <td>${t.entry_time.slice(0, 16).replace('T', ' ')}</td>
      <td><span class="pill-tag ${t.direction}">${t.direction === 'long' ? 'LONG' : 'SHORT'}</span></td>
      <td class="num">${fmt(t.entry_price)}</td>
      <td class="num">${fmt(t.initial_stop)}</td>
      <td class="num">${t.tp2 !== null ? fmt(t.tp2) : (t.tp1 !== null ? fmt(t.tp1) : '—')}</td>
      <td class="num">${fmt(t.lots, 2)}</td>
      <td class="num ${t.r_multiple >= 0 ? 'pos' : 'neg'}">${fmtSign(t.r_multiple)}</td>
      <td class="num ${t.net_pnl >= 0 ? 'pos' : 'neg'}">${fmtSign(t.net_pnl)}</td>
      <td>${t.exit_reason}</td>
    </tr>`).join('');
    tableEl.innerHTML = head + `<tbody>${body}</tbody>`;

    tableEl.querySelectorAll('.trade-row').forEach(row => {
      row.addEventListener('click', () => selectTrade(Number(row.dataset.idx), row));
    });
  }

  function selectTrade(idx, rowEl) {
    if (selectedRow) selectedRow.classList.remove('selected');
    rowEl.classList.add('selected');
    selectedRow = rowEl;
    const t = lastTrades[idx];
    priceChart.clearLevelLines();
    priceChart.addLevelLine(t.entry_price, '#97a1ad', 'Entry');
    priceChart.addLevelLine(t.initial_stop, '#e4685f', 'SL');
    if (t.tp1 !== null) priceChart.addLevelLine(t.tp1, '#3987e5', 'TP1');
    if (t.tp2 !== null) priceChart.addLevelLine(t.tp2, '#3ecb3e', 'TP2');
    const entrySec = Math.floor(new Date(t.entry_time).getTime() / 1000);
    const exitSec = Math.floor(new Date(t.exit_time).getTime() / 1000);
    const pad = Math.max(3600, Math.round((exitSec - entrySec) * 1.5));
    priceChart.setVisibleRange(entrySec - pad, exitSec + pad);
  }

  async function renderResults(data, req) {
    document.getElementById('btEmpty').hidden = true;
    document.getElementById('btResultsBody').hidden = false;

    document.getElementById('btWarning').textContent = data.warning || '';

    const riskEl = document.getElementById('btRiskNote');
    const riskMsgs = [];
    if (data.risk && data.risk.dd_halted_at) {
      riskMsgs.push(`Max-drawdown limit halted new entries on ${data.risk.dd_halted_at.slice(0, 10)} — everything after that is the position(s) already open playing out, no new risk taken.`);
    }
    if (data.risk && data.risk.daily_loss_breach_days > 0) {
      riskMsgs.push(`Daily-loss limit was hit on ${data.risk.daily_loss_breach_days} separate day(s), blocking new entries for the rest of each of those days.`);
    }
    riskEl.hidden = riskMsgs.length === 0;
    riskEl.innerHTML = riskMsgs.join('<br>');

    lastTrades = data.trades;
    selectedRow = null;

    renderStats(data.stats, data.trades);
    equityChart.setData(data.equity); equityChart.fitContent();
    drawdownChart.setData(data.drawdown); drawdownChart.fitContent();

    renderSplitTable(document.getElementById('btSessionTable'), data.splits.session || [], 'Session');
    renderSplitTable(document.getElementById('btExitTable'), data.splits.exit_reason || [], 'Exit reason');
    renderSplitTable(document.getElementById('btYearTable'), data.splits.year || [], 'Year');
    renderSplitTable(document.getElementById('btMonthTable'), data.splits.month || [], 'Month');
    renderBestWorst(data.best_worst);
    renderTradeTable(data.trades);

    // load the same window's OHLC bars for the trade-plot chart
    const qs = new URLSearchParams({ tf: req.timeframe, limit: '20000' });
    if (req.start) qs.set('start', req.start);
    if (req.end) qs.set('end', req.end);
    const barsRes = await fetch(`/api/market/bars?${qs}`);
    const barsData = await barsRes.json();
    priceChart.setData(barsData.bars);
    priceChart.setMarkers(buildMarkers(data.trades));
    priceChart.fitContent();
  }

  function onShow() {
    if (initialized) { refreshSavedConfigs(); return; }
    initialized = true;
    init();
  }

  return { onShow, refreshSavedConfigs };
})();
