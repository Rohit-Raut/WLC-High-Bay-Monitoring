// features/dashboard/chart_interactions.js
//
// Chart interaction logic for the WLC High Bay monitoring dashboard.
// Embedded verbatim by particle_plus.py::generate_dashboard_html() immediately
// after the data-constants block (TS, COUNTS, PM, DIST, LIVE_TS, etc.).
//
// Key fixes vs. the original inline JS:
//   1. Y axis on particle-count chart is FIXED (autorange: false, fixedrange: true)
//      so ISO reference lines always stay visible and the chart never disappears
//      when the time-range dropdown changes.
//   2. +/- modebar buttons and scroll-zoom only affect the X (time) axis because
//      yaxis.fixedrange: true prevents them from rescaling Y.
//   3. PM chart Y is anchored at zero (rangemode: 'tozero') with a stable max
//      computed from the full dataset at page load.
//   4. scrollZoom: true added to PLOTLY_CFG so trackpad / mouse-wheel zooms
//      the time axis smoothly.

// ── Stable Y ranges — computed ONCE from the full dataset at page load ────────
// Using all data (not the current time slice) means the range never jumps when
// the dropdown changes or filterAndRender() is called again.

// Particle counts (log scale).
// Floor: COUNTS_Y_MAX >= 8.0 ensures ISO 5-9 reference lines (3520–35.2M/m³)
// are always in view.  Ceiling expands automatically if the data exceeds 10^8.
const _allCountVals = COUNTS.flatMap(tr => tr.y).filter(v => v !== null && v > 0);
const _rawCountMax  = _allCountVals.length ? Math.max(..._allCountVals) : 1e6;
const COUNTS_Y_RANGE = [1.5, Math.max(Math.log10(_rawCountMax) + 0.5, 8.0)];

// PM mass (linear scale).  20 % headroom above the dataset max; floor at 5 µg/m³.
const _allPMVals = PM.flatMap(tr => tr.y).filter(v => v !== null && v >= 0);
const _rawPMMax  = _allPMVals.length ? Math.max(..._allPMVals) : 10;
const PM_Y_MAX   = Math.max(_rawPMMax * 1.2, 5);

// ── Layout base shared by all charts ─────────────────────────────────────────
const DARK = {
  paper_bgcolor: '#0f172a',
  plot_bgcolor:  '#0f172a',
  font:      { color: '#9ca3af', family: 'Courier New, monospace', size: 11 },
  margin:    { l: 60, r: 20, t: 30, b: 50 },
  hovermode: 'x unified',
  hoverlabel: { bgcolor: '#1e293b', bordercolor: '#334155', font: { size: 11 } },
  legend: { bgcolor: 'rgba(0,0,0,0)', bordercolor: '#334155', borderwidth: 1,
            font: { size: 11 }, orientation: 'h', yanchor: 'bottom', y: 1.02, x: 0 },
  xaxis: { gridcolor: '#1e293b', linecolor: '#334155', zerolinecolor: '#1e293b',
           tickfont: { color: '#6b7280', size: 10 },
           title_font: { color: '#6b7280', size: 11 } },
  yaxis: { gridcolor: '#1e293b', linecolor: '#334155', zerolinecolor: '#1e293b',
           tickfont: { color: '#6b7280', size: 10 },
           title_font: { color: '#6b7280', size: 11 } },
};

