(function () {
  const TIMEFRAMES = [
    { id: '1min', label: '1m' },
    { id: '5min', label: '5m' },
    { id: '15min', label: '15m' },
    { id: '30min', label: '30m' },
    { id: '1h', label: '1h' },
    { id: '4h', label: '4h' },
    { id: '1d', label: '1D' },
  ];
  let currentTf = '15min';

  // ---------------- modules (Live / Demo / Backtest) + sub-nav ----------------
  const navEl = document.getElementById('nav');
  const subNavEl = document.getElementById('subNav');
  const sectionTitleEl = document.getElementById('sectionTitle');

  // Live and Demo are the same five views, wired to a forced feed source;
  // Backtest has no feed at all — it's the offline research side of the app.
  const MODULES = {
    live: {
      label: 'Live', feedSource: 'biquote',
      tabs: [
        { id: 'charts', label: 'Chart' },
        { id: 'dashboard', label: 'Dashboard' },
        { id: 'paper', label: 'Trade' },
        { id: 'positions', label: 'Positions' },
        { id: 'history', label: 'History' },
      ],
    },
    demo: {
      label: 'Demo', feedSource: 'biquote',
      tabs: [
        { id: 'charts', label: 'Chart' },
        { id: 'dashboard', label: 'Dashboard' },
        { id: 'paper', label: 'Trade' },
        { id: 'positions', label: 'Positions' },
        { id: 'history', label: 'History' },
      ],
    },
    backtest: {
      label: 'Backtest', feedSource: null,
      tabs: [
        { id: 'backtest', label: 'Run Backtest' },
        { id: 'strategies', label: 'Strategies' },
      ],
    },
    ai: {
      label: 'AI Copilot', feedSource: null,
      tabs: [
        { id: 'ai', label: 'AI Quant Engine & Assistant' },
        { id: 'optimizer', label: 'Daily Optimizer' },
      ],
    },
  };


  let currentModule = 'demo';
  const lastTabByModule = { live: 'charts', demo: 'charts', backtest: 'backtest', ai: 'ai' };


  // Remember where the user was so a browser refresh lands back on the
  // same module/tab instead of always resetting to Demo → Chart.
  // Per-browser only (localStorage) — never sent anywhere, and a private
  // window or blocked storage just falls back to the old default quietly.
  const NAV_STORAGE_KEY = 'goldTerminalNav';
  function saveNavState() {
    try {
      localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify({ module: currentModule, tab: lastTabByModule[currentModule] }));
    } catch (err) { /* private browsing / storage blocked — just won't persist */ }
  }
  function loadNavState() {
    try {
      const raw = localStorage.getItem(NAV_STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const mod = parsed && MODULES[parsed.module];
      if (mod && mod.tabs.some(t => t.id === parsed.tab)) return parsed;
    } catch (err) { /* ignore corrupt/blocked storage */ }
    return null;
  }

  function showView(sectionId) {
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === `view-${sectionId}`));
    if (sectionId === 'backtest' && window.BacktestView) window.BacktestView.onShow();
    if (sectionId === 'optimizer' && window.OptimizerView) window.OptimizerView.onShow();
    if (sectionId === 'strategies' && window.StrategiesView) window.StrategiesView.onShow();
    if (['dashboard', 'paper', 'positions', 'history'].includes(sectionId) && window.PaperView) window.PaperView.onShow();
  }

  function renderSubNav() {
    const mod = MODULES[currentModule];
    subNavEl.innerHTML = '';
    mod.tabs.forEach(tab => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'subnav-btn' + (tab.id === lastTabByModule[currentModule] ? ' active' : '');
      btn.textContent = tab.label;
      btn.addEventListener('click', () => selectTab(tab.id));
      subNavEl.appendChild(btn);
    });
  }

  function selectTab(tabId) {
    lastTabByModule[currentModule] = tabId;
    renderSubNav();
    showView(tabId);
    const mod = MODULES[currentModule];
    const tab = mod.tabs.find(t => t.id === tabId);
    sectionTitleEl.textContent = `${mod.label} — ${tab ? tab.label : tabId}`;
    saveNavState();
  }

  async function selectModule(moduleId, opts) {
    opts = opts || {};
    currentModule = moduleId;
    navEl.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.module === moduleId));
    renderSubNav();
    selectTab(lastTabByModule[moduleId]);

    const mod = MODULES[moduleId];
    if (!mod.feedSource || opts.skipFeedSwitch) return;
    if (mod.feedSource === 'simulated') {
      await fetch('/api/paper/feed/start?source=simulated', { method: 'POST' });
    } else if (mod.feedSource === 'biquote') {
      const status = await (await fetch('/api/paper/feed/start?source=biquote', { method: 'POST' })).json();
      if (status.source !== 'biquote') {
        alert(`${mod.label}: biquote.io unreachable (${status.live_error || 'unknown error'}). Falling back to the simulated replay for now — click ${mod.label} again to retry.`);
      }
    }
  }

  navEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.nav-item');
    if (!btn) return;
    selectModule(btn.dataset.module);
  });

  // ---------------- settings overlay ----------------
  const settingsOverlayEl = document.getElementById('settingsOverlay');
  document.getElementById('settingsBtn').addEventListener('click', () => {
    settingsOverlayEl.hidden = false;
    if (window.SettingsView) window.SettingsView.onShow();
  });
  document.getElementById('settingsClose').addEventListener('click', () => { settingsOverlayEl.hidden = true; });
  document.getElementById('settingsBackdrop').addEventListener('click', () => { settingsOverlayEl.hidden = true; });

  // ---------------- logout (only shown once APP_USERNAME/APP_PASSWORD are set) ----------------
  const logoutBtn = document.getElementById('logoutBtn');
  fetch('/api/auth/status').then(r => r.json()).then(s => { logoutBtn.hidden = !s.enabled; }).catch(() => {});
  logoutBtn.addEventListener('click', async () => {
    await fetch('/api/auth/logout', { method: 'POST' });
    window.location.href = '/login';
  });

  // ---------------- data badge ----------------
  const dataBadgeEl = document.getElementById('dataBadge');
  async function loadMeta() {
    try {
      const res = await fetch('/api/market/meta');
      const meta = await res.json();
      dataBadgeEl.textContent = meta.source === 'real'
        ? `REAL DATA · ${meta.base_minutes}min base · ${meta.bars.toLocaleString()} bars`
        : `SYNTHETIC DATA · plumbing check, not an edge`;
      dataBadgeEl.className = 'data-badge ' + meta.source;
      return meta;
    } catch (err) {
      dataBadgeEl.textContent = 'data unavailable';
      return null;
    }
  }

  // ---------------- external spot-price cross-check ----------------
  // Independent real-world price from gold-api.com, shown purely as a
  // sanity check next to whatever feed the app is actually trading against
  // — never the tick source itself. The backend caches this for a few
  // minutes (see webapp/goldprice.py), so frequent polling here is cheap.
  const spotBadgeEl = document.getElementById('spotCheckBadge');
  function fmtAge(fetchedAtSeconds) {
    const mins = Math.max(0, Math.round(Date.now() / 1000 - fetchedAtSeconds) / 60);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${Math.round(mins)}m ago`;
    return `${Math.round(mins / 60)}h ago`;
  }
  async function refreshSpotCheck() {
    try {
      const res = await fetch('/api/market/spot-check');
      const data = await res.json();
      if (!res.ok || data.error) { spotBadgeEl.hidden = true; return; }
      spotBadgeEl.hidden = false;
      spotBadgeEl.textContent = `gold-api.com: $${fmtPrice(data.price)} · ${fmtAge(data.fetched_at)}`;
    } catch (err) {
      spotBadgeEl.hidden = true;
    }
  }

  // ---------------- timeframe pills ----------------
  const tfPillsEl = document.getElementById('tfPills');
  function renderTfPills() {
    tfPillsEl.innerHTML = '';
    TIMEFRAMES.forEach(tf => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pill-btn' + (tf.id === currentTf ? ' active' : '');
      btn.textContent = tf.label;
      btn.addEventListener('click', () => {
        if (tf.id === currentTf) return;
        currentTf = tf.id;
        renderTfPills();
        loadBars();
      });
      tfPillsEl.appendChild(btn);
    });
  }

  // ---------------- chart ----------------
  const chartWarningEl = document.getElementById('chartWarning');
  const ohlcReadoutEl = document.getElementById('ohlcReadout');
  let chart = null;
  let lastBars = [];

  function fmtPrice(v) {
    return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function renderReadout(bar) {
    if (!bar) { ohlcReadoutEl.innerHTML = ''; return; }
    const chg = bar.close - bar.open;
    const chgClass = chg >= 0 ? 'pos' : 'neg';
    ohlcReadoutEl.innerHTML = `
      <span>O <b>${fmtPrice(bar.open)}</b></span>
      <span>H <b>${fmtPrice(bar.high)}</b></span>
      <span>L <b>${fmtPrice(bar.low)}</b></span>
      <span>C <b class="${chgClass}">${fmtPrice(bar.close)}</b></span>
    `;
  }

  let loadBarsToken = 0; // bumped on every call; a stale in-flight call's
  // result is discarded instead of overwriting/racing a newer one's
  // chart.setData() — two overlapping setData calls on the same series
  // (e.g. rapid timeframe-pill clicks) is exactly the kind of misordering
  // that crashes lightweight-charts with an uncaught, unrecoverable
  // "Value is null" deep inside its own renderer (tradingview/lightweight-charts#568).
  let barsReadyForTf = null; // the tf whose history is actually in the chart
  // right now — liveTick() checks this before calling chart.update(), so a
  // live tick for a just-selected timeframe can never land ahead of
  // loadBars() finishing that timeframe's own chart.setData() (same class
  // of race/crash as above, between loadBars() and liveTick() this time).
  async function loadBars() {
    const token = ++loadBarsToken;
    const tf = currentTf;
    // The chart shows only real recent bars from biquote.io (/api/market/live-bars);
    // liveTick()/applyLiveBar() then keep the newest candle moving.
    chartWarningEl.textContent = '';

    const res = await fetch(`/api/market/live-bars?tf=${tf}`);
    const data = await res.json();
    if (token !== loadBarsToken) return;
    if (data.error) {
      chartWarningEl.textContent = data.error;
      lastBars = [];
      chart.setData([]);
      return;
    }
    chartWarningEl.textContent = data.warning || '';
    lastBars = data.bars;
    chart.setData(lastBars);
    chart.fitContent();
    if (lastBars.length) renderReadout(lastBars[lastBars.length - 1]);
    barsReadyForTf = tf;
  }

  function initChart() {
    chart = createTerminalChart(document.getElementById('priceChart'));
    chart.onCrosshairMove((param) => {
      if (!param || !param.time) { renderReadout(lastBars[lastBars.length - 1]); return; }
      const bar = lastBars.find(b => b.time === param.time);
      if (bar) renderReadout(bar);
    });
  }

  // ---------------- live trading on the chart ----------------
  const DIR_OPTS = [{ id: 'long', label: 'Long', dataDir: 1 }, { id: 'short', label: 'Short', dataDir: -1 }];
  let chartDir = 'long';
  let riskSettingsCache = null;
  let lastLiveBarTime = null;

  function renderChartPills(container, options, current, onPick) {
    container.innerHTML = '';
    options.forEach(opt => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pill-btn' + (opt.id === current ? ' active' : '');
      if (opt.dataDir !== undefined) btn.dataset.dir = opt.dataDir;
      btn.textContent = opt.label;
      btn.addEventListener('click', () => onPick(opt.id));
      container.appendChild(btn);
    });
  }
  function pickChartDir(id) {
    chartDir = id;
    renderChartPills(document.getElementById('chartDirPills'), DIR_OPTS, chartDir, pickChartDir);
    updateChartSizePreview();
  }

  // SL and TP get separate risk factors here, per Settings: SL suggests
  // from ATR × default_sl_atr_mult, TP auto-fills from the ACTUAL SL
  // distance × default_tp_rr — independent multipliers, not the same
  // number doing double duty. Auto-trade strategies never use either of
  // these; they always compute their own explicit SL/TP.
  async function suggestChartSl() {
    const errEl = document.getElementById('chartOrderError');
    if (!riskSettingsCache) return;
    const [priceRes, atrRes] = await Promise.all([
      fetch('/api/paper/price').catch(() => null),
      fetch('/api/paper/atr').catch(() => null),
    ]);
    if (!priceRes || !priceRes.ok || !atrRes || !atrRes.ok) {
      errEl.hidden = false; errEl.textContent = 'No live price/ATR available to suggest from yet.';
      return;
    }
    const price = await priceRes.json();
    const atrData = await atrRes.json();
    const dir = chartDir === 'long' ? 1 : -1;
    const entry = dir === 1 ? price.ask : price.bid;
    const dist = riskSettingsCache.default_sl_atr_mult * atrData.atr;
    const stopEl = document.getElementById('chartStop');
    stopEl.value = (entry - dir * dist).toFixed(2);
    stopEl.dispatchEvent(new Event('input'));
  }

  function autoFillChartTp() {
    const tpEl = document.getElementById('chartTP');
    if (tpEl.value.trim() !== '' || !riskSettingsCache) return; // never clobber a value the user typed themselves
    const stop = Number(document.getElementById('chartStop').value);
    if (!stop) return;
    fetch('/api/paper/price').then(r => r.ok ? r.json() : null).then(price => {
      if (!price || tpEl.value.trim() !== '') return;
      const dir = chartDir === 'long' ? 1 : -1;
      const entry = dir === 1 ? price.ask : price.bid;
      const risk = Math.abs(entry - stop);
      if (risk <= 0) return;
      tpEl.value = (entry + dir * riskSettingsCache.default_tp_rr * risk).toFixed(2);
    }).catch(() => {});
  }

  async function updateChartSizePreview() {
    const previewEl = document.getElementById('chartSizePreview');
    const stop = Number(document.getElementById('chartStop').value);
    if (!stop || !riskSettingsCache) { previewEl.innerHTML = ''; return; }
    const priceRes = await fetch('/api/paper/price').catch(() => null);
    if (!priceRes || !priceRes.ok) { previewEl.innerHTML = ''; return; }
    const price = await priceRes.json();
    const dir = chartDir === 'long' ? 1 : -1;
    const entry = dir === 1 ? price.ask : price.bid;
    const riskPct = Number(document.getElementById('chartRiskPct').value) || riskSettingsCache.risk_per_trade_pct;
    const acct = await (await fetch('/api/paper/account')).json();
    const res = await fetch('/api/risk/position-size', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        equity: acct.balance, risk_pct: riskPct, entry_price: entry, stop_price: stop,
        contract_size: riskSettingsCache.contract_size, min_lot: riskSettingsCache.min_lot,
        max_lot: riskSettingsCache.max_lot, max_risk_pct: riskSettingsCache.max_risk_per_trade_pct,
      }),
    });
    const d = await res.json();
    previewEl.className = 'calc-result' + (d.unsizeable ? ' warn' : '');
    previewEl.innerHTML = `
      <div class="cr-tile"><div class="cr-val">${d.lots}</div><div class="cr-lbl">Lots</div></div>
      <div class="cr-tile"><div class="cr-val">$${fmtPrice(d.risk_amount_usd)}</div><div class="cr-lbl">Risk $</div></div>
      <div class="cr-tile"><div class="cr-val">$${fmtPrice(d.risk_per_oz)}</div><div class="cr-lbl">Risk/oz</div></div>
    `;
  }

  async function submitChartOrder() {
    const errEl = document.getElementById('chartOrderError');
    errEl.hidden = true;
    const stop = Number(document.getElementById('chartStop').value);
    if (!stop) { errEl.hidden = false; errEl.textContent = 'Stop-loss is required.'; return; }
    const body = {
      kind: 'market', direction: chartDir === 'long' ? 1 : -1,
      stop_loss: stop,
      take_profit: document.getElementById('chartTP').value ? Number(document.getElementById('chartTP').value) : null,
      risk_pct: document.getElementById('chartRiskPct').value ? Number(document.getElementById('chartRiskPct').value) : null,
    };
    const res = await fetch('/api/paper/orders', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { errEl.hidden = false; errEl.textContent = data.detail || 'Order rejected.'; return; }
    document.getElementById('chartStop').value = '';
    document.getElementById('chartTP').value = '';
    updateChartSizePreview();
    refreshChartTrading();
  }

  let lastChartFeedKey = null; // `${source}|${running}|${hasError}` — reload real bars only when this actually changes

  async function refreshChartTrading() {
    const acct = await (await fetch('/api/paper/account')).json();
    const feedStatus = await (await fetch('/api/paper/feed/status')).json();

    // including live_error means a self-healed connection (error clears
    // while source/running stay the same) also triggers a fresh attempt,
    // instead of being stuck showing local data until the user re-clicks
    const feedKey = `${feedStatus.source}|${acct.feed_running}|${!!feedStatus.live_error}`;
    if (feedKey !== lastChartFeedKey) {
      lastChartFeedKey = feedKey;
      loadBars(); // feed just started/stopped, switched source (e.g. Live <-> Demo), or recovered from an error — refresh what the chart's history is drawn from
    }

    const feedStatusEl = document.getElementById('chartFeedStatus');
    feedStatusEl.textContent = acct.feed_running ? `feed: running (${feedStatus.source})` : 'feed: stopped';
    feedStatusEl.className = 'feed-status ' + (acct.feed_running ? 'on' : 'off');
    document.getElementById('chartFeedToggle').textContent = acct.feed_running ? 'Stop feed' : 'Start feed';

    const liveBadgeEl = document.getElementById('liveBadge');
    liveBadgeEl.hidden = !acct.feed_running;
    if (acct.feed_running) {
      const isLive = feedStatus.source === 'biquote';
      liveBadgeEl.classList.toggle('replay', !isLive);
      liveBadgeEl.title = isLive ? 'Ticking from biquote.io (real MT5 feed)' : 'Ticking from the replayed historical dataset, not a real live price';
      document.getElementById('liveBadgeLabel').textContent = isLive ? 'LIVE' : 'REPLAY';
    }

    const noteEl = document.getElementById('chartFeedNote');
    if (!acct.feed_running) {
      noteEl.textContent = 'Start the feed to see live movement and place demo trades.';
    } else if (feedStatus.source === 'biquote') {
      noteEl.textContent = 'Live biquote.io feed — candles updating live at any timeframe.';
    } else if (feedStatus.timeframe !== currentTf) {
      noteEl.textContent = `Simulated feed running on ${feedStatus.timeframe} — switch this chart to ${feedStatus.timeframe} to see candles move live (trading still works at any timeframe view).`;
    } else {
      noteEl.textContent = 'Simulated replay — candles updating live.';
    }

    const positions = await (await fetch('/api/paper/positions')).json();
    const boxEl = document.getElementById('chartPositionBox');
    chart.clearLevelLines();
    if (!positions.length) {
      boxEl.innerHTML = 'No open position.';
    } else {
      const p = positions[0];
      chart.addLevelLine(p.entry_price, '#97a1ad', 'Entry');
      chart.addLevelLine(p.stop_loss, '#e4685f', 'SL');
      if (p.take_profit !== null) chart.addLevelLine(p.take_profit, '#3ecb3e', 'TP');
      const pnl = p.floating_pnl || 0;
      const pnlClass = pnl >= 0 ? 'pos' : 'neg';
      boxEl.innerHTML = `
        <div class="cp-row"><span>${p.direction === 1 ? 'LONG' : 'SHORT'} ${p.lots} lots</span><span>@ ${fmtPrice(p.entry_price)}</span></div>
        <div class="cp-row"><span>Floating P&amp;L</span><span class="${pnlClass}">${pnl >= 0 ? '+' : ''}$${fmtPrice(pnl)}</span></div>
        <button type="button" class="dash-mini-action cp-close" data-close="${p.id}">Close position</button>
      `;
      boxEl.querySelector('[data-close]').addEventListener('click', async () => {
        await fetch(`/api/paper/positions/${p.id}/close`, { method: 'POST' });
        refreshChartTrading();
      });
    }
  }

  function applyLiveBar(bar) {
    chart.update(bar);
    chart.scrollToRealTime(); // series.update() doesn't pan the view on its own — without this the chart silently falls behind "now" as bars keep arriving off-screen
    const idx = lastBars.findIndex(b => b.time === bar.time);
    if (idx >= 0) lastBars[idx] = bar; else lastBars.push(bar);
    renderReadout(bar);
  }

  async function liveTick() {
    const status = await (await fetch('/api/paper/feed/status')).json();
    if (!status.running) return;

    if (status.source === 'biquote') {
      // biquote can serve any interval on demand, independent of the
      // feed's own fixed working timeframe (which auto-trade's signal
      // generation depends on and never changes) — so live movement works
      // at whatever timeframe the chart is currently showing, not just one.
      const requestedTf = currentTf;
      const res = await fetch(`/api/paper/feed/live?tf=${requestedTf}`).catch(() => null);
      // re-check: the user may have switched timeframe pills while that
      // fetch was in flight. Without this, a stale timeframe's bar can
      // land on the candle series right after it was just repainted with a
      // different timeframe's data — lightweight-charts doesn't tolerate
      // out-of-order/mismatched updates like that and corrupts the whole
      // series (an uncaught, unrecoverable "Value is null" from deep
      // inside its renderer, tradingview/lightweight-charts#568).
      if (currentTf !== requestedTf || barsReadyForTf !== requestedTf) return;
      if (res && res.ok) {
        const data = await res.json();
        if (currentTf !== requestedTf || barsReadyForTf !== requestedTf) return;
        const bar = data.forming || data.bar;
        if (bar) {
          lastLiveBarTime = bar.time;
          applyLiveBar(bar);
        }
      }
      return;
    }

    if (status.timeframe !== currentTf) return;
    const barRes = await fetch('/api/paper/feed/bar').catch(() => null);
    if (!barRes || !barRes.ok) return;
    if (status.timeframe !== currentTf) return;
    const bar = await barRes.json();
    if (bar.time === lastLiveBarTime) return;
    lastLiveBarTime = bar.time;
    applyLiveBar(bar);
  }

  // ---------------- running strategy's own indicators + trade markers ----
  const OVERLAY_COLORS = ['#3987e5', '#d95926', '#199e70', '#d8ac47', '#9b8cf2', '#e4685f', '#5fb8e4', '#c97fd6'];
  let oscChart = null;
  let lastAutoWallet = null;

  function buildLiveMarkers(trades) {
    const markers = [];
    trades.forEach(t => {
      const entrySec = Math.floor(new Date(t.entry_time).getTime() / 1000);
      const exitSec = Math.floor(new Date(t.exit_time).getTime() / 1000);
      markers.push({
        time: entrySec, position: t.direction === 1 ? 'belowBar' : 'aboveBar',
        color: t.direction === 1 ? '#3ecb3e' : '#e4685f',
        shape: t.direction === 1 ? 'arrowUp' : 'arrowDown',
      });
      markers.push({
        time: exitSec, position: t.direction === 1 ? 'aboveBar' : 'belowBar',
        color: t.net_pnl >= 0 ? '#3ecb3e' : '#e4685f', shape: 'circle',
      });
    });
    markers.sort((a, b) => a.time - b.time);
    return markers;
  }

  async function refreshChartIndicators() {
    try {
      const panelEl = document.getElementById('chartIndicatorsPanel');
      if (!panelEl || !chart || typeof chart.setOverlayLine !== 'function') {
        // stale cached JS/HTML (element or chart API missing) — surface it
        // once instead of failing silently every 2s forever
        if (!refreshChartIndicators._warned) {
          refreshChartIndicators._warned = true;
          console.error('refreshChartIndicators: missing #chartIndicatorsPanel or chart.setOverlayLine — likely a stale cached copy of index.html/chart.js. Hard-refresh (Ctrl+Shift+R).');
        }
        return;
      }

      // Several strategies can run at once now — the chart overlay only
      // ever shows ONE of them at a time, so this picks the first running
      // one (matching /autotrade/indicators' own fallback when no wallet
      // is specified). Use the Trade page to see every running strategy.
      const autoStatusList = await (await fetch('/api/paper/autotrade/status')).json();
      const runningList = autoStatusList.filter(s => s.enabled);
      const autoStatus = runningList[0];
      if (!autoStatus) {
        if (lastAutoWallet !== null) {
          chart.clearOverlays([]);
          chart.setMarkers([]);
          document.getElementById('chartStateBadges').innerHTML = '';
          lastAutoWallet = null;
        }
        panelEl.hidden = true;
        return;
      }

      const data = await (await fetch(`/api/paper/autotrade/indicators?count=300&wallet=${encodeURIComponent(autoStatus.wallet)}`)).json();
      const priceNames = Object.keys(data.price_series);
      chart.clearOverlays(priceNames);
      priceNames.forEach((name, i) => chart.setOverlayLine(name, data.price_series[name], OVERLAY_COLORS[i % OVERLAY_COLORS.length]));

      const stateNames = Object.keys(data.state || {});
      document.getElementById('chartStateBadges').innerHTML = stateNames.map(name => {
        const v = data.state[name];
        const cls = v > 0 ? 'bullish' : v < 0 ? 'bearish' : 'neutral';
        const label = v > 0 ? 'Bullish' : v < 0 ? 'Bearish' : 'Neutral';
        return `<span class="state-badge ${cls}"><span class="dot"></span>${name.replace(/_/g, ' ')}: ${label}</span>`;
      }).join('');

      const oscNames = Object.keys(data.oscillator_series);
      panelEl.hidden = false;
      document.getElementById('chartIndicatorsNote').textContent = runningList.length > 1
        ? `running: ${autoStatus.strategy} (+${runningList.length - 1} more running — see Trade page)`
        : `running: ${autoStatus.strategy}`;
      const oscChartEl = document.getElementById('chartOscChart');
      const legendEl = document.getElementById('chartOscLegend');
      if (oscNames.length) {
        oscChartEl.hidden = false;
        if (!oscChart) oscChart = createMultiLineChart(oscChartEl, { height: 140 });
        oscChart.clear();
        legendEl.innerHTML = '';
        oscNames.forEach((name, i) => {
          const color = OVERLAY_COLORS[i % OVERLAY_COLORS.length];
          oscChart.addSeries(data.oscillator_series[name].map(([t, v]) => [new Date(t * 1000).toISOString(), v]), color);
          legendEl.innerHTML += `<div class="legend-item"><span class="sw" style="background:${color}"></span>${name}</div>`;
        });
        oscChart.fitContent();
      } else {
        oscChartEl.hidden = true;
        legendEl.innerHTML = '';
      }

      // trade markers, only re-fetched when the running strategy's wallet changes
      if (autoStatus.wallet !== lastAutoWallet) {
        lastAutoWallet = autoStatus.wallet;
        const trades = await (await fetch(`/api/paper/history?tag=${encodeURIComponent(autoStatus.wallet)}&limit=200`)).json();
        chart.setMarkers(buildLiveMarkers(trades));
      }
    } catch (err) {
      console.error('refreshChartIndicators failed:', err);
    }
  }

  function startChartPolling() {
    setInterval(() => {
      if (!document.getElementById('view-charts').classList.contains('active')) return;
      liveTick();
      refreshChartTrading();
      refreshChartIndicators();
    }, 2000);
  }

  async function initLiveTrading() {
    riskSettingsCache = await (await fetch('/api/risk/settings')).json();
    document.getElementById('chartRiskPct').value = riskSettingsCache.risk_per_trade_pct;
    renderChartPills(document.getElementById('chartDirPills'), DIR_OPTS, chartDir, pickChartDir);

    document.getElementById('chartFeedToggle').addEventListener('click', async () => {
      const acct = await (await fetch('/api/paper/account')).json();
      await fetch(`/api/paper/feed/${acct.feed_running ? 'stop' : 'start'}`, { method: 'POST' });
      refreshChartTrading();
    });
    document.getElementById('chartSubmit').addEventListener('click', submitChartOrder);
    document.getElementById('chartSuggestSl').addEventListener('click', suggestChartSl);
    ['chartStop', 'chartRiskPct'].forEach(id => {
      document.getElementById(id).addEventListener('input', updateChartSizePreview);
    });
    document.getElementById('chartStop').addEventListener('input', autoFillChartTp);

    startChartPolling();
    refreshChartTrading();
  }

  // ---------------- init ----------------
  renderTfPills();
  initChart();
  loadMeta();
  loadBars();
  initLiveTrading();
  refreshSpotCheck();
  setInterval(refreshSpotCheck, 5 * 60 * 1000);
  // Restore whatever module/tab the user was last on (a refresh shouldn't
  // bounce back to Demo → Chart) — falls back to the original default when
  // nothing was saved yet, or storage is unavailable.
  const savedNav = loadNavState();
  if (savedNav) lastTabByModule[savedNav.module] = savedNav.tab;
  selectModule(savedNav ? savedNav.module : 'demo', { skipFeedSwitch: true }); // never force the feed on load
})();
