// Factory around lightweight-charts: candles + entry/exit markers + SL/TP
// level lines. Each caller gets its own independent instance (Charts view and
// Backtest view each have their own chart, on different timeframes/ranges).

function createTerminalChart(container, opts) {
  opts = opts || {};
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: 'transparent' },
      textColor: '#97a1ad',
      fontFamily: 'IBM Plex Mono, ui-monospace, monospace',
      fontSize: 11,
    },
    grid: {
      vertLines: { color: 'rgba(255,255,255,0.05)' },
      horzLines: { color: 'rgba(255,255,255,0.05)' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)' },
    timeScale: { borderColor: 'rgba(255,255,255,0.08)', timeVisible: true, secondsVisible: false },
    autoSize: true,
    height: opts.height,
  });
  const candleSeries = chart.addCandlestickSeries({
    upColor: '#3ecb3e', downColor: '#e4685f',
    borderUpColor: '#3ecb3e', borderDownColor: '#e4685f',
    wickUpColor: '#3ecb3e', wickDownColor: '#e4685f',
  });

  let priceLines = [];

  function setData(bars) {
    candleSeries.setData(bars.map(b => ({
      time: b.time, open: b.open, high: b.high, low: b.low, close: b.close,
    })));
  }

  function update(bar) {
    // updates the last candle in place, or appends a new one — exactly the
    // semantics a live feed tick needs (lightweight-charts requires this to
    // be called only for the newest bar or later, same as setData ordering)
    candleSeries.update({ time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close });
  }

  function setMarkers(markers) {
    candleSeries.setMarkers(markers || []);
  }

  function clearLevelLines() {
    priceLines.forEach(pl => candleSeries.removePriceLine(pl));
    priceLines = [];
  }

  function addLevelLine(price, color, title) {
    if (price === null || price === undefined || !isFinite(price)) return null;
    const pl = candleSeries.createPriceLine({
      price, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true, title,
    });
    priceLines.push(pl);
    return pl;
  }

  function onCrosshairMove(cb) { chart.subscribeCrosshairMove(cb); }
  function fitContent() { chart.timeScale().fitContent(); }
  function setVisibleRange(fromSec, toSec) {
    chart.timeScale().setVisibleRange({ from: fromSec, to: toSec });
  }
  // keeps the newest bar in view as live ticks arrive, without resetting
  // zoom the way fitContent() would on every single tick
  function scrollToRealTime() { chart.timeScale().scrollToRealTime(); }

  // named overlay lines (EMA, PDH/PDL, swing levels, ...) drawn directly on
  // the price pane — a running auto-trade strategy's own indicators, added
  // and removed by name so re-drawing each poll doesn't pile up duplicates
  let overlaySeries = {};
  function setOverlayLine(name, points, color) {
    if (!points || !points.length) return;
    if (!overlaySeries[name]) {
      // dashed + thin: these are reference levels (EMA, PDH/PDL, swing
      // highs/lows), not the primary series — they should read as
      // background context behind the candles, not compete with them
      overlaySeries[name] = chart.addLineSeries({
        color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
    }
    overlaySeries[name].setData(points.map(([t, v]) => ({ time: t, value: v })));
  }
  function clearOverlays(keepNames) {
    const keep = new Set(keepNames || []);
    Object.keys(overlaySeries).forEach(name => {
      if (keep.has(name)) return;
      chart.removeSeries(overlaySeries[name]);
      delete overlaySeries[name];
    });
  }

  return {
    chart, candleSeries, setData, update, setMarkers, clearLevelLines, addLevelLine,
    onCrosshairMove, fitContent, setVisibleRange, scrollToRealTime,
    setOverlayLine, clearOverlays,
  };
}

// small line/area chart for equity + drawdown curves
function createLineChart(container, opts) {
  opts = opts || {};
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: 'transparent' },
      textColor: '#97a1ad',
      fontFamily: 'IBM Plex Mono, ui-monospace, monospace',
      fontSize: 10.5,
    },
    grid: {
      vertLines: { color: 'rgba(255,255,255,0.05)' },
      horzLines: { color: 'rgba(255,255,255,0.05)' },
    },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)' },
    timeScale: { borderColor: 'rgba(255,255,255,0.08)', timeVisible: true, secondsVisible: false },
    autoSize: true,
    height: opts.height,
  });
  const series = opts.area
    ? chart.addAreaSeries({
        lineColor: opts.color || '#3987e5',
        topColor: (opts.color || '#3987e5') + '33',
        bottomColor: (opts.color || '#3987e5') + '00',
        lineWidth: 2,
      })
    : chart.addLineSeries({ color: opts.color || '#3987e5', lineWidth: 2 });

  function setData(points) {
    // points: [[isoString, value], ...]
    series.setData(points.map(([t, v]) => ({ time: Math.floor(new Date(t).getTime() / 1000), value: v })));
  }
  function fitContent() { chart.timeScale().fitContent(); }

  return { chart, series, setData, fitContent };
}

// multiple named line series overlaid on one chart — used by the strategy
// comparison dashboard (one equity curve per selected config)
function createMultiLineChart(container, opts) {
  opts = opts || {};
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: 'transparent' },
      textColor: '#97a1ad',
      fontFamily: 'IBM Plex Mono, ui-monospace, monospace',
      fontSize: 10.5,
    },
    grid: {
      vertLines: { color: 'rgba(255,255,255,0.05)' },
      horzLines: { color: 'rgba(255,255,255,0.05)' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)' },
    timeScale: { borderColor: 'rgba(255,255,255,0.08)', timeVisible: true, secondsVisible: false },
    autoSize: true,
    height: opts.height,
  });
  let seriesList = [];

  function addSeries(points, color) {
    // lastValueVisible/priceLineVisible off: with several series sharing
    // one price scale, their floating last-value labels stack on top of
    // each other on the right axis and become unreadable — the color-coded
    // legend callers already render is how you tell series apart instead
    const s = chart.addLineSeries({ color, lineWidth: 2, lastValueVisible: false, priceLineVisible: false });
    s.setData(points.map(([t, v]) => ({ time: Math.floor(new Date(t).getTime() / 1000), value: v })));
    seriesList.push(s);
    return s;
  }
  function clear() {
    seriesList.forEach(s => chart.removeSeries(s));
    seriesList = [];
  }
  function fitContent() { chart.timeScale().fitContent(); }

  return { chart, addSeries, clear, fitContent };
}
