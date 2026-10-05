window.PaperView = (function () {
  let initialized = false;
  let pollTimer = null;
  let riskSettings = null;

  const fmt = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtSign = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : (n > 0 ? '+' : '') + fmt(n, d);
  // Trade times are stored/served in UTC — shown as IST (primary) with the
  // UTC time underneath in small type, same convention as the optimizer reports.
  const DT_OPTS = { year: 'numeric', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false };
  const fmtZone = (iso, timeZone) => new Date(iso).toLocaleString('en-GB', { ...DT_OPTS, timeZone }).replace(',', '');
  const fmtDT = (iso) => `<div>${fmtZone(iso, 'Asia/Kolkata')} IST</div><div class="hist-time-utc">${fmtZone(iso, 'UTC')} UTC</div>`;
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
      <td>${fmtDT(p.entry_time)}</td>
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
      <td>${fmtDT(t.exit_time)}</td>
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
    const allBtn = document.getElementById('autoSelectAllBtn');
    if (allBtn) allBtn.textContent = (autoConfigs.length && n === autoConfigs.length) ? 'Clear all' : 'Select all';
  }

  function toggleSelectAllAuto() {
    if (selectedAutoConfigs.size === autoConfigs.length) {
      selectedAutoConfigs.clear();
    } else {
      autoConfigs.forEach(cfg => selectedAutoConfigs.add(cfg.id));
    }
    renderAutoStrategyList();
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
    refreshFundWallet(running);
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
            <div class="bt-panel-note">${s.fund_wallet ? `trades from shared wallet '${s.fund_wallet}'` : `wallet '${s.wallet}'`} · ${s.timeframe} · ${modeTxt} · ${riskTxt}</div>
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

  // every auto-trading strategy shares one wallet (webapp.portfolio) — show
  // its money once above the list instead of implying each row has its own
  async function refreshFundWallet(running) {
    const el = document.getElementById('autoFundWallet');
    const fund = (running.find(s => s.fund_wallet) || {}).fund_wallet || 'portfolio';
    try {
      const w = await (await fetch(`/api/paper/account?wallet=${encodeURIComponent(fund)}`)).json();
      const money = v => `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      el.textContent = `Shared wallet '${fund}': balance ${money(w.balance)} · equity ${money(w.equity)} · `
        + `${w.open_positions} open · ${w.trades} closed trades · started at ${money(w.starting_balance)}`;
    } catch (err) {
      el.textContent = '';
    }
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

  // limits for the shared wallet (webapp.portfolio) — loaded once rather
  // than on the 2s status poll, so a half-typed value isn't overwritten
  const PF_FIELDS = { pfMaxOpen: 'max_open', pfMaxDay: 'max_trades_per_day', pfMaxSession: 'max_trades_per_session' };

  async function loadPortfolioLimits() {
    try {
      const s = await (await fetch('/api/paper/portfolio/settings')).json();
      Object.entries(PF_FIELDS).forEach(([id, key]) => { document.getElementById(id).value = s[key]; });
    } catch (err) {
      console.error('loadPortfolioLimits: failed', err);
    }
  }

  async function savePortfolioLimits() {
    const msg = document.getElementById('pfSaveMsg');
    const body = {};
    Object.entries(PF_FIELDS).forEach(([id, key]) => { body[key] = Number(document.getElementById(id).value); });
    const res = await fetch('/api/paper/portfolio/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (res.ok) {
      msg.textContent = 'Saved';
      refreshActivity();
    } else {
      const d = await res.json().catch(() => ({}));
      msg.textContent = d.detail ? `Not saved: ${typeof d.detail === 'string' ? d.detail : 'enter whole numbers from 1 to 1000'}` : 'Not saved';
    }
    setTimeout(() => { msg.textContent = ''; }, 4000);
  }

  async function initAutoTrade() {
    document.getElementById('autoToggle').addEventListener('click', startAutoTrade);
    document.getElementById('autoSelectAllBtn').addEventListener('click', toggleSelectAllAuto);
    document.getElementById('pfSaveBtn').addEventListener('click', savePortfolioLimits);
    loadPortfolioLimits();
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
      // every wallet's positions, matching the table — acct.open_positions is the manual wallet only
      document.getElementById('dashPosCount').textContent = `${positions.length} open`;
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
    if (histVisible) await refreshHistory();
  }

  // ==================== HISTORY VIEW ====================
  // Client-side filtering over a dedicated, larger fetch (up to 500 trades)
  // so filtering isn't limited to the ~30 rows the other, lighter widgets
  // (dashboard preview, Trade tab) pull for their own quick glance. Every
  // filter re-renders the KPIs, equity curve and both tables immediately.
  let histRows = [];
  let histBalances = {};
  let histSig = null; // polls every 2s — only re-render when the trade list actually changed
  let histStrategyOptions = null; // Set — rebuilt only when the actual strategy list changes
  let histReasonOptions = null;
  const HIST_DEFAULTS = { period: 'all', from: '', to: '', strategy: 'all', session: 'all', side: 'all', outcome: 'all', reason: 'all' };
  const histState = { ...HIST_DEFAULTS };
  const histSort = { trades: { key: 'exit_time', dir: -1 }, strats: { key: 'net', dir: -1 } };
  const HIST_STATE_KEY = 'goldterm.histFilters.v1';
  // Filters are remembered per browser as a convenience; everything still
  // works if storage is blocked.
  try { Object.assign(histState, JSON.parse(localStorage.getItem(HIST_STATE_KEY) || '{}')); } catch (_) { /* ignore */ }
  const saveHistState = () => { try { localStorage.setItem(HIST_STATE_KEY, JSON.stringify(histState)); } catch (_) { /* ignore */ } };

  const IST_OFFSET_MS = 5.5 * 3600 * 1000;
  // 'YYYY-MM-DD' read as an IST calendar day -> UTC ms of that day's IST midnight
  const istDayStartMs = (ymd) => { const [y, m, d] = ymd.split('-').map(Number); return Date.UTC(y, m - 1, d) - IST_OFFSET_MS; };
  const istTodayYmd = () => new Date(Date.now() + IST_OFFSET_MS).toISOString().slice(0, 10);

  function histPeriodBounds() {
    const p = histState.period;
    if (p === 'today') return [istDayStartMs(istTodayYmd()), null];
    if (p === '7' || p === '30') return [Date.now() - Number(p) * 86400000, null];
    if (p === 'custom') {
      return [histState.from ? istDayStartMs(histState.from) : null,
        histState.to ? istDayStartMs(histState.to) + 86400000 : null];
    }
    return [null, null];
  }

  // ignoreStrategy: the strategy table shows every strategy under the other
  // filters, so picking one there doesn't make the rest disappear.
  function histFilter(rows, { ignoreStrategy = false } = {}) {
    const [start, end] = histPeriodBounds();
    const s = histState;
    return rows.filter(t => {
      const ms = Date.parse(t.exit_time);
      return (start == null || ms >= start) && (end == null || ms < end)
        && (ignoreStrategy || s.strategy === 'all' || (t.tag || 'manual') === s.strategy)
        && (s.session === 'all' || t.session === s.session)
        && (s.side === 'all' || String(t.direction) === s.side)
        && (s.outcome === 'all' || (s.outcome === 'win' ? t.net_pnl > 0 : t.net_pnl <= 0))
        && (s.reason === 'all' || t.exit_reason === s.reason);
    });
  }

  function histStats(rows) {
    let wins = 0, profit = 0, loss = 0, rSum = 0, rN = 0, best = null, worst = null;
    rows.forEach(t => {
      if (t.net_pnl > 0) { wins++; profit += t.net_pnl; } else { loss += -t.net_pnl; }
      if (t.r_multiple != null) { rSum += t.r_multiple; rN++; }
      if (best == null || t.net_pnl > best) best = t.net_pnl;
      if (worst == null || t.net_pnl < worst) worst = t.net_pnl;
    });
    const n = rows.length;
    return {
      n, wins, losses: n - wins, profit, loss, net: profit - loss,
      winRate: n ? wins / n : null,
      pf: loss > 0 ? profit / loss : (profit > 0 ? Infinity : null),
      avgR: rN ? rSum / rN : null, best, worst,
    };
  }
  const fmtPf = (v) => v == null ? '—' : v === Infinity ? '∞' : fmt(v, 2);
  const fmtPct = (v) => v == null ? '—' : `${(v * 100).toFixed(0)}%`;
  const pnlCls = (v) => v == null || v === 0 ? '' : v > 0 ? 'pos' : 'neg';
  function fmtHeld(ms) {
    if (!isFinite(ms) || ms < 0) return '—';
    const m = Math.round(ms / 60000);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ${m % 60}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  }

  function syncOptions(selId, values, prev, allLabel, labelOf, stateKey) {
    const same = prev && values.size === prev.size && [...values].every(v => prev.has(v));
    if (same) return prev;
    const sel = document.getElementById(selId);
    sel.innerHTML = `<option value="all">${allLabel}</option>`
      + [...values].sort().map(v => `<option value="${v}">${labelOf(v)}</option>`).join('');
    // keep a remembered choice even if it has no trades right now, so a
    // filter restored from a previous visit isn't silently dropped
    if (histState[stateKey] !== 'all' && !values.has(histState[stateKey])) {
      sel.insertAdjacentHTML('beforeend', `<option value="${histState[stateKey]}">${labelOf(histState[stateKey])}</option>`);
    }
    sel.value = histState[stateKey];
    return values;
  }

  function populateHistOptions(rows) {
    histStrategyOptions = syncOptions('histFilterStrategy', new Set(rows.map(t => t.tag || 'manual')), histStrategyOptions,
      'All strategies', t => t === 'manual' ? 'Manual' : t, 'strategy');
    histReasonOptions = syncOptions('histFilterReason', new Set(rows.map(t => t.exit_reason)), histReasonOptions,
      'All reasons', r => EXIT_LABEL[r] || r, 'reason');
  }

  function syncHistControls() {
    [['histPeriod', 'period'], ['histSide', 'side'], ['histOutcome', 'outcome']].forEach(([id, key]) => {
      document.querySelectorAll(`#${id} button`).forEach(b => {
        const on = b.dataset.v === histState[key];
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', on);
      });
    });
    document.getElementById('histCustom').hidden = histState.period !== 'custom';
    document.getElementById('histFrom').value = histState.from;
    document.getElementById('histTo').value = histState.to;
    document.getElementById('histFilterSession').value = histState.session;
    document.getElementById('histFilterStrategy').value = histState.strategy;
    document.getElementById('histFilterReason').value = histState.reason;
  }

  function renderHistory() {
    const trades = histFilter(histRows);
    const s = histStats(trades);
    document.getElementById('histCount').textContent = `${trades.length} of ${histRows.length} trades`;
    document.getElementById('histKpis').innerHTML = [
      statTile(fmtSign(s.n ? s.net : null), 'Net P&amp;L', pnlCls(s.net)),
      statTile(s.n, 'Trades'),
      statTile(fmtPct(s.winRate), `Win rate · ${s.wins}W / ${s.losses}L`),
      statTile(fmtPf(s.pf), 'Profit factor'),
      statTile(s.avgR == null ? '—' : fmtSign(s.avgR) + 'R', 'Avg R', pnlCls(s.avgR)),
      statTile(fmtSign(s.best), 'Best trade', pnlCls(s.best)),
      statTile(fmtSign(s.worst), 'Worst trade', pnlCls(s.worst)),
    ].join('');
    renderHistEquity(trades);
    renderHistStrategyTable();
    renderHistTradeTable(trades);
  }

  // ---- equity curve: one series, so no legend — the panel title names it ----
  function renderHistEquity(trades) {
    const box = document.getElementById('histEquity');
    const pts = [...trades].sort((a, b) => Date.parse(a.exit_time) - Date.parse(b.exit_time));
    if (pts.length < 2) {
      box.innerHTML = `<div class="hist-equity-empty">${pts.length ? 'Need at least 2 trades to draw a curve.' : 'No trades match these filters.'}</div>`;
      return;
    }
    let cum = 0;
    const ys = pts.map(t => (cum += t.net_pnl));
    const W = Math.max(box.clientWidth, 300), H = 170, padL = 56, padR = 12, padT = 12, padB = 22;
    const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
    const span = hi - lo || 1;
    const x = (i) => padL + (i / (pts.length - 1)) * (W - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / span) * (H - padT - padB);
    const line = ys.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');
    const area = `${line}L${x(pts.length - 1).toFixed(1)},${y(0).toFixed(1)}L${x(0).toFixed(1)},${y(0).toFixed(1)}Z`;
    const dayLbl = (iso) => fmtZone(iso, 'Asia/Kolkata').split(' ').slice(0, 2).join(' ');
    const final = ys[ys.length - 1];
    box.innerHTML = `
      <svg class="hist-equity-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
           aria-label="Equity curve: ${pts.length} trades, ending at ${fmtSign(final)}">
        <line class="eq-grid" x1="${padL}" x2="${W - padR}" y1="${y(hi)}" y2="${y(hi)}"/>
        <line class="eq-grid" x1="${padL}" x2="${W - padR}" y1="${y(lo)}" y2="${y(lo)}"/>
        <line class="eq-zero" x1="${padL}" x2="${W - padR}" y1="${y(0)}" y2="${y(0)}"/>
        <text class="eq-axis" x="${padL - 8}" y="${y(hi) + 4}" text-anchor="end">${fmtSign(hi)}</text>
        ${lo < 0 && hi > 0 ? `<text class="eq-axis" x="${padL - 8}" y="${y(0) + 4}" text-anchor="end">0.00</text>` : ''}
        <text class="eq-axis" x="${padL - 8}" y="${y(lo) + 4}" text-anchor="end">${fmtSign(lo)}</text>
        <text class="eq-axis" x="${padL}" y="${H - 5}">${dayLbl(pts[0].exit_time)}</text>
        <text class="eq-axis" x="${W - padR}" y="${H - 5}" text-anchor="end">${dayLbl(pts[pts.length - 1].exit_time)}</text>
        <path class="eq-area" d="${area}"/>
        <path class="eq-line" d="${line}"/>
        <line class="eq-cross" id="eqCross" y1="${padT}" y2="${H - padB}" visibility="hidden"/>
        <circle class="eq-dot" id="eqDot" r="4" visibility="hidden"/>
        <rect x="${padL}" y="0" width="${W - padL - padR}" height="${H}" fill="transparent" id="eqHit"/>
      </svg>
      <div class="eq-tip" id="eqTip" hidden></div>`;

    const cross = box.querySelector('#eqCross'), dot = box.querySelector('#eqDot'), tip = box.querySelector('#eqTip');
    const hit = box.querySelector('#eqHit');
    hit.addEventListener('mousemove', (e) => {
      const rect = box.querySelector('svg').getBoundingClientRect();
      const px = (e.clientX - rect.left) * (W / rect.width);
      const i = Math.max(0, Math.min(pts.length - 1, Math.round((px - padL) / (W - padL - padR) * (pts.length - 1))));
      const t = pts[i];
      cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i));
      dot.setAttribute('cx', x(i)); dot.setAttribute('cy', y(ys[i]));
      cross.setAttribute('visibility', 'visible'); dot.setAttribute('visibility', 'visible');
      tip.hidden = false;
      tip.innerHTML = `<div class="eq-tip-time">${fmtZone(t.exit_time, 'Asia/Kolkata')} IST</div>
        <div>${t.tag || 'manual'} · ${t.direction === 1 ? 'Long' : 'Short'}</div>
        <div>Trade <b class="${pnlCls(t.net_pnl)}">${fmtSign(t.net_pnl)}</b></div>
        <div>Cumulative <b class="${pnlCls(ys[i])}">${fmtSign(ys[i])}</b></div>`;
      const left = (x(i) / W) * rect.width;
      tip.style.left = `${Math.min(Math.max(left, 80), rect.width - 80)}px`;
    });
    hit.addEventListener('mouseleave', () => {
      cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); tip.hidden = true;
    });
  }

  // ---- sortable tables ----
  function sortRows(rows, cols, sort) {
    const col = cols.find(c => c.key === sort.key);
    if (!col || !col.sort) return rows;
    return [...rows].sort((a, b) => {
      const va = col.sort(a), vb = col.sort(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;   // missing values always sink
      if (vb == null) return -1;
      return (va < vb ? -1 : va > vb ? 1 : 0) * sort.dir;
    });
  }
  function sortHead(cols, sort) {
    return `<thead><tr>${cols.map(c => {
      const on = c.sort && sort.key === c.key;
      const aria = on ? ` aria-sort="${sort.dir > 0 ? 'ascending' : 'descending'}"` : '';
      return `<th class="${c.num ? 'num' : ''}${c.sort ? ' sortable-th' : ''}${on ? ' sorted' : ''}" data-key="${c.key}"${aria}>`
        + (c.sort ? `<button type="button">${c.label}<span class="sort-ind">${on ? (sort.dir > 0 ? '▲' : '▼') : ''}</span></button>` : c.label)
        + '</th>';
    }).join('')}</tr></thead>`;
  }
  function bindSort(tableEl, sort, rerender) {
    tableEl.querySelectorAll('th.sortable-th button').forEach(btn => btn.addEventListener('click', () => {
      const key = btn.parentElement.dataset.key;
      if (sort.key === key) sort.dir = -sort.dir; else { sort.key = key; sort.dir = -1; }
      rerender();
    }));
  }

  const STRAT_COLS = [
    { key: 'tag', label: 'Strategy', sort: r => r.tag },
    { key: 'bal', label: 'Balance', num: true, sort: r => r.bal },
    { key: 'n', label: 'Trades', num: true, sort: r => r.n },
    { key: 'winRate', label: 'Win rate', sort: r => r.winRate },
    { key: 'profit', label: 'Total profit', num: true, sort: r => r.profit },
    { key: 'loss', label: 'Total loss', num: true, sort: r => r.loss },
    { key: 'net', label: 'Net P&amp;L', num: true, sort: r => r.net },
    { key: 'pf', label: 'Profit factor', num: true, sort: r => r.pf },
    { key: 'avgR', label: 'Avg R', num: true, sort: r => r.avgR },
  ];

  // one row per strategy (tag) with trades under the current filters —
  // remaining wallet balance (authoritative, from the backend) alongside
  // stats computed from the trades on hand. Clicking a row picks that
  // strategy in the filter bar; clicking it again clears it.
  function renderHistStrategyTable() {
    const tableEl = document.getElementById('histStrategyTable');
    const byTag = {};
    histFilter(histRows, { ignoreStrategy: true }).forEach(t => { (byTag[t.tag || 'manual'] ||= []).push(t); });
    const rows = Object.entries(byTag).map(([tag, ts]) => ({ tag, bal: histBalances[tag], ...histStats(ts) }));
    if (!rows.length) {
      tableEl.innerHTML = `<tbody><tr><td class="hist-empty">${histRows.length ? 'No trades match these filters.' : 'No closed trades yet.'}</td></tr></tbody>`;
      return;
    }
    const active = histState.strategy;
    const body = sortRows(rows, STRAT_COLS, histSort.strats).map(r => `
      <tr class="hist-strategy-row${r.tag === active ? ' selected' : ''}" data-tag="${r.tag}" tabindex="0">
        <td>${sourceTag(r.tag === 'manual' ? null : r.tag)}</td>
        <td class="num">${r.bal !== undefined ? fmt(r.bal, 2) : '—'}</td>
        <td class="num">${r.n}</td>
        <td><div class="wr-cell"><span class="num">${fmtPct(r.winRate)}</span><span class="wr-bar" aria-hidden="true"><i style="width:${((r.winRate || 0) * 100).toFixed(1)}%"></i></span><span class="wr-count">${r.wins}/${r.losses}</span></div></td>
        <td class="num pos">${fmtSign(r.profit)}</td>
        <td class="num neg">${r.loss > 0 ? '-' + fmt(r.loss, 2) : fmt(0, 2)}</td>
        <td class="num ${pnlCls(r.net)}"><b>${fmtSign(r.net)}</b></td>
        <td class="num">${fmtPf(r.pf)}</td>
        <td class="num ${pnlCls(r.avgR)}">${r.avgR == null ? '—' : fmtSign(r.avgR)}</td>
      </tr>`).join('');
    tableEl.innerHTML = sortHead(STRAT_COLS, histSort.strats) + `<tbody>${body}</tbody>`;
    bindSort(tableEl, histSort.strats, renderHistStrategyTable);
    tableEl.querySelectorAll('.hist-strategy-row').forEach(tr => {
      const pick = () => {
        histState.strategy = histState.strategy === tr.dataset.tag ? 'all' : tr.dataset.tag;
        onHistFilterChange();
      };
      tr.addEventListener('click', pick);
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
    });
  }

  const TRADE_COLS = [
    { key: 'exit_time', label: 'Exit', sort: t => Date.parse(t.exit_time) },
    { key: 'direction', label: 'Side', sort: t => t.direction },
    { key: 'tag', label: 'Strategy', sort: t => t.tag || 'manual' },
    { key: 'session', label: 'Session', sort: t => t.session },
    { key: 'held', label: 'Held', num: true, sort: t => Date.parse(t.exit_time) - Date.parse(t.entry_time) },
    { key: 'lots', label: 'Lots', num: true, sort: t => t.lots },
    { key: 'entry_price', label: 'Entry', num: true, sort: t => t.entry_price },
    { key: 'exit_price', label: 'Exit price', num: true, sort: t => t.exit_price },
    { key: 'r_multiple', label: 'R', num: true, sort: t => t.r_multiple },
    { key: 'net_pnl', label: 'Net P&amp;L', num: true, sort: t => t.net_pnl },
    { key: 'exit_reason', label: 'Reason', sort: t => EXIT_LABEL[t.exit_reason] || t.exit_reason },
  ];

  let histTradesShown = [];
  function renderHistTradeTable(trades = histTradesShown) {
    histTradesShown = trades;
    const tableEl = document.getElementById('histTable');
    if (!trades.length) {
      tableEl.innerHTML = `<tbody><tr><td class="hist-empty">${histRows.length ? 'No trades match these filters.' : 'No closed trades yet.'}</td></tr></tbody>`;
      return;
    }
    const body = sortRows(trades, TRADE_COLS, histSort.trades).map(t => `<tr>
      <td>${fmtDT(t.exit_time)}</td>
      <td><span class="pill-tag ${t.direction === 1 ? 'long' : 'short'}">${t.direction === 1 ? 'LONG' : 'SHORT'}</span></td>
      <td>${sourceTag(t.tag)}</td>
      <td>${SESSION_LABEL[t.session] || t.session}</td>
      <td class="num">${fmtHeld(Date.parse(t.exit_time) - Date.parse(t.entry_time))}</td>
      <td class="num">${fmt(t.lots, 2)}</td>
      <td class="num">${fmt(t.entry_price)}</td>
      <td class="num">${fmt(t.exit_price)}</td>
      <td class="num ${t.r_multiple >= 0 ? 'pos' : 'neg'}">${fmtSign(t.r_multiple)}</td>
      <td class="num ${t.net_pnl >= 0 ? 'pos' : 'neg'}">${fmtSign(t.net_pnl)}</td>
      <td>${EXIT_LABEL[t.exit_reason] || t.exit_reason}</td>
    </tr>`).join('');
    tableEl.innerHTML = sortHead(TRADE_COLS, histSort.trades) + `<tbody>${body}</tbody>`;
    bindSort(tableEl, histSort.trades, () => renderHistTradeTable());
  }

  function onHistFilterChange() {
    saveHistState();
    syncHistControls();
    renderHistory();
  }

  async function refreshHistory() {
    const rows = await fetch('/api/paper/history?limit=500').then(r => r.json());
    const sig = `${rows.length}:${rows.length ? rows[0].id : ''}`;
    if (sig === histSig) return;
    histSig = sig;
    histRows = rows;
    try {
      const wallets = await fetch('/api/paper/wallets').then(r => r.json());
      histBalances = Object.fromEntries(wallets.map(w => [w.wallet_key, w.balance]));
    } catch (_) { /* leave balances blank rather than fail the whole view */ }
    populateHistOptions(histRows);
    renderHistory();
  }

  function initHistory() {
    [['histPeriod', 'period'], ['histSide', 'side'], ['histOutcome', 'outcome']].forEach(([id, key]) => {
      document.querySelectorAll(`#${id} button`).forEach(b => b.addEventListener('click', () => {
        histState[key] = b.dataset.v;
        onHistFilterChange();
      }));
    });
    [['histFilterStrategy', 'strategy'], ['histFilterSession', 'session'], ['histFilterReason', 'reason'],
      ['histFrom', 'from'], ['histTo', 'to']].forEach(([id, key]) => {
      document.getElementById(id).addEventListener('change', (e) => { histState[key] = e.target.value; onHistFilterChange(); });
    });
    document.getElementById('histReset').addEventListener('click', () => {
      Object.assign(histState, HIST_DEFAULTS);
      onHistFilterChange();
    });
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (document.getElementById('view-history').classList.contains('active')) renderHistEquity(histFilter(histRows));
      }, 150);
    });
    syncHistControls();
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

    initHistory();

    startPolling();
    refreshAll();
  }

  function onShow() {
    if (!initialized) { initialized = true; init(); return; }
    refreshAutoConfigs(); // pick up strategies saved since the last visit
    histSig = null; // wallet balances may have moved while away — redraw History once
    startPolling();
    refreshAll();
  }

  return { onShow };
})();