// ── Plotly config shared across all charts ────────────────────────────────────
// scrollZoom: true  lets the user zoom in/out on the time axis with the
//             trackpad or mouse wheel without touching the toolbar.
// select2d / lasso2d are removed — they are not useful on time-series data.
const PLOTLY_CFG = {
  responsive:              true,
  displaylogo:             false,
  scrollZoom:              true,
  modeBarButtonsToRemove:  ['select2d', 'lasso2d'],
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function sliceIdx(mins) {
  if (!mins || TS.length === 0) return 0;
  const cut = new Date(new Date(TS[TS.length - 1]) - mins * 60000);
  const i   = TS.findIndex(t => new Date(t) >= cut);
  return i < 0 ? TS.length - 1 : i;
}

function sliceTraces(traces, i) {
  return traces.map(tr => Object.assign({}, tr, {
    x: tr.x.slice(i),
    y: tr.y.slice(i),
  }));
}

// Returns vrect shapes for gaps > GAP_THRESH_MS in a timestamp array.
// Rendered as subtle grey bands indicating the counter was offline.
function gapShapes(ts) {
  const GAP_THRESH_MS = 90 * 60 * 1000;   // 90 minutes
  const shapes = [];
  for (let k = 1; k < ts.length; k++) {
    if (new Date(ts[k]) - new Date(ts[k - 1]) > GAP_THRESH_MS) {
      shapes.push({
        type: 'rect', xref: 'x', yref: 'paper',
        x0: ts[k - 1], x1: ts[k], y0: 0, y1: 1,
        fillcolor: 'rgba(100,116,139,0.10)', line: { width: 0 }, layer: 'below',
      });
    }
  }
  return shapes;
}

// ISO 14644-1 reference lines (horizontal dashed) for the 0.5 µm channel.
function isoShapes() {
  return ISO_LINES.map(l => ({
    type: 'line', xref: 'paper', x0: 0, x1: 1,
    yref: 'y', y0: l.y, y1: l.y,
    line: { color: l.color, width: l.width, dash: l.dash },
  }));
}
function isoAnnotations() {
  return ISO_LINES.map(l => ({
    xref: 'paper', x: 1.02, yref: 'y', y: l.y,
    text: l.bold ? '<b>' + l.label + '</b>' : l.label,
    showarrow: false, xanchor: 'left',
    font: { color: l.color, size: l.bold ? 12 : 10, family: 'Courier New, monospace' },
  }));
}

function updateStats(i) {
  const ts    = TS.slice(i);
  const ch1   = COUNTS[0].y.slice(i).filter(v => v !== null && v !== undefined);
  const ch2   = COUNTS[1].y.slice(i).filter(v => v !== null && v !== undefined);
  const n     = ts.length;
  const fmt   = v => (v !== null && !isNaN(v))
    ? Math.round(v).toLocaleString() + ' /m³' : '--';
  const mean1 = ch1.length ? ch1.reduce((a, b) => a + b, 0) / ch1.length : null;
  const peak1 = ch1.length ? Math.max(...ch1) : null;
  const exc7  = ch2.filter(v => v > 352000).length;
  const exc7s = ch2.length
    ? exc7 + ' / ' + ch2.length + ' ('
      + (exc7 / ch2.length * 100).toFixed(0) + '%)'
    : '--';
  const gaps = gapShapes(ts).length;

  document.getElementById('stat-n').textContent     = n;
  document.getElementById('stat-mean1').textContent = fmt(mean1);
  document.getElementById('stat-peak1').textContent = fmt(peak1);

  const excEl = document.getElementById('stat-exc7');
  excEl.textContent = exc7s;
  excEl.className   = 'stat-v' + (exc7 > 0 ? ' warn' : '');

  const gapEl = document.getElementById('stat-gaps');
  gapEl.textContent = gaps > 0 ? gaps + (gaps === 1 ? ' gap' : ' gaps') : 'none';
  gapEl.className   = 'stat-v' + (gaps > 0 ? ' warn' : '');
}

// ── Main render function ──────────────────────────────────────────────────────
function filterAndRender() {
  const mins = parseInt(document.getElementById('sel-range').value);
  const i    = sliceIdx(mins);
  const ts   = TS.slice(i);
  const gaps = gapShapes(ts);

  // ── Particle count chart (log scale) ──────────────────────────────────────
  // fixedrange: true on Y  →  +/- modebar buttons, scroll-zoom, and box-zoom
  //   only move/scale the X (time) axis.  Y stays pinned to COUNTS_Y_RANGE so
  //   all ISO reference lines remain visible regardless of which time window is
  //   selected or how far the user zooms in on the time axis.
  // autorange: false + range  →  Y never re-scales when filterAndRender() fires
  //   (e.g. dropdown change), so the chart never "disappears" into a tiny range.
  Plotly.react('chart-counts', sliceTraces(COUNTS, i),
    Object.assign({}, DARK, {
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'Counts / m³',
        type:       'log',
        autorange:  false,
        range:      COUNTS_Y_RANGE,
        fixedrange: true,
      }),
      xaxis:       Object.assign({}, DARK.xaxis, { title: '' }),
      margin:      { l: 60, r: 72, t: 30, b: 50 },
      shapes:      [...gaps, ...isoShapes()],
      annotations: isoAnnotations(),
    }), PLOTLY_CFG);

  // ── PM mass chart (linear scale) ──────────────────────────────────────────
  // fixedrange: true prevents Y from zooming so the chart stays readable.
  // rangemode: 'tozero' + fixed range keeps the baseline at 0 across all
  // time-window selections and prevents the axis from inverting or collapsing.
  Plotly.react('chart-pm', sliceTraces(PM, i),
    Object.assign({}, DARK, {
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'μg / m³',
        rangemode:  'tozero',
        range:      [0, PM_Y_MAX],
        autorange:  false,
        fixedrange: true,
      }),
      xaxis:  Object.assign({}, DARK.xaxis, { title: '' }),
      shapes: gaps,
    }), PLOTLY_CFG);

  // ── Size distribution bar chart ───────────────────────────────────────────
  // Y is fixed to the data max (computed from latest snapshot, not time slice).
  // Both axes are fixedrange so the bar chart is not accidentally panned.
  const _distMax    = (DIST[0] && DIST[0].y.length) ? Math.max(...DIST[0].y) : 100;
  const _distLogMax = Math.log10(Math.max(_distMax, 1)) + 0.3;
  Plotly.react('chart-dist', DIST,
    Object.assign({}, DARK, {
      showlegend: false,
      bargap:     0.3,
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'Counts / m³',
        type:       'log',
        range:      [-0.5, _distLogMax],
        autorange:  false,
        fixedrange: true,
      }),
      xaxis: Object.assign({}, DARK.xaxis, {
        title:      'Particle Size (μm)',
        fixedrange: true,
      }),
    }), PLOTLY_CFG);

  // ── Environment chart (temperature + humidity) ────────────────────────────
  // Y axes are NOT fixedrange here — temp and RH benefit from autorange because
  // their absolute values are meaningful and vary within a known human-comfort
  // range.  The user can zoom/pan to inspect fine fluctuations.
  const livei = (LIVE_TS.length === 0 || !mins) ? 0 : (() => {
    const cut = new Date(new Date(LIVE_TS[LIVE_TS.length - 1]) - mins * 60000);
    const j   = LIVE_TS.findIndex(t => new Date(t) >= cut);
    return j < 0 ? LIVE_TS.length - 1 : j;
  })();
  Plotly.react('chart-env', [
    { x: LIVE_TS.slice(livei), y: TEMP_F.slice(livei), name: 'Temperature (°F)',
      type: 'scatter', mode: 'lines',
      line: { color: '#ff6b6b', width: 2 }, yaxis: 'y' },
    { x: LIVE_TS.slice(livei), y: RH_VALS.slice(livei), name: 'Humidity (%)',
      type: 'scatter', mode: 'lines',
      line: { color: '#4ecdc4', width: 2 }, yaxis: 'y2' },
  ], Object.assign({}, DARK, {
    margin: { l: 60, r: 70, t: 30, b: 50 },
    xaxis:  Object.assign({}, DARK.xaxis, { title: '' }),
    yaxis:  Object.assign({}, DARK.yaxis, { title: 'Temperature (°F)' }),
    yaxis2: {
      title:      { text: 'Humidity (%)', standoff: 15 },
      overlaying: 'y', side: 'right',
      gridcolor:  '#1e293b', linecolor: '#334155',
      tickfont:   { color: '#6b7280', size: 10 },
      title_font: { color: '#6b7280', size: 11 },
    },
  }), PLOTLY_CFG);

  updateStats(i);
}

// Initial render
filterAndRender();

// Close notification dropdown when clicking outside it
document.addEventListener('click', function(e) {
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open') && !drop.parentElement.contains(e.target)) {
    drop.classList.remove('open');
  }
});
