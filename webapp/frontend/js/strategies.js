window.StrategiesView = (function () {
  let initialized = false;
  let schemas = [];
  let configs = [];
  let marketMeta = null;
  let editingId = null;
  const selectedForCompare = new Set();
  let compareChart = null;

  const TIMEFRAMES = [
    { id: '1min', label: '1m' }, { id: '5min', label: '5m' }, { id: '15min', label: '15m' },
    { id: '30min', label: '30m' }, { id: '1h', label: '1h' }, { id: '4h', label: '4h' }, { id: '1d', label: '1D' },
  ];
  const SESSIONS = [
    { id: 'all', label: 'All' }, { id: 'london', label: 'London' },
    { id: 'ny', label: 'New York' }, { id: 'overlap', label: 'Overlap' },
  ];
  let compareTf = '15min';
  let compareSession = 'all';

  // ==================== NO-CODE STRATEGY BUILDER ====================
  let builderMode = 'builtin'; // 'builtin' | 'custom'
  let indicatorCatalog = [];   // from /api/strategies/indicators
  let indicatorCatalogMap = {};
  let priceFields = [];
  let operators = [];
  let builderIndicators = [];        // [{uid, type, params}]
  let builderIndicatorCounters = {}; // type -> next number, for auto uid
  let builderLongCombinator = 'and';
  let builderShortCombinator = 'and';
  const OPERATOR_LABELS = { '<': '<', '<=': '≤', '>': '>', '>=': '≥', '==': '=', cross_above: 'crosses above', cross_below: 'crosses below' };

  function resetBuilderState() {
    builderIndicators = [];
    builderIndicatorCounters = {};
    builderLongCombinator = 'and';
    builderShortCombinator = 'and';
    document.getElementById('builderSlType').value = 'atr_mult';
    document.getElementById('builderSlValue').value = '1.5';
    document.getElementById('builderTpType').value = 'rr';
    document.getElementById('builderTpValue').value = '2.0';
    document.getElementById('builderTrail').value = '';
    document.getElementById('builderTimeframe').value = '15min';
    document.getElementById('builderSessionStart').value = '';
    document.getElementById('builderSessionEnd').value = '';
    document.getElementById('builderError').hidden = true;
    renderBuilderIndicators();
    document.getElementById('builderLongConditions').innerHTML = '';
    document.getElementById('builderShortConditions').innerHTML = '';
    renderBuilderConditions('long');
    renderBuilderConditions('short');
    renderCombinatorPills('long');
    renderCombinatorPills('short');
  }

  function operandOptionsHtml(selected) {
    let html = '<optgroup label="Price">';
    priceFields.forEach(f => { html += `<option value="${f}"${selected === f ? ' selected' : ''}>${f}</option>`; });
    html += '</optgroup>';
    if (builderIndicators.length) {
      html += '<optgroup label="Indicators">';
      builderIndicators.forEach(ind => {
        const cat = indicatorCatalogMap[ind.type];
        cat.outputs.forEach(tmpl => {
          const col = tmpl.replace('{id}', ind.uid);
          html += `<option value="${col}"${selected === col ? ' selected' : ''}>${col}</option>`;
        });
      });
      html += '</optgroup>';
    }
    html += `<option value="__num__"${selected === '__num__' ? ' selected' : ''}>Custom number…</option>`;
    return html;
  }

  function refreshAllOperandSelects() {
    document.querySelectorAll('.cond-left, .cond-right').forEach(sel => {
      const current = sel.value;
      sel.innerHTML = operandOptionsHtml(current);
      const numInput = sel.parentElement.querySelector(sel.classList.contains('cond-left') ? '.cond-left-num' : '.cond-right-num');
      numInput.hidden = sel.value !== '__num__';
    });
  }

  // ---------------- indicators ----------------
  function renderBuilderIndicators() {
    const el = document.getElementById('builderIndicators');
    if (!builderIndicators.length) {
      el.innerHTML = '<div class="builder-empty-note">No indicators added yet — conditions can still use raw price (open/high/low/close).</div>';
      return;
    }
    el.innerHTML = builderIndicators.map(ind => {
      const cat = indicatorCatalogMap[ind.type];
      const paramInputs = cat.params.map(p => {
        if (p.type === 'source') {
          const opts = priceFields.map(f => `<option value="${f}"${ind.params[p.name] === f ? ' selected' : ''}>${f}</option>`).join('');
          return `<label class="builder-param-inline">${p.name} <select data-uid="${ind.uid}" data-param="${p.name}">${opts}</select></label>`;
        }
        return `<label class="builder-param-inline">${p.name} <input type="number" step="${p.type === 'float' ? 'any' : '1'}" value="${ind.params[p.name]}" data-uid="${ind.uid}" data-param="${p.name}"></label>`;
      }).join('');
      return `<div class="builder-ind-row">
        <div class="builder-ind-row-fields">
          <span class="builder-ind-label">${ind.uid}</span>
          <span class="bt-panel-note">${cat.label}</span>
          ${paramInputs}
        </div>
        <button type="button" class="builder-remove-btn" data-remove-ind="${ind.uid}" title="Remove">✕</button>
      </div>`;
    }).join('');

    el.querySelectorAll('[data-uid]').forEach(input => {
      input.addEventListener('change', () => {
        const ind = builderIndicators.find(i => i.uid === input.dataset.uid);
        if (!ind) return;
        const raw = input.value;
        ind.params[input.dataset.param] = input.tagName === 'SELECT' ? raw : Number(raw);
      });
    });
    el.querySelectorAll('[data-remove-ind]').forEach(btn => {
      btn.addEventListener('click', () => {
        builderIndicators = builderIndicators.filter(i => i.uid !== btn.dataset.removeInd);
        renderBuilderIndicators();
        refreshAllOperandSelects();
      });
    });
  }

  function addBuilderIndicator() {
    const type = document.getElementById('builderAddIndicator').value;
    const cat = indicatorCatalogMap[type];
    if (!cat) return;
    builderIndicatorCounters[type] = (builderIndicatorCounters[type] || 0) + 1;
    const uid = `${type}${builderIndicatorCounters[type]}`;
    const params = {};
    cat.params.forEach(p => { params[p.name] = p.default; });
    builderIndicators.push({ uid, type, params });
    renderBuilderIndicators();
    refreshAllOperandSelects();
  }

  // ---------------- entry conditions ----------------
  function conditionRowHtml() {
    return `<div class="builder-cond-row">
      <div class="builder-cond-row-fields">
        <select class="cond-left bt-select">${operandOptionsHtml('close')}</select>
        <input type="number" class="cond-left-num" step="any" hidden>
        <select class="cond-op bt-select">${operators.map(op => `<option value="${op}">${OPERATOR_LABELS[op] || op}</option>`).join('')}</select>
        <select class="cond-right bt-select">${operandOptionsHtml(null)}</select>
        <input type="number" class="cond-right-num" step="any" hidden value="0">
      </div>
      <button type="button" class="builder-remove-btn" title="Remove condition">✕</button>
    </div>`;
  }

  function wireConditionRow(rowEl) {
    const leftSel = rowEl.querySelector('.cond-left');
    const leftNum = rowEl.querySelector('.cond-left-num');
    const rightSel = rowEl.querySelector('.cond-right');
    const rightNum = rowEl.querySelector('.cond-right-num');
    leftSel.addEventListener('change', () => { leftNum.hidden = leftSel.value !== '__num__'; });
    rightSel.addEventListener('change', () => { rightNum.hidden = rightSel.value !== '__num__'; });
    rowEl.querySelector('.builder-remove-btn').addEventListener('click', () => rowEl.remove());
  }

  function renderBuilderConditions(side) {
    const containerId = side === 'long' ? 'builderLongConditions' : 'builderShortConditions';
    const el = document.getElementById(containerId);
    if (!el.children.length) {
      el.innerHTML = '<div class="builder-empty-note">No conditions yet.</div>';
    }
  }

  function addBuilderCondition(side) {
    const containerId = side === 'long' ? 'builderLongConditions' : 'builderShortConditions';
    const el = document.getElementById(containerId);
    if (el.querySelector('.builder-empty-note')) el.innerHTML = '';
    const wrapper = document.createElement('div');
    wrapper.innerHTML = conditionRowHtml();
    const rowEl = wrapper.firstElementChild;
    el.appendChild(rowEl);
    wireConditionRow(rowEl);
  }

  function collectCondition(rowEl) {
    const leftSel = rowEl.querySelector('.cond-left').value;
    const left = leftSel === '__num__' ? Number(rowEl.querySelector('.cond-left-num').value) : leftSel;
    const op = rowEl.querySelector('.cond-op').value;
    const rightSel = rowEl.querySelector('.cond-right').value;
    const right = rightSel === '__num__' ? Number(rowEl.querySelector('.cond-right-num').value) : rightSel;
    return { left, op, right };
  }

  function renderCombinatorPills(side) {
    const containerId = side === 'long' ? 'builderLongCombinator' : 'builderShortCombinator';
    const current = side === 'long' ? builderLongCombinator : builderShortCombinator;
    renderPills(document.getElementById(containerId), [{ id: 'and', label: 'AND' }, { id: 'or', label: 'OR' }], current, (id) => {
      if (side === 'long') builderLongCombinator = id; else builderShortCombinator = id;
      renderCombinatorPills(side);
    });
  }

  function collectConditionGroup(side) {
    const containerId = side === 'long' ? 'builderLongConditions' : 'builderShortConditions';
    const rows = Array.from(document.querySelectorAll(`#${containerId} .builder-cond-row`));
    if (!rows.length) return null;
    const combinator = side === 'long' ? builderLongCombinator : builderShortCombinator;
    return { combinator, conditions: rows.map(collectCondition) };
  }

  // ---------------- spec assembly ----------------
  function collectBuilderSpec(name) {
    const indicators = builderIndicators.map(ind => ({ id: ind.uid, type: ind.type, params: ind.params }));
    const entry_long = collectConditionGroup('long');
    const entry_short = collectConditionGroup('short');

    const slType = document.getElementById('builderSlType').value;
    const slValue = Number(document.getElementById('builderSlValue').value);
    const stop_loss = slType === 'atr_mult' ? { type: 'atr_mult', mult: slValue }
      : slType === 'fixed_points' ? { type: 'fixed_points', points: slValue }
      : { type: 'fixed_pct', pct: slValue };

    const tpType = document.getElementById('builderTpType').value;
    const tpValue = Number(document.getElementById('builderTpValue').value);
    const take_profit = tpType === 'none' ? null
      : tpType === 'rr' ? { type: 'rr', rr: tpValue }
      : tpType === 'fixed_points' ? { type: 'fixed_points', points: tpValue }
      : { type: 'fixed_pct', pct: tpValue };

    const trailVal = document.getElementById('builderTrail').value;
    const sessStart = document.getElementById('builderSessionStart').value.trim();
    const sessEnd = document.getElementById('builderSessionEnd').value.trim();

    return {
      strategy_name: name,
      timeframe: document.getElementById('builderTimeframe').value,
      indicators,
      entry_long, entry_short,
      stop_loss, take_profit,
      trail_atr_mult: trailVal ? Number(trailVal) : null,
      session: (sessStart && sessEnd) ? { start: sessStart, end: sessEnd } : null,
    };
  }

  function loadBuilderSpec(spec) {
    resetBuilderState();
    builderIndicators = (spec.indicators || []).map(ind => ({ uid: ind.id, type: ind.type, params: { ...ind.params } }));
    (spec.indicators || []).forEach(ind => {
      const n = parseInt(ind.id.replace(ind.type, ''), 10);
      if (!isNaN(n)) builderIndicatorCounters[ind.type] = Math.max(builderIndicatorCounters[ind.type] || 0, n);
    });
    renderBuilderIndicators();

    function fillSide(side, group) {
      const containerId = side === 'long' ? 'builderLongConditions' : 'builderShortConditions';
      const el = document.getElementById(containerId);
      el.innerHTML = '';
      if (!group || !group.conditions.length) { renderBuilderConditions(side); renderCombinatorPills(side); return; }
      if (side === 'long') builderLongCombinator = group.combinator; else builderShortCombinator = group.combinator;
      group.conditions.forEach(cond => {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = conditionRowHtml();
        const rowEl = wrapper.firstElementChild;
        el.appendChild(rowEl);
        const isNumLeft = typeof cond.left === 'number';
        const isNumRight = typeof cond.right === 'number';
        rowEl.querySelector('.cond-left').innerHTML = operandOptionsHtml(isNumLeft ? '__num__' : cond.left);
        if (isNumLeft) { rowEl.querySelector('.cond-left-num').hidden = false; rowEl.querySelector('.cond-left-num').value = cond.left; }
        rowEl.querySelector('.cond-op').value = cond.op;
        rowEl.querySelector('.cond-right').innerHTML = operandOptionsHtml(isNumRight ? '__num__' : cond.right);
        if (isNumRight) { rowEl.querySelector('.cond-right-num').hidden = false; rowEl.querySelector('.cond-right-num').value = cond.right; }
        wireConditionRow(rowEl);
      });
      renderCombinatorPills(side);
    }
    fillSide('long', spec.entry_long);
    fillSide('short', spec.entry_short);

    if (spec.stop_loss) {
      document.getElementById('builderSlType').value = spec.stop_loss.type;
      document.getElementById('builderSlValue').value = spec.stop_loss.mult ?? spec.stop_loss.points ?? spec.stop_loss.pct ?? '';
    }
    if (spec.take_profit) {
      document.getElementById('builderTpType').value = spec.take_profit.type;
      document.getElementById('builderTpValue').value = spec.take_profit.rr ?? spec.take_profit.points ?? spec.take_profit.pct ?? '';
    } else {
      document.getElementById('builderTpType').value = 'none';
    }
    document.getElementById('builderTrail').value = spec.trail_atr_mult ?? '';
    document.getElementById('builderTimeframe').value = spec.timeframe || '15min';
    if (spec.session) {
      document.getElementById('builderSessionStart').value = spec.session.start;
      document.getElementById('builderSessionEnd').value = spec.session.end;
    }
  }

  const fmt = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtSign = (n, d = 2) => (n === null || n === undefined || !isFinite(n)) ? '—' : (n > 0 ? '+' : '') + fmt(n, d);
  const SERIES_COLORS = ['#3987e5', '#d95926', '#199e70', '#d8ac47', '#9b8cf2'];

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

  // ---------------- param form (mirrors backtest.js's, kept local —
  // different container/consumer, small enough not to be worth sharing) ----
  function renderParamForm(schema, containerEl) {
    containerEl.innerHTML = '';
    schema.params.forEach(p => {
      const row = document.createElement('div');
      row.className = 'bt-param';
      const label = document.createElement('label');
      label.textContent = p.name.replace(/_/g, ' ');
      label.setAttribute('for', `strat_param_${p.name}`);
      row.appendChild(label);
      let input;
      if (p.type === 'bool') {
        input = document.createElement('input'); input.type = 'checkbox'; input.checked = !!p.default;
      } else if (p.type === 'tuple') {
        input = document.createElement('input'); input.type = 'text';
        input.value = Array.isArray(p.default) ? p.default.join(',') : (p.default ?? '');
      } else if (p.type === 'float' || p.type === 'int') {
        input = document.createElement('input'); input.type = 'number'; input.step = p.type === 'float' ? 'any' : '1';
        if (p.default !== null && p.default !== undefined) input.value = p.default;
      } else {
        input = document.createElement('input'); input.type = 'text'; input.value = p.default ?? '';
      }
      input.id = `strat_param_${p.name}`;
      input.dataset.paramName = p.name;
      input.dataset.paramType = p.type;
      row.appendChild(input);
      containerEl.appendChild(row);
    });
  }

  function applyParamsToForm(params, containerEl) {
    containerEl.querySelectorAll('[data-param-name]').forEach(input => {
      const name = input.dataset.paramName;
      if (!(name in params)) return;
      const val = params[name];
      const type = input.dataset.paramType;
      if (type === 'bool') { input.checked = !!val; return; }
      if (type === 'tuple') { input.value = Array.isArray(val) ? val.join(',') : (val ?? ''); return; }
      input.value = (val === null || val === undefined) ? '' : val;
    });
  }

  function collectParams(containerEl) {
    const out = {};
    containerEl.querySelectorAll('[data-param-name]').forEach(input => {
      const name = input.dataset.paramName, type = input.dataset.paramType;
      if (type === 'bool') { out[name] = input.checked; return; }
      if (type === 'tuple') { const v = input.value.trim(); out[name] = v ? v.split(',').map(s => s.trim()) : null; return; }
      if (type === 'float' || type === 'int') { const v = input.value.trim(); out[name] = v === '' ? null : Number(v); return; }
      out[name] = input.value.trim() || null;
    });
    return out;
  }

  // ---------------- mode toggle (built-in class vs custom rule builder) ----
  const STRAT_MODE_OPTS = [{ id: 'builtin', label: 'Built-in strategy' }, { id: 'custom', label: 'Custom rule builder' }];
  function setStratMode(mode) {
    builderMode = mode;
    renderPills(document.getElementById('stratModePills'), STRAT_MODE_OPTS, builderMode, setStratMode);
    document.getElementById('stratBuiltinForm').hidden = builderMode !== 'builtin';
    document.getElementById('stratBuilderForm').hidden = builderMode !== 'custom';
  }

  // ---------------- form panel (create / edit) ----------------
  function resetForm() {
    editingId = null;
    document.getElementById('stratFormTitle').textContent = 'New strategy config';
    document.getElementById('stratName').value = '';
    const sel = document.getElementById('stratClass');
    sel.selectedIndex = 0;
    renderParamForm(schemas[0], document.getElementById('stratParams'));
    resetBuilderState();
    setStratMode(builderMode); // keep whichever mode the user was already in
    document.getElementById('stratSave').textContent = 'Create';
    document.getElementById('stratCancelEdit').hidden = true;
  }

  function loadIntoForm(cfg) {
    editingId = cfg.id;
    document.getElementById('stratFormTitle').textContent = `Editing "${cfg.name}"`;
    document.getElementById('stratName').value = cfg.name;
    if (cfg.strategy_class === 'custom_rule') {
      setStratMode('custom');
      loadBuilderSpec(cfg.params.spec || {});
    } else {
      setStratMode('builtin');
      document.getElementById('stratClass').value = cfg.strategy_class;
      renderParamForm(schemas.find(s => s.id === cfg.strategy_class), document.getElementById('stratParams'));
      applyParamsToForm(cfg.params, document.getElementById('stratParams'));
    }
    document.getElementById('stratSave').textContent = 'Save changes';
    document.getElementById('stratCancelEdit').hidden = false;
    document.getElementById('stratName').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  async function saveForm() {
    const name = document.getElementById('stratName').value.trim();
    if (!name) { alert('Give this configuration a name.'); return; }

    let body;
    if (builderMode === 'custom') {
      const errEl = document.getElementById('builderError');
      errEl.hidden = true;
      const spec = collectBuilderSpec(name);
      if (!spec.entry_long && !spec.entry_short) {
        errEl.hidden = false; errEl.textContent = 'Add at least one entry condition (long or short).'; return;
      }
      body = { name, strategy_class: 'custom_rule', params: { spec } };
    } else {
      body = {
        name,
        strategy_class: document.getElementById('stratClass').value,
        params: collectParams(document.getElementById('stratParams')),
      };
    }

    const url = editingId ? `/api/strategies/${editingId}` : '/api/strategies';
    const method = editingId ? 'PUT' : 'POST';
    const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json();
    if (data.detail) {
      if (builderMode === 'custom') { document.getElementById('builderError').hidden = false; document.getElementById('builderError').textContent = data.detail; }
      else alert(data.detail);
      return;
    }
    await refreshConfigs();
    resetForm();
    refreshLibrary();
    if (window.BacktestView) window.BacktestView.refreshSavedConfigs();
  }

  // ---------------- saved config cards ----------------
  async function refreshConfigs() {
    const res = await fetch('/api/strategies');
    configs = await res.json();
    document.getElementById('stratCount').textContent = `${configs.length} saved`;
    renderCards();
  }

  function paramSummary(cfg) {
    if (cfg.strategy_class === 'custom_rule') {
      const spec = cfg.params.spec || {};
      const nInd = (spec.indicators || []).length;
      const nLong = spec.entry_long ? spec.entry_long.conditions.length : 0;
      const nShort = spec.entry_short ? spec.entry_short.conditions.length : 0;
      return `${nInd} indicator${nInd === 1 ? '' : 's'} · ${nLong} long / ${nShort} short condition${(nLong + nShort) === 1 ? '' : 's'}`;
    }
    const keys = Object.keys(cfg.params);
    if (!keys.length) return 'default parameters';
    return keys.slice(0, 3).map(k => `${k}=${cfg.params[k]}`).join(', ') + (keys.length > 3 ? '…' : '');
  }

  function renderCards() {
    const el = document.getElementById('stratList');
    if (!configs.length) {
      el.innerHTML = '<div class="bt-panel-note">No saved strategies yet — create one on the left, or save one from the Backtesting tab.</div>';
      return;
    }
    el.innerHTML = '';
    configs.forEach(cfg => {
      const card = document.createElement('div');
      card.className = 'strat-card';
      card.innerHTML = `
        <label class="strat-card-check">
          <input type="checkbox" data-id="${cfg.id}" ${selectedForCompare.has(cfg.id) ? 'checked' : ''}>
        </label>
        <div class="strat-card-body">
          <div class="strat-card-name">${cfg.name}</div>
          <div class="strat-card-class">${cfg.strategy_class === 'custom_rule' ? 'Custom (rule builder)' : cfg.strategy_class}</div>
          <div class="strat-card-params">${paramSummary(cfg)}</div>
        </div>
        <div class="strat-card-actions">
          <button type="button" data-act="edit" title="Edit">✎</button>
          <button type="button" data-act="dup" title="Duplicate">⧉</button>
          <button type="button" data-act="del" title="Delete" class="bt-danger-btn">✕</button>
        </div>
      `;
      card.querySelector('[data-act="edit"]').addEventListener('click', () => loadIntoForm(cfg));
      card.querySelector('[data-act="dup"]').addEventListener('click', async () => {
        await fetch(`/api/strategies/${cfg.id}/duplicate`, { method: 'POST' });
        await refreshConfigs();
        refreshLibrary();
        if (window.BacktestView) window.BacktestView.refreshSavedConfigs();
      });
      card.querySelector('[data-act="del"]').addEventListener('click', async () => {
        if (!confirm(`Delete "${cfg.name}"?`)) return;
        await fetch(`/api/strategies/${cfg.id}`, { method: 'DELETE' });
        selectedForCompare.delete(cfg.id);
        await refreshConfigs();
        refreshLibrary();
        if (window.BacktestView) window.BacktestView.refreshSavedConfigs();
      });
      card.querySelector('input[type="checkbox"]').addEventListener('change', (e) => {
        if (e.target.checked) selectedForCompare.add(cfg.id); else selectedForCompare.delete(cfg.id);
      });
      el.appendChild(card);
    });
  }

  // ---------------- comparison ----------------
  function statRow(label, key, fmtFn) {
    return { label, key, fmtFn };
  }
  const COMPARE_ROWS = [
    statRow('Trades', 'trades', v => fmt(v, 0)),
    statRow('Win Rate', 'win_rate_pct', v => fmt(v, 1) + '%'),
    statRow('Profit Factor', 'profit_factor', v => fmt(v, 2)),
    statRow('Expectancy (R)', 'expectancy_R', v => fmtSign(v, 3)),
    statRow('Max Drawdown', 'max_drawdown_pct', v => fmt(v, 1) + '%'),
    statRow('Sharpe', 'sharpe_daily_ann', v => fmt(v, 2)),
    statRow('Total Return', 'total_return_pct', v => fmtSign(v, 1) + '%'),
  ];

  async function runCompare() {
    const ids = Array.from(selectedForCompare);
    if (ids.length < 2) { alert('Select at least two saved strategies (checkboxes on the cards above) to compare.'); return; }
    const btn = document.getElementById('compareRun');
    btn.disabled = true; btn.textContent = 'Comparing…';
    try {
      const body = {
        config_ids: ids,
        timeframe: compareTf,
        session: compareSession,
        start: document.getElementById('compareFrom').value || null,
        end: document.getElementById('compareTo').value || null,
      };
      const res = await fetch('/api/strategies/compare', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const results = await res.json();
      renderCompareResults(results);
    } finally {
      btn.disabled = false; btn.textContent = 'Run Comparison';
    }
  }

  function renderCompareResults(results) {
    document.getElementById('compareEmpty').hidden = true;
    const box = document.getElementById('compareResults');
    box.hidden = false;

    if (!compareChart) compareChart = createMultiLineChart(document.getElementById('compareChart'));
    compareChart.clear();
    results.forEach((r, i) => {
      if (r.error) return;
      compareChart.addSeries(r.equity, SERIES_COLORS[i % SERIES_COLORS.length]);
    });
    compareChart.fitContent();

    const legendEl = document.getElementById('compareLegend');
    legendEl.innerHTML = results.map((r, i) => r.error ? '' : `
      <div class="legend-item"><span class="sw" style="background:${SERIES_COLORS[i % SERIES_COLORS.length]}"></span>${r.name}</div>
    `).join('');

    const tableEl = document.getElementById('compareTable');
    const head = `<thead><tr><th>Metric</th>${results.map(r => `<th class="num">${r.name || ('#' + r.id)}</th>`).join('')}</tr></thead>`;
    const body = COMPARE_ROWS.map(row => `<tr>
      <td>${row.label}</td>
      ${results.map(r => `<td class="num">${r.error ? '—' : row.fmtFn(r.stats[row.key])}</td>`).join('')}
    </tr>`).join('');
    const errorRow = results.some(r => r.error)
      ? `<tr><td>Errors</td>${results.map(r => `<td class="num" style="color:var(--bad)">${r.error || ''}</td>`).join('')}</tr>`
      : '';
    tableEl.innerHTML = head + `<tbody>${body}${errorRow}</tbody>`;
  }

  // ---------------- Strategy Library ----------------
  const LIBRARY_SORTS = [
    { id: 'total_return_pct', label: 'Highest Return' },
    { id: 'win_rate_pct', label: 'Highest Win Rate' },
    { id: 'max_drawdown_pct', label: 'Lowest Drawdown' },
    { id: 'profit_factor', label: 'Highest Profit Factor' },
    { id: 'trades', label: 'Most Trades' },
  ];
  let librarySort = 'total_return_pct';
  let libraryRows = [];

  function pickLibrarySort(id) {
    librarySort = id;
    renderPills(document.getElementById('librarySortPills'), LIBRARY_SORTS, librarySort, pickLibrarySort);
    renderLibraryTable();
  }

  async function refreshLibrary() {
    const emptyEl = document.getElementById('libraryEmpty');
    const wrap = document.getElementById('libraryTableWrap');
    if (!configs.length) {
      emptyEl.hidden = false; emptyEl.textContent = 'No saved strategies yet.';
      wrap.hidden = true;
      document.getElementById('libraryCount').textContent = '';
      return;
    }
    document.getElementById('libraryCount').textContent = 'running backtests…';
    // no explicit timeframe here — each strategy runs at its OWN declared
    // working timeframe (Strategy.timeframe / spec.timeframe) instead of
    // being forced onto one bar size for the ranking, same principle as
    // live/auto-trade now honoring each strategy's own choice.
    const res = await fetch('/api/strategies/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config_ids: configs.map(c => c.id), session: 'all' }),
    });
    libraryRows = await res.json();
    document.getElementById('libraryCount').textContent = `${libraryRows.length} strategies · trailing 1 year`;
    renderLibraryTable();
  }

  function renderLibraryTable() {
    const emptyEl = document.getElementById('libraryEmpty');
    const wrap = document.getElementById('libraryTableWrap');
    if (!libraryRows.length) { emptyEl.hidden = false; emptyEl.textContent = 'No saved strategies yet.'; wrap.hidden = true; return; }
    emptyEl.hidden = true; wrap.hidden = false;

    const ok = libraryRows.filter(r => !r.error && r.stats && r.stats.trades);
    const flat = libraryRows.filter(r => !r.error && (!r.stats || !r.stats.trades));
    const bad = libraryRows.filter(r => r.error);
    ok.sort((a, b) => (b.stats[librarySort] ?? -Infinity) - (a.stats[librarySort] ?? -Infinity));

    const head = `<thead><tr>
      <th>Strategy</th><th>Type</th><th>Timeframe</th><th class="num">Trades</th><th class="num">Win Rate</th>
      <th class="num">Return %</th><th class="num">Max DD</th>
      <th class="num">Profit Factor</th><th class="num">Sharpe</th><th></th>
    </tr></thead>`;
    const typeLabel = (r) => r.strategy_class === 'custom_rule' ? 'Custom' : r.strategy_class;
    const editBtn = (r) => `<button type="button" class="dash-mini-action" data-edit="${r.id}">Edit</button>`;
    const okRows = ok.map(r => `<tr>
      <td>${r.name}</td>
      <td>${typeLabel(r)}</td>
      <td>${r.timeframe || ''}</td>
      <td class="num">${fmt(r.stats.trades, 0)}</td>
      <td class="num">${fmt(r.stats.win_rate_pct, 1)}%</td>
      <td class="num ${r.stats.total_return_pct >= 0 ? 'pos' : 'neg'}">${fmtSign(r.stats.total_return_pct, 1)}%</td>
      <td class="num neg">${fmt(r.stats.max_drawdown_pct, 1)}%</td>
      <td class="num">${fmt(r.stats.profit_factor, 2)}</td>
      <td class="num">${fmt(r.stats.sharpe_daily_ann, 2)}</td>
      <td>${editBtn(r)}</td>
    </tr>`).join('');
    const flatRows = flat.map(r => `<tr>
      <td>${r.name}</td><td>${typeLabel(r)}</td><td>${r.timeframe || ''}</td>
      <td colspan="6" class="bt-panel-note">no trades generated over this window</td>
      <td>${editBtn(r)}</td>
    </tr>`).join('');
    const errRows = bad.map(r => `<tr>
      <td>${r.name || ('#' + r.id)}</td><td colspan="8" style="color:var(--bad)">${r.error}</td><td>${editBtn(r)}</td>
    </tr>`).join('');

    const tableEl = document.getElementById('libraryTable');
    tableEl.innerHTML = head + `<tbody>${okRows}${flatRows}${errRows}</tbody>`;
    tableEl.querySelectorAll('[data-edit]').forEach(btn => {
      btn.addEventListener('click', () => {
        const cfg = configs.find(c => c.id === Number(btn.dataset.edit));
        if (cfg) loadIntoForm(cfg);
      });
    });
  }

  // ---------------- init ----------------
  async function init() {
    const [metaRes, stratRes, cfgRes, indRes] = await Promise.all([
      fetch('/api/market/meta'), fetch('/api/strategies/classes'), fetch('/api/strategies'), fetch('/api/strategies/indicators'),
    ]);
    marketMeta = await metaRes.json();
    schemas = await stratRes.json();
    configs = await cfgRes.json();
    const indData = await indRes.json();
    indicatorCatalog = indData.indicators;
    indicatorCatalogMap = Object.fromEntries(indicatorCatalog.map(c => [c.id, c]));
    priceFields = indData.price_fields;
    operators = indData.operators;

    const sel = document.getElementById('stratClass');
    sel.innerHTML = '';
    schemas.forEach(s => {
      const opt = document.createElement('option'); opt.value = s.id; opt.textContent = s.label;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', () => renderParamForm(schemas.find(s => s.id === sel.value), document.getElementById('stratParams')));
    renderParamForm(schemas[0], document.getElementById('stratParams'));

    // strategy-type mode toggle + builder wiring
    const addIndSel = document.getElementById('builderAddIndicator');
    addIndSel.innerHTML = indicatorCatalog.map(c => `<option value="${c.id}">${c.label}</option>`).join('');
    document.getElementById('builderAddIndicatorBtn').addEventListener('click', addBuilderIndicator);
    document.getElementById('builderAddLongCond').addEventListener('click', () => addBuilderCondition('long'));
    document.getElementById('builderAddShortCond').addEventListener('click', () => addBuilderCondition('short'));
    setStratMode('builtin');
    resetBuilderState();

    document.getElementById('stratSave').addEventListener('click', saveForm);
    document.getElementById('stratCancelEdit').addEventListener('click', resetForm);

    document.getElementById('stratCount').textContent = `${configs.length} saved`;
    renderCards();

    const startDate = marketMeta.start.slice(0, 10), endDate = marketMeta.end.slice(0, 10);
    const fromEl = document.getElementById('compareFrom'), toEl = document.getElementById('compareTo');
    fromEl.min = startDate; fromEl.max = endDate; fromEl.value = startDate;
    toEl.min = startDate; toEl.max = endDate; toEl.value = endDate;

    function pickTf(id) { compareTf = id; renderPills(document.getElementById('compareTfPills'), TIMEFRAMES, compareTf, pickTf); }
    renderPills(document.getElementById('compareTfPills'), TIMEFRAMES, compareTf, pickTf);
    function pickSession(id) { compareSession = id; renderPills(document.getElementById('compareSessionPills'), SESSIONS, compareSession, pickSession); }
    renderPills(document.getElementById('compareSessionPills'), SESSIONS, compareSession, pickSession);

    document.getElementById('compareRun').addEventListener('click', runCompare);

    renderPills(document.getElementById('librarySortPills'), LIBRARY_SORTS, librarySort, pickLibrarySort);
    document.getElementById('libraryRefresh').addEventListener('click', refreshLibrary);
    refreshLibrary();
  }

  function onShow() {
    if (initialized) { refreshConfigs(); return; }
    initialized = true;
    init();
  }

  return { onShow };
})();
