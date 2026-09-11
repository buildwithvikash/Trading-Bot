window.PaperView = (function () {
  let initialized = false;
  let pollTimer = null;
  let riskSettings = null;

  const fmt = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtSign = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : (n > 0 ? '+' : '') + fmt(n, d);
  const EXIT_LABEL = { stop: 'Stop loss', tp1: 'Take profit', breakeven: 'Breakeven', trail_stop: 'Trailing stop', time_exit: 'Time exit', stop_ambiguous: 'Stop (ambiguous)', manual_close: 'Manual close' };
  const SESSION_LABEL = { london: 'London', ny: 'New York', overlap: 'Overlap', asian: 'Asian' };

  function statTile(val, label, cls) {
    return `<div class="stat-tile"><div class="st-val ${cls || ''}">${val}</div><div class="st-lbl">${label}</div></div>`;
  }
  function renderPills(container, options, current, onPick) {
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

  // ==================== ORDER TICKET ====================
  const KIND_OPTS = [{ id: 'market', label: 'Market' }, { id: 'stop', label: 'Stop' }, { id: 'limit', label: 'Limit' }];
  const DIR_OPTS = [{ id: 'long', label: 'Long', dataDir: 1 }, { id: 'short', label: 'Short', dataDir: -1 }];
  let orderKind = 'market';
  let orderDir = 'long';

  function pickKind(id) {
    orderKind = id;
    renderPills(document.getElementById('paperKindPills'), KIND_OPTS, orderKind, pickKind);
    document.getElementById('paperTriggerField').hidden = (orderKind === 'market');
    updateSizePreview();
  }
  function pickDir(id) {
    orderDir = id;
    renderPills(document.getElementById('paperDirPills'), DIR_OPTS, orderDir, pickDir);
    updateSizePreview();
  }

  async function updateSizePreview() {
    const previewEl = document.getElementById('paperSizePreview');
    const stop = Number(document.getElementById('paperStop').value);
    const trigger = Number(document.getElementById('paperTrigger').value);
    if (!stop || !riskSettings) { previewEl.innerHTML = ''; return; }
    const priceRes = await fetch('/api/paper/price').catch(() => null);
    if (!priceRes || !priceRes.ok) { previewEl.innerHTML = '<div class="bt-panel-note">Feed is not running — start it on the Dashboard.</div>'; return; }
    const price = await priceRes.json();
    const dir = orderDir === 'long' ? 1 : -1;
    let entry = orderKind === 'market' ? (dir === 1 ? price.ask : price.bid) : trigger;
    if (!entry) { previewEl.innerHTML = ''; return; }
    const riskPct = Number(document.getElementById('paperRiskPct').value) || riskSettings.risk_per_trade_pct;
    const res = await fetch('/api/risk/position-size', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        equity: (await (await fetch('/api/paper/account')).json()).balance,
        risk_pct: riskPct, entry_price: entry, stop_price: stop,
        contract_size: riskSettings.contract_size, min_lot: riskSettings.min_lot,
        max_lot: riskSettings.max_lot, max_risk_pct: riskSettings.max_risk_per_trade_pct,
      }),
    });
    const d = await res.json();
    const lotsOverride = document.getElementById('paperLots').value;
    previewEl.className = 'calc-result' + (d.unsizeable && !lotsOverride ? ' warn' : '');
    previewEl.innerHTML = `
      <div class="cr-tile"><div class="cr-val">${lotsOverride || fmt(d.lots, 2)}</div><div class="cr-lbl">Lots</div></div>
      <div class="cr-tile"><div class="cr-val">$${fmt(d.risk_amount_usd, 2)}</div><div class="cr-lbl">Risk amount</div></div>
      <div class="cr-tile"><div class="cr-val">$${fmt(d.risk_per_oz, 2)}</div><div class="cr-lbl">Risk / oz</div></div>
    `;
  }

  async function submitOrder() {
    const errEl = document.getElementById('paperOrderError');
    errEl.hidden = true;
    const body = {
      kind: orderKind,
      direction: orderDir === 'long' ? 1 : -1,
      trigger_price: orderKind === 'market' ? null : Number(document.getElementById('paperTrigger').value) || null,
      stop_loss: Number(document.getElementById('paperStop').value),
      take_profit: document.getElementById('paperTP').value ? Number(document.getElementById('paperTP').value) : null,
      risk_pct: document.getElementById('paperRiskPct').value ? Number(document.getElementById('paperRiskPct').value) : null,
      lots: document.getElementById('paperLots').value ? Number(document.getElementById('paperLots').value) : null,
    };
    if (!body.stop_loss) { errEl.hidden = false; errEl.textContent = 'Stop-loss is required.'; return; }
    const res = await fetch('/api/paper/orders', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { errEl.hidden = false; errEl.textContent = data.detail || 'Order rejected.'; return; }
    document.getElementById('paperStop').value = '';
    document.getElementById('paperTP').value = '';
    document.getElementById('paperLots').value = '';
    document.getElementById('paperTrigger').value = '';
    updateSizePreview();
    refreshAll();
  }

  // ==================== RENDER TABLES ====================
  // strategy-placed orders/positions carry their strategy name as `tag`;
  // manual order-ticket trades have no tag — this is the only place the two
  // are told apart, since both share the same paper account
  function sourceTag(tag) {
    return tag
      ? `<span class="pill-tag strategy-tag">${tag}</span>`
      : `<span class="pill-tag manual-tag">manual</span>`;
  }

  function renderPositionsTable(tableEl, positions, { withClose }) {
    if (!positions.length) { tableEl.innerHTML = '<tbody><tr><td style="padding:12px;color:var(--text-faint)">No open positions.</td></tr></tbody>'; return; }
    const head = `<thead><tr>
      <th>Entry</th><th>Side</th><th>Strategy</th><th class="num">Lots</th><th class="num">Entry px</th><th class="num">Current</th>
      <th class="num">SL</th><th class="num">TP</th><th class="num">R</th><th class="num">Floating P&amp;L</th>${withClose ? '<th></th>' : ''}
    </tr></thead>`;
    const body = positions.map(p => `<tr>
      <td>${p.entry_time.slice(0, 16).replace('T', ' ')}</td>
      <td><span class="pill-tag ${p.direction === 1 ? 'long' : 'short'}">${p.direction === 1 ? 'LONG' : 'SHORT'}</span></td>
      <td>${sourceTag(p.tag)}</td>
      <td class="num">${fmt(p.lots, 2)}</td>
      <td class="num">${fmt(p.entry_price)}</td>
      <td class="num">${p.current_price !== null ? fmt(p.current_price) : '—'}</td>
      <td class="num">${fmt(p.stop_loss)}</td>
      <td class="num">${p.take_profit !== null ? fmt(p.take_profit) : '—'}</td>
      <td class="num ${(p.r_multiple || 0) >= 0 ? 'pos' : 'neg'}">${p.r_multiple !== null ? fmtSign(p.r_multiple) : '—'}</td>
      <td class="num ${(p.floating_pnl || 0) >= 0 ? 'pos' : 'neg'}">${p.floating_pnl !== null ? fmtSign(p.floating_pnl) : '—'}</td>
      ${withClose ? `<td><button type="button" class="dash-mini-action" data-close="${p.id}">Close</button></td>` : ''}
    </tr>`).join('');
    tableEl.innerHTML = head + `<tbody>${body}</tbody>`;
    if (withClose) {
      tableEl.querySelectorAll('[data-close]').forEach(btn => {
        btn.addEventListener('click', async () => {
          await fetch(`/api/paper/positions/${btn.dataset.close}/close`, { method: 'POST' });
          refreshAll();
        });
      });
    }
  }

  function renderOrdersTable(tableEl, orders) {
    if (!orders.length) { tableEl.innerHTML = '<tbody><tr><td style="padding:12px;color:var(--text-faint)">No pending orders.</td></tr></tbody>'; return; }
    const head = `<thead><tr><th>Kind</th><th>Side</th><th class="num">Trigger</th><th class="num">SL</th><th class="num">TP</th><th class="num">Lots</th><th></th></tr></thead>`;
    const body = orders.map(o => `<tr>
      <td>${o.kind}</td>
      <td><span class="pill-tag ${o.direction === 1 ? 'long' : 'short'}">${o.direction === 1 ? 'LONG' : 'SHORT'}</span></td>
      <td class="num">${o.trigger_price !== null ? fmt(o.trigger_price) : '—'}</td>
      <td class="num">${fmt(o.stop_loss)}</td>
      <td class="num">${o.take_profit !== null ? fmt(o.take_profit) : '—'}</td>
      <td class="num">${fmt(o.lots, 2)}</td>
      <td><button type="button" class="dash-mini-action" data-cancel="${o.id}">Cancel</button></td>
    </tr>`).join('');
    tableEl.innerHTML = head + `<tbody>${body}</tbody>`;
    tableEl.querySelectorAll('[data-cancel]').forEach(btn => {
      btn.addEventListener('click', async () => {
        await fetch(`/api/paper/orders/${btn.dataset.cancel}`, { method: 'DELETE' });
        refreshAll();
      });
    });
  }

  function renderHistoryTable(tableEl, rows) {
    if (!rows.length) { tableEl.innerHTML = '<tbody><tr><td style="padding:12px;color:var(--text-faint)">No closed trades yet.</td></tr></tbody>'; return; }
    const head = `<thead><tr>
      <th>Exit</th><th>Side</th><th>Strategy</th><th>Session</th><th class="num">Lots</th><th class="num">Entry</th><th class="num">Exit</th>
      <th class="num">R</th><th class="num">Net P&amp;L</th><th>Reason</th>
    </tr></thead>`;
    const body = rows.map(t => `<tr>
      <td>${t.exit_time.slice(0, 16).replace('T', ' ')}</td>
      <td><span class="pill-tag ${t.direction === 1 ? 'long' : 'short'}">${t.direction === 1 ? 'LONG' : 'SHORT'}</span></td>
      <td>${sourceTag(t.tag)}</td>
      <td>${SESSION_LABEL[t.session] || t.session}</td>
      <td class="num">${fmt(t.lots, 2)}</td>
      <td class="num">${fmt(t.entry_price)}</td>
      <td class="num">${fmt(t.exit_price)}</td>
      <td class="num ${t.r_multiple >= 0 ? 'pos' : 'neg'}">${fmtSign(t.r_multiple)}</td>
      <td class="num ${t.net_pnl >= 0 ? 'pos' : 'neg'}">${fmtSign(t.net_pnl)}</td>
      <td>${EXIT_LABEL[t.exit_reason] || t.exit_reason}</td>
    </tr>`).join('');
    tableEl.innerHTML = head + `<tbody>${body}</tbody>`;
  }

  // ==================== DASHBOARD ====================
  const SPEEDS = [{ id: 'fast', label: 'Fast', v: 0.5 }, { id: 'normal', label: 'Normal', v: 2 }, { id: 'slow', label: 'Slow', v: 5 }];
  let currentSpeed = 'normal';

  function pickSpeed(id) {
    currentSpeed = id;
    renderPills(document.getElementById('dashSpeedPills'), SPEEDS, currentSpeed, pickSpeed);
    const speed = SPEEDS.find(s => s.id === id);
    fetch('/api/paper/feed/speed', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ seconds_per_bar: speed.v }) });
  }

  async function renderDashboard() {
    const [acctRes, statsRes] = await Promise.all([fetch('/api/paper/account'), fetch('/api/paper/stats')]);
    const acct = await acctRes.json();
    const stats = await statsRes.json();

    const feedStatus = await (await fetch('/api/paper/feed/status')).json();
    const feedStatusEl = document.getElementById('dashFeedStatus');
    feedStatusEl.textContent = acct.feed_running ? `feed: running (${feedStatus.source})` : 'feed: stopped';
    feedStatusEl.className = 'feed-status ' + (acct.feed_running ? 'on' : 'off');
    document.getElementById('dashFeedToggle').textContent = acct.feed_running ? 'Stop feed' : 'Start feed';

    const price = acct.feed_running ? await (await fetch('/api/paper/price')).json() : null;
    const label = feedStatus.source === 'biquote' ? 'live time' : 'simulated time';
    document.getElementById('dashSimTime').textContent = price ? `${label}: ${price.time.slice(0, 16).replace('T', ' ')} UTC` : '';

    document.getElementById('dashStats').innerHTML = [
      statTile('$' + fmt(acct.balance), 'Balance'),
      statTile('$' + fmt(acct.equity), 'Equity'),
      statTile('$' + fmt(acct.free_margin), 'Free Margin'),
      statTile(fmtSign(acct.floating_pnl), 'Open P&amp;L', acct.floating_pnl >= 0 ? 'pos' : 'neg'),
      statTile(fmtSign(stats.today_pnl), "Today's P&amp;L", stats.today_pnl >= 0 ? 'pos' : 'neg'),
      statTile(stats.win_rate_pct !== null ? fmt(stats.win_rate_pct, 1) + '%' : '—', 'Win Rate'),
      statTile(stats.trades, 'Closed Trades'),
      statTile(stats.profit_factor !== null ? fmt(stats.profit_factor, 2) : '—', 'Profit Factor'),
      statTile(fmt(stats.max_drawdown_pct, 1) + '%', 'Max Drawdown', 'neg'),
      statTile(acct.open_positions, 'Open Positions'),
    ].join('');

    document.getElementById('dashPosCount').textContent = `${acct.open_positions} open`;
  }

  // ==================== STRATEGY WALLETS ====================
  async function refreshWallets() {
    let wallets;
    try {
      wallets = await (await fetch('/api/paper/wallets')).json();
    } catch (err) {
      console.error('refreshWallets: failed to load', err);
      return;
    }
    const tableEl = document.getElementById('walletsTable');
    if (!wallets.length) {
      tableEl.innerHTML = '<tbody><tr><td style="padding:12px;color:var(--text-faint)">No wallets yet.</td></tr></tbody>';
      return;
    }
    const head = `<thead><tr>
      <th>Wallet</th><th class="num">Balance</th><th class="num">Equity</th><th class="num">Open P&amp;L</th>
      <th class="num">Trades</th><th class="num">Win Rate</th><th class="num">Profit Factor</th>
      <th class="num">Max DD</th><th class="num">Today's P&amp;L</th><th></th>
    </tr></thead>`;
    const body = wallets.map(w => `<tr>
      <td>${sourceTag(w.wallet_key === 'manual' ? null : w.wallet_key)}</td>
      <td class="num">$${fmt(w.balance)}</td>
      <td class="num">$${fmt(w.equity)}</td>
      <td class="num ${(w.floating_pnl || 0) >= 0 ? 'pos' : 'neg'}">${fmtSign(w.floating_pnl)}</td>
      <td class="num">${w.trades}</td>
      <td class="num">${w.win_rate_pct !== null ? fmt(w.win_rate_pct, 1) + '%' : '—'}</td>
      <td class="num">${w.profit_factor !== null ? fmt(w.profit_factor, 2) : '—'}</td>
      <td class="num neg">${fmt(w.max_drawdown_pct, 1)}%</td>
      <td class="num ${(w.today_pnl || 0) >= 0 ? 'pos' : 'neg'}">${fmtSign(w.today_pnl)}</td>
      <td><button type="button" class="dash-mini-action" data-reset-wallet="${w.wallet_key}">Reset</button></td>
    </tr>`).join('');
    tableEl.innerHTML = head + `<tbody>${body}</tbody>`;
    tableEl.querySelectorAll('[data-reset-wallet]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const key = btn.dataset.resetWallet;
        if (!confirm(`Reset the "${key}" wallet? This clears its balance, pending orders, open positions and trade history back to a starting balance. Other wallets are untouched.`)) return;
        const input = prompt('Starting balance for this wallet:', '10000');
        if (input === null) return;
        const startingBalance = Number(input);
        if (!isFinite(startingBalance) || startingBalance <= 0) { alert('Enter a valid positive number.'); return; }
        await fetch(`/api/paper/wallets/${encodeURIComponent(key)}/reset?starting_balance=${startingBalance}`, { method: 'POST' });
        refreshWallets();
        refreshAll();
      });
    });
  }

  // ==================== AUTO-TRADE ====================
  // runs a SAVED strategy from the Strategy Library — built-in or built with
  // the no-code rule builder in the Strategies tab. The strategy is already
  // fully configured when saved, so this panel only picks which one to run.
  let autoConfigs = [];
  let autoStatusList = []; // every strategy started this session (webapp.autotrade.AutoTraderManager), running or stopped
  const selectedAutoConfigs = new Set(); // ids checked in the multi-select list below, start-batch this session only
  const openSetupWallets = new Set(); // wallets whose "Setup" detail panel is expanded — the running list is rebuilt every 2s poll, so this is what keeps a panel open across that instead of it silently re-hiding

  function summarizeAutoConfig(cfg) {
    if (cfg.strategy_class === 'custom_rule') {
      const spec = cfg.params.spec || {};
      const nInd = (spec.indicators || []).length;
      const nLong = spec.entry_long ? spec.entry_long.conditions.length : 0;
      const nShort = spec.entry_short ? spec.entry_short.conditions.length : 0;
      return `Custom rule strategy — ${nInd} indicator${nInd === 1 ? '' : 's'}, ${nLong} long / ${nShort} short condition${(nLong + nShort) === 1 ? '' : 's'}, ${cfg.params.timeframe || (spec.timeframe || '15min')}.`;
    }
    const keys = Object.keys(cfg.params || {}).filter(k => k !== 'timeframe');
    const tf = cfg.params.timeframe || '15min';
    const base = keys.length ? keys.slice(0, 3).map(k => `${k}=${cfg.params[k]}`).join(', ') : 'default parameters';
    return `${cfg.strategy_class} — ${base}, ${tf}.`;
  }

  function renderAutoSelectedCount() {
    const n = selectedAutoConfigs.size;
    document.getElementById('autoSelectedCount').textContent = n ? `${n} selected` : '';
  }

  function renderAutoStrategyList() {
    const listEl = document.getElementById('autoStrategyList');
    if (!autoConfigs.length) {
      listEl.innerHTML = '<div class="bt-panel-note" style="padding:12px">Save a strategy in the Strategies tab first — built-in or the no-code rule builder both work here.</div>';
      return;
    }
    listEl.innerHTML = autoConfigs.map(cfg => `
      <label class="auto-strategy-item">
        <input type="checkbox" data-id="${cfg.id}" ${selectedAutoConfigs.has(cfg.id) ? 'checked' : ''}>
        <div>
          <div class="asi-name">${cfg.name}</div>
          <div class="asi-summary">${summarizeAutoConfig(cfg)}</div>
        </div>
      </label>`).join('');
    listEl.querySelectorAll('input[type="checkbox"]').forEach(box => {
      box.addEventListener('change', (e) => {
        const id = Number(e.target.dataset.id);
        if (e.target.checked) selectedAutoConfigs.add(id); else selectedAutoConfigs.delete(id);
        renderAutoSelectedCount();
      });
    });
    renderAutoSelectedCount();
  }

  async function refreshAutoConfigs() {
    try {
      autoConfigs = await (await fetch('/api/strategies')).json();
    } catch (err) {
      console.error('refreshAutoConfigs: failed to load saved strategies', err);
      return;
    }
    const validIds = new Set(autoConfigs.map(c => c.id));
    Array.from(selectedAutoConfigs).forEach(id => { if (!validIds.has(id)) selectedAutoConfigs.delete(id); });
    renderAutoStrategyList();
  }

  // Several strategies can run at once now (webapp.autotrade.AutoTraderManager)
  // — the form below only ever ADDS a new one; each running strategy gets
  // its own row with its own Stop button in the "Running strategies" list.
  async function refreshAutoStatus() {
    try {
      autoStatusList = await (await fetch('/api/paper/autotrade/status')).json();
    } catch (err) {
      console.error('refreshAutoStatus: failed', err);
      return;
    }
    const listEl = document.getElementById('autoRunningList');
    const countEl = document.getElementById('autoRunningCount');
    const running = autoStatusList.filter(s => s.enabled);

    countEl.textContent = running.length ? `${running.length} active` : '';
    if (!running.length) {
      listEl.className = 'bt-panel-note';
      listEl.textContent = 'Nothing running yet.';
      return;
    }
    listEl.className = '';
    // prune wallets that stopped running since the last poll so this set
    // doesn't grow forever
    Array.from(openSetupWallets).forEach(w => { if (!running.some(s => s.wallet === w)) openSetupWallets.delete(w); });
    listEl.innerHTML = running.map(s => {
      const cfg = autoConfigs.find(c => c.strategy_class === s.strategy && JSON.stringify(c.params) === JSON.stringify(s.params));
      const modeTxt = s.mode === 'biquote' ? 'live biquote.io' : 'historical replay';
      const riskTxt = s.risk_pct !== null && s.risk_pct !== undefined ? `${s.risk_pct}% risk` : 'account default risk';
      const blockTxt = s.last_block_reason
        ? `<div class="risk-warning">Blocked: ${s.last_block_reason}</div>` : '';
      const isOpen = openSetupWallets.has(s.wallet);
      return `
        <div class="auto-running-row">
          <div class="auto-running-info">
            <b>${cfg ? cfg.name : s.strategy}</b>
            <div class="bt-panel-note">wallet '${s.wallet}' · ${s.timeframe} · ${modeTxt} · ${riskTxt}</div>
            ${blockTxt}
          </div>
          <button type="button" class="dash-mini-action" data-details="${s.wallet}">${isOpen ? 'Hide setup' : 'Setup'}</button>
          <button type="button" class="dash-mini-action" data-stop="${s.wallet}">Stop</button>
        </div>
        <div class="auto-setup-detail" id="setup-${cssEscape(s.wallet)}" ${isOpen ? '' : 'hidden'}></div>`;
    }).join('');
    listEl.querySelectorAll('[data-stop]').forEach(btn => {
      btn.addEventListener('click', async () => {
        await fetch(`/api/paper/autotrade/stop?wallet=${encodeURIComponent(btn.dataset.stop)}`, { method: 'POST' });
        refreshAutoStatus();
        refreshActivity();
        refreshAll();
      });
    });
    listEl.querySelectorAll('[data-details]').forEach(btn => {
      btn.addEventListener('click', () => toggleSetupDetail(btn.dataset.details));
    });
    // re-populate any panels that are supposed to stay open across this
    // rebuild — without this, the very next 2s poll would silently wipe
    // whatever the click handler just showed.
    openSetupWallets.forEach(wallet => loadSetupDetail(wallet));
  }

  // one strategy's own state-machine snapshot — what stage it's in, how
  // many bars ago each anchor point in that stage happened, and its
  // funnel counters (if it tracks any). Stays open across the panel's own
  // 2s poll cycle (openSetupWallets) and gets refreshed each time, so an
  // open panel live-updates instead of going stale.
  function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, '_');
  }

  function toggleSetupDetail(wallet) {
    const el = document.getElementById(`setup-${cssEscape(wallet)}`);
    if (!el) return;
    if (openSetupWallets.has(wallet)) {
      openSetupWallets.delete(wallet);
      el.hidden = true;
      refreshAutoStatus(); // relabel the button back to "Setup"
      return;
    }
    openSetupWallets.add(wallet);
    el.hidden = false;
    loadSetupDetail(wallet);
    refreshAutoStatus(); // relabel the button to "Hide setup"
  }

  async function loadSetupDetail(wallet) {
    const el = document.getElementById(`setup-${cssEscape(wallet)}`);
    if (!el) return;
    if (!el.innerHTML) el.textContent = 'Loading…';
    try {
      const data = await (await fetch(`/api/paper/autotrade/setup?wallet=${encodeURIComponent(wallet)}`)).json();
      el.innerHTML = renderSetupDetail(data);
    } catch (err) {
      el.textContent = 'Could not load setup detail.';
    }
  }

  function fmtKey(k) {
    return k.replace(/_/g, ' ');
  }

  function renderSetupDetail(data) {
    const parts = [];
    parts.push(`<div class="bt-panel-note">last bar ${data.last_bar_time ? new Date(data.last_bar_time).toLocaleString() : '—'} · ${data.bars_seen} bars seen</div>`);

    if (!data.setup) {
      parts.push('<div class="bt-panel-note">No persistent multi-bar setup — this strategy decides fresh from the current bar\'s indicators each time, so there\'s nothing "in flight" to wait on between bars.</div>');
    } else {
      const rows = Object.entries(data.setup)
        .filter(([k]) => !k.endsWith('_bar')) // raw bar index — the *_bars_ago companion is what's readable
        .map(([k, v]) => `<div class="setup-kv"><span>${fmtKey(k)}</span><b>${v}</b></div>`)
        .join('');
      parts.push(`<div class="setup-grid">${rows}</div>`);
    }

    if (data.funnel) {
      const flat = [];
      for (const [k, v] of Object.entries(data.funnel)) {
        if (v && typeof v === 'object') {
          const inner = Object.entries(v).map(([rk, rv]) => `${fmtKey(rk)}: ${rv}`).join(', ');
          flat.push(`<div class="setup-kv"><span>${fmtKey(k)}</span><b>${inner || '—'}</b></div>`);
        } else {
          flat.push(`<div class="setup-kv"><span>${fmtKey(k)}</span><b>${v}</b></div>`);
        }
      }
      parts.push(`<div class="bt-panel-note" style="margin-top:6px">Funnel (cumulative this run)</div><div class="setup-grid">${flat.join('')}</div>`);
    }
    return parts.join('');
  }

  async function startAutoTrade() {
    const errEl = document.getElementById('autoStartError');
    errEl.hidden = true;
    const picked = autoConfigs.filter(c => selectedAutoConfigs.has(c.id));
    if (!picked.length) {
      errEl.hidden = false;
      errEl.textContent = 'Check at least one saved strategy above first.';
      return;
    }

    // nice-to-have side effect: also light up the shared chart feed if it
    // isn't running yet. Auto-trade itself no longer depends on this at
    // all — each strategy polls biquote directly and falls back to the
    // historical replay on its own if biquote isn't reachable.
    fetch('/api/paper/feed/status').then(r => r.json()).then(s => {
      if (!s.running) fetch('/api/paper/feed/start?source=biquote', { method: 'POST' });
    }).catch(() => {});

    const riskPct = document.getElementById('autoRiskPct').value ? Number(document.getElementById('autoRiskPct').value) : null;
    const failures = [];
    for (const cfg of picked) {
      const res = await fetch('/api/paper/autotrade/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy: cfg.strategy_class, params: cfg.params, risk_pct: riskPct }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        failures.push(`${cfg.name}: ${d.detail || 'could not start'}`);
      } else {
        selectedAutoConfigs.delete(cfg.id); // started successfully — uncheck it so the list reflects what's left to start
      }
    }
    renderAutoStrategyList();
    if (failures.length) {
      errEl.hidden = false;
      errEl.textContent = `Started ${picked.length - failures.length} of ${picked.length}. ` + failures.join(' · ');
    }
    refreshAutoStatus();
    refreshActivity();
    refreshAll();
  }

  async function initAutoTrade() {
    document.getElementById('autoToggle').addEventListener('click', startAutoTrade);
    await refreshAutoConfigs();
    await refreshAutoStatus().catch(err => console.error('initAutoTrade: failed to load status', err));
  }

  // ==================== LIVE ACTIVITY FEED ====================
  const ACTIVITY_KIND_LABEL = { info: 'info', signal: 'signal', fill: 'fill', exit: 'exit', blocked: 'blocked' };

  async function refreshActivity() {
    let events;
    try {
      events = await (await fetch('/api/paper/autotrade/activity?limit=100')).json();
    } catch (err) {
      console.error('refreshActivity: failed to load', err);
      return;
    }
    const feedEl = document.getElementById('activityFeed');
    document.getElementById('activityCount').textContent = events.length ? `${events.length} events` : '';
    if (!events.length) {
      feedEl.innerHTML = '<div class="builder-empty-note">Nothing yet — start auto-trade to see it here.</div>';
      return;
    }
    feedEl.innerHTML = events.map(e => {
      const kindClass = e.kind === 'exit' && e.pnl !== undefined && e.pnl < 0 ? 'exit neg' : e.kind;
      return `<div class="activity-row">
        <span class="activity-time">${e.time.slice(11, 19)}</span>
        <span class="activity-kind ${kindClass}">${ACTIVITY_KIND_LABEL[e.kind] || e.kind}</span>
        <span class="activity-message">${e.message}</span>
      </div>`;
    }).join('');
  }

  // ==================== SHARED REFRESH ====================
  async function refreshAll() {
    const dashVisible = document.getElementById('view-dashboard').classList.contains('active');
    const paperVisible = document.getElementById('view-paper').classList.contains('active');
    const posVisible = document.getElementById('view-positions').classList.contains('active');
    const histVisible = document.getElementById('view-history').classList.contains('active');
    if (!dashVisible && !paperVisible && !posVisible && !histVisible) return;

    const [positions, orders, history] = await Promise.all([
      fetch('/api/paper/positions').then(r => r.json()),
      fetch('/api/paper/orders').then(r => r.json()),
      fetch('/api/paper/history?limit=30').then(r => r.json()),
    ]);

    if (dashVisible) {
      await renderDashboard();
      refreshWallets();
      renderPositionsTable(document.getElementById('dashPosTable'), positions.slice(0, 6), { withClose: false });
      renderHistoryTable(document.getElementById('dashHistTable'), history.slice(0, 6));
    }
    if (paperVisible) {
      const price = await fetch('/api/paper/price').catch(() => null);
      const tickerEl = document.getElementById('paperTicker');
      if (price && price.ok) {
        const p = await price.json();
        tickerEl.innerHTML = `<span class="bid">${fmt(p.bid, 2)}</span><span class="sep">/</span><span class="ask">${fmt(p.ask, 2)}</span> <span class="bt-panel-note">spread ${fmt(p.spread, 2)}</span>`;
      } else {
        tickerEl.textContent = 'Feed not running — start it from the Dashboard.';
      }
      renderOrdersTable(document.getElementById('paperOrdersTable'), orders);
      document.getElementById('paperOrderCount').textContent = `${orders.length} pending`;
      renderPositionsTable(document.getElementById('paperPosTable'), positions, { withClose: true });
      document.getElementById('paperPosCount').textContent = `${positions.length} open`;
      renderHistoryTable(document.getElementById('autoHistTable'), history);
      document.getElementById('autoHistCount').textContent = `${history.length} shown`;
      refreshAutoStatus();
      refreshActivity();
    }
    if (posVisible) {
      renderPositionsTable(document.getElementById('posTable'), positions, { withClose: true });
      document.getElementById('posCount').textContent = `${positions.length} open`;
    }
    if (histVisible) {
      histRows = await fetch('/api/paper/history?limit=500').then(r => r.json());
      populateHistStrategyOptions(histRows);
      renderHistTradeTable();
      await renderHistStrategyTable();
    }
  }

  // ==================== HISTORY FILTERS + STRATEGY SUMMARY ====================
  // client-side filtering over a dedicated, larger fetch (up to 500 trades)
  // so filtering isn't limited to the ~30 rows the other, lighter widgets
  // (dashboard preview, Trade tab) pull for their own quick glance.
  let histRows = [];
  let histStrategyOptions = null; // Set — rebuilt only when the actual strategy list changes

  function populateHistStrategyOptions(rows) {
    const tags = new Set(rows.map(t => t.tag || 'manual'));
    const same = histStrategyOptions && tags.size === histStrategyOptions.size
      && [...tags].every(t => histStrategyOptions.has(t));
    if (same) return;
    histStrategyOptions = tags;
    const sel = document.getElementById('histFilterStrategy');
    const current = sel.value;
    sel.innerHTML = '<option value="all">All strategies</option>'
      + [...tags].sort().map(t => `<option value="${t}">${t === 'manual' ? 'Manual' : t}</option>`).join('');
    sel.value = [...tags, 'all'].includes(current) ? current : 'all';
  }

  function renderHistTradeTable() {
    const strategy = document.getElementById('histFilterStrategy').value;
    const session = document.getElementById('histFilterSession').value;
    const side = document.getElementById('histFilterSide').value;
    const filtered = histRows.filter(t =>
      (strategy === 'all' || (t.tag || 'manual') === strategy)
      && (session === 'all' || t.session === session)
      && (side === 'all' || String(t.direction) === side)
    );
    renderHistoryTable(document.getElementById('histTable'), filtered);
    document.getElementById('histCount').textContent = `${filtered.length} of ${histRows.length} shown`;
  }

  // one row per strategy (tag) that has ever closed a trade — remaining
  // wallet balance (authoritative, from the backend) alongside profit/loss
  // totals and win/loss counts computed from the trades already on hand.
  // Clicking a row is a shortcut for picking that strategy in the dropdown
  // above the trade log, so "see this strategy's trades" is one click.
  async function renderHistStrategyTable() {
    const tableEl = document.getElementById('histStrategyTable');
    const byTag = {};
    histRows.forEach(t => {
      const tag = t.tag || 'manual';
      const s = byTag[tag] || (byTag[tag] = { wins: 0, losses: 0, profit: 0, loss: 0 });
      if (t.net_pnl > 0) { s.wins++; s.profit += t.net_pnl; } else { s.losses++; s.loss += -t.net_pnl; }
    });
    const tags = Object.keys(byTag).sort();
    if (!tags.length) {
      tableEl.innerHTML = '<tbody><tr><td style="padding:12px;color:var(--text-faint)">No closed trades yet.</td></tr></tbody>';
      return;
    }
    let balByTag = {};
    try {
      const wallets = await fetch('/api/paper/wallets').then(r => r.json());
      balByTag = Object.fromEntries(wallets.map(w => [w.wallet_key, w.balance]));
    } catch (_) { /* leave balances blank rather than fail the whole summary */ }

    const active = document.getElementById('histFilterStrategy').value;
    const head = `<thead><tr>
      <th>Strategy</th><th class="num">Balance</th><th class="num">Total profit</th><th class="num">Total loss</th>
      <th class="num">Net P&amp;L</th><th class="num">Win trades</th><th class="num">Loss trades</th>
    </tr></thead>`;
    const body = tags.map(tag => {
      const s = byTag[tag];
      const bal = balByTag[tag];
      const net = s.profit - s.loss;
      return `<tr class="hist-strategy-row${tag === active ? ' selected' : ''}" data-tag="${tag}">
        <td>${sourceTag(tag === 'manual' ? null : tag)}</td>
        <td class="num">${bal !== undefined ? fmt(bal, 2) : '—'}</td>
        <td class="num pos">${fmtSign(s.profit)}</td>
        <td class="num neg">${s.loss > 0 ? '-' + fmt(s.loss, 2) : fmt(0, 2)}</td>
        <td class="num ${net >= 0 ? 'pos' : 'neg'}">${fmtSign(net)}</td>
        <td class="num">${s.wins}</td>
        <td class="num">${s.losses}</td>
      </tr>`;
    }).join('');
    tableEl.innerHTML = head + `<tbody>${body}</tbody>`;
    tableEl.querySelectorAll('.hist-strategy-row').forEach(tr => {
      tr.addEventListener('click', () => {
        document.getElementById('histFilterStrategy').value = tr.dataset.tag;
        renderHistTradeTable();
        renderHistStrategyTable();
      });
    });
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(refreshAll, 2000);
  }

  // ==================== INIT ====================
  async function init() {
    riskSettings = await (await fetch('/api/risk/settings')).json();
    document.getElementById('paperRiskPct').value = riskSettings.risk_per_trade_pct;

    renderPills(document.getElementById('paperKindPills'), KIND_OPTS, orderKind, pickKind);
    renderPills(document.getElementById('paperDirPills'), DIR_OPTS, orderDir, pickDir);
    renderPills(document.getElementById('dashSpeedPills'), SPEEDS, currentSpeed, pickSpeed);

    ['paperStop', 'paperTrigger', 'paperRiskPct', 'paperLots'].forEach(id => {
      document.getElementById(id).addEventListener('input', updateSizePreview);
    });
    document.getElementById('paperSubmit').addEventListener('click', submitOrder);

    document.getElementById('dashFeedToggle').addEventListener('click', async () => {
      const acct = await (await fetch('/api/paper/account')).json();
      await fetch(`/api/paper/feed/${acct.feed_running ? 'stop' : 'start'}`, { method: 'POST' });
      refreshAll();
    });

    await initAutoTrade();

    ['histFilterStrategy', 'histFilterSession', 'histFilterSide'].forEach(id => {
      document.getElementById(id).addEventListener('change', () => {
        renderHistTradeTable();
        renderHistStrategyTable();
      });
    });

    startPolling();
    refreshAll();
  }

  function onShow() {
    if (!initialized) { initialized = true; init(); return; }
    refreshAutoConfigs(); // pick up strategies saved since the last visit
    startPolling();
    refreshAll();
  }

  return { onShow };
})();
