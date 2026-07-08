// features/dashboard/chart_interactions_local.js
//
// LOCAL-ONLY Chart interaction logic for the WLC High Bay monitoring dashboard.
// Includes extended features: custom time range selector, dynamic PM log scale.
// Embedded verbatim by particle_plus.py::generate_dashboard_html() immediately
// after the data-constants block (TS, COUNTS, PM, DIST, LIVE_TS, etc.) when local=True.
//
// Zoom model: discrete step-based.
//   Scroll wheel / trackpad steps the dropdown through 6 fixed time windows:
//     30 min → 1 hr → 6 hr → 12 hr → 24 hr → 7 days
//   Scroll up  (deltaY < 0) = zoom in  → smaller time window (more detail).
//   Scroll down (deltaY > 0) = zoom out → larger time window (less detail).
//   Hard stops: 30 min (can't zoom in further), 7 days (can't zoom out further).
//   Each step calls filterAndRender() which resets all charts to the exact
//   selected window — no pixel-level drift, no out-of-bounds issues.

// ── Stable Y ranges — computed ONCE from the full dataset at page load ────────
// Using all data (not the current time slice) means Y never jumps when the
// dropdown changes or filterAndRender() is called again.

// Particle counts (log scale).
// Ceiling >= 8.0 keeps ISO 5–9 reference lines (3 520 – 35.2 M /m³) in view.
const _allCountVals = COUNTS.flatMap(tr => tr.y).filter(v => v !== null && v > 0);
const _rawCountMax  = _allCountVals.length ? Math.max(..._allCountVals) : 1e6;
const COUNTS_Y_RANGE = [1.5, Math.max(Math.log10(_rawCountMax) + 0.5, 8.0)];

// PM mass — no longer using global PM_Y_MAX (dynamic log scale in local version)
// Kept for backwards compatibility but unused in filterAndRender below

// Distributed Shelly sensors share the env chart axes — include their values
// so no trace clips. ENV_SENSORS is embedded by the generator ([] when the
// sensor csv is absent); the typeof guard covers stale generated pages.
const _SENS = (typeof ENV_SENSORS !== 'undefined' && Array.isArray(ENV_SENSORS))
  ? ENV_SENSORS : [];

// Temperature (°F).  ±5 °F padding around observed range; floor at 32 °F.
const _tempVals  = TEMP_F.concat(..._SENS.map(s => s.temp))
  .filter(v => v !== null && !isNaN(v));
const TEMP_Y_RANGE = _tempVals.length
  ? [Math.max(32,  Math.min(..._tempVals) - 5), Math.max(..._tempVals) + 5]
  : [60, 90];

// Relative humidity (%).  ±5 % padding; clamped to [0, 100].
const _rhVals   = RH_VALS.concat(..._SENS.map(s => s.rh))
  .filter(v => v !== null && !isNaN(v));
const RH_Y_RANGE = _rhVals.length
  ? [Math.max(0,   Math.min(..._rhVals) - 5), Math.min(100, Math.max(..._rhVals) + 5)]
  : [0, 100];

// ── Theme-aware Plotly layout ─────────────────────────────────────────────────
// All four chart divs — iterated when the theme toggles.
const CHART_IDS = ['chart-counts', 'chart-pm', 'chart-dist', 'chart-env'];

function _isLightTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light';
}

// Plotly layout patch matching the current CSS theme (values mirror the
// :root / [data-theme="light"] variables in the generated <style> block).
function getPlotlyTheme() {
  const isLight = _isLightTheme();
  return {
    paper_bgcolor: isLight ? '#ffffff' : '#0d1117',
    plot_bgcolor:  isLight ? '#ffffff' : '#0d1117',
    font: {
      color: isLight ? '#1f2328' : '#e6edf3',
      family: 'Arial, "Helvetica Neue", Helvetica, sans-serif',
      size: 12
    },
    xaxis: {
      gridcolor:     isLight ? '#e8ecf0' : '#21262d',
      linecolor:     isLight ? '#d0d7de' : '#30363d',
      tickcolor:     isLight ? '#656d76' : '#8b949e',
      zerolinecolor: isLight ? '#d0d7de' : '#30363d',
    },
    yaxis: {
      gridcolor:     isLight ? '#e8ecf0' : '#21262d',
      linecolor:     isLight ? '#d0d7de' : '#30363d',
      tickcolor:     isLight ? '#656d76' : '#8b949e',
      zerolinecolor: isLight ? '#d0d7de' : '#30363d',
    },
    legend: {
      bgcolor:     isLight ? 'rgba(240,243,246,0.85)' : 'rgba(22,27,34,0.85)',
      bordercolor: isLight ? '#d0d7de' : '#30363d',
    },
    hoverlabel: {
      bgcolor:     isLight ? '#ffffff' : '#161b22',
      bordercolor: isLight ? '#d0d7de' : '#30363d',
      font: { color: isLight ? '#1f2328' : '#e6edf3' }
    }
  };
}

// Secondary-text and border colors per theme (axis ticks, bar value labels).
function _themeMuted()  { return _isLightTheme() ? '#656d76' : '#8b949e'; }
function _themeBorder() { return _isLightTheme() ? '#d0d7de' : '#30363d'; }

// ── Per-theme trace palettes ─────────────────────────────────────────────────
// Dark mode keeps the Wong colorblind-safe palette baked in by the generator.
// On a white background those hues wash out (sky blue and amber especially),
// so light mode remaps each channel to a darker color in the SAME hue family
// (all ~4:1+ contrast on #ffffff) — the legend mapping stays intuitive when
// switching themes.
const TRACE_DARK  = ['#0072B2', '#E69F00', '#009E73', '#D55E00', '#56B4E9', '#CC79A7'];
const TRACE_LIGHT = ['#0550AE', '#9A6700', '#1A7F37', '#BC4C00', '#0E7490', '#BF3989'];

function _traceColor(c) {
  if (!_isLightTheme() || !c) return c;
  const i = TRACE_DARK.indexOf(String(c).toUpperCase());
  return i >= 0 ? TRACE_LIGHT[i] : c;
}

// 'rgba(r,g,b,a)' from a '#rrggbb' hex — used for error-bar colors.
function _rgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
}

// Re-color an array of generator-built traces for the active theme.
function _themedTraces(traces) {
  if (!_isLightTheme()) return traces;
  return traces.map(tr => {
    const out = Object.assign({}, tr);
    if (out.line)   out.line   = Object.assign({}, out.line,   { color: _traceColor(out.line.color) });
    if (out.marker) out.marker = Object.assign({}, out.marker, { color: _traceColor(out.marker.color) });
    return out;
  });
}

// Original bar colors of the size-distribution chart (its marker.color is an
// array, one color per channel) — kept so re-renders can re-theme from source.
const DIST_BASE_COLORS = (DIST[0] && DIST[0].marker && Array.isArray(DIST[0].marker.color))
  ? DIST[0].marker.color.slice() : null;

// The ISO 14644-1 reference-line greens are tuned for the dark background;
// on white the lighter grades (ISO 5/6) are nearly invisible, so light mode
// substitutes a darker green ramp (same ordering, ISO 6 still boldest).
const ISO_LIGHT_COLORS = {
  '#81c784': '#5a9e60',   // ISO 5
  '#2ecc71': '#1a7f37',   // ISO 6 (bold)
  '#27ae60': '#176d33',   // ISO 7
  '#1e8449': '#115226',   // ISO 8
  '#115f2e': '#0b3d1e',   // ISO 9
};
function _isoColor(c) {
  if (!_isLightTheme()) return c;
  return ISO_LIGHT_COLORS[String(c).toLowerCase()] || c;
}

// Full base layout shared by all charts: current theme colors + the static
// sizing/behaviour options. Rebuilt on every render, so a theme toggle only
// needs to call filterAndRender() to re-skin everything.
function _baseLayout() {
  const t = getPlotlyTheme();
  return {
    paper_bgcolor: t.paper_bgcolor,
    plot_bgcolor:  t.plot_bgcolor,
    font:          t.font,
    margin:        { l: 60, r: 20, t: 30, b: 50 },
    hovermode:     'x unified',
    hoverlabel: Object.assign({}, t.hoverlabel, {
      font: Object.assign({ size: 11 }, t.hoverlabel.font),
    }),
    legend: Object.assign({}, t.legend, {
      borderwidth: 1,
      font: { size: 12.5, family: 'Arial, "Helvetica Neue", Helvetica, sans-serif' },
      orientation: 'h', yanchor: 'bottom', y: 1.02, x: 0,
    }),
    xaxis: Object.assign({}, t.xaxis, {
      tickfont:   { color: _themeMuted(), size: 12, family: 'Arial, "Helvetica Neue", Helvetica, sans-serif' },
      title_font: { color: _themeMuted(), size: 13.5, family: 'Arial, "Helvetica Neue", Helvetica, sans-serif' },
    }),
    yaxis: Object.assign({}, t.yaxis, {
      tickfont:   { color: _themeMuted(), size: 12, family: 'Arial, "Helvetica Neue", Helvetica, sans-serif' },
      title_font: { color: _themeMuted(), size: 13.5, family: 'Arial, "Helvetica Neue", Helvetica, sans-serif' },
    }),
  };
}

// ── Plotly config shared across all charts ────────────────────────────────────
// scrollZoom: false — native Plotly scroll zoom disabled; handled by
// _attachWheelListeners() instead so scroll steps the dropdown discretely.
const PLOTLY_CFG = {
  responsive:             true,
  displaylogo:            false,
  scrollZoom:             false,
  modeBarButtonsToRemove: ['select2d', 'lasso2d'],
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function sliceIdxForArray(tsArray, mins) {
  if (!mins || tsArray.length === 0) return 0;
  const cut = new Date(_parseDate(tsArray[tsArray.length - 1]).getTime() - mins * 60000);
  const i   = tsArray.findIndex(t => _parseDate(t) >= cut);
  return i < 0 ? tsArray.length - 1 : i;
}

function sliceIdx(mins) {
  return sliceIdxForArray(TS, mins);
}

// ── Env-chart helpers for the distributed Shelly sensors ─────────────────────
// The x window must span the counter AND every sensor series — anchoring to
// LIVE_TS alone would clip sensor reports newer than the last counter sample
// (e.g., counter down while the Shellys keep reporting).
function envTimeSpan(mins) {
  const firsts = [], lasts = [];
  ENV_SITES.forEach(function (s) {
    if (s.ts.length) { firsts.push(s.ts[0]); lasts.push(s.ts[s.ts.length - 1]); }
  });
  if (!lasts.length) return null;
  const right    = lasts.reduce((a, b) => _parseDate(a) > _parseDate(b) ? a : b);
  const earliest = firsts.reduce((a, b) => _parseDate(a) < _parseDate(b) ? a : b);
  // clamp the window's left edge to where env data actually begins — a 7-day
  // window over 8 h of sensor history would otherwise be mostly empty space
  const left = mins
    ? _toLocalStr(new Date(Math.max(
        _parseDate(right).getTime() - mins * 60000,
        _parseDate(earliest).getTime())))
    : earliest;
  return { left: left, right: right };
}

// ── Environment section: per-sensor cards (View B) ↔ combined chart (View C) ──
// All sites arrive via ENV_SENSORS (°C): the generator prepends "Sensor 1" =
// the particle counter's own temp/RH from the measurement archive, followed by
// the Shellys. View B is a card grid: current values + mini-axis sparkline.
// The Cohort segment / a card click swaps in View C: the classic dual-axis
// chart (temperature left, humidity right, every sensor at once), with the
// selected sensor emphasized. State survives the auto-refresh reload.

const SPARK_BIN_MS  = 2 * 60 * 60 * 1000;  // sparkline buckets (≤28 d window)
const SPARK_DAYS    = 28;
const ENV_STALE_MIN = 30;    // minutes without a report → stale

// Sensor identity colors (Okabe–Ito, colorblind-safe), keyed by sensor number
// so Sensor 4 keeps its color even while sensors are missing.
const SITE_COLORS = ['#0072B2', '#E69F00', '#009E73', '#D55E00', '#CC79A7', '#56B4E9'];

const ENV_SITES = _SENS.slice();

function siteColor(site, idx) {
  const m = /(\d+)\s*$/.exec(site.name);
  const n = m ? parseInt(m[1], 10) - 1 : idx;
  return SITE_COLORS[((n % 6) + 6) % 6];
}
function _lastVal(arr) {
  for (let k = arr.length - 1; k >= 0; k--)
    if (arr[k] !== null && !isNaN(arr[k])) return arr[k];
  return null;
}
function _median(a) {
  const s = a.slice().sort(function (x, y) { return x - y; });
  const n = s.length;
  return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null;
}

// Latest reading per site. No status vocabulary — the only marker kept is
// "stale" (silent > 30 min), because showing a dead sensor's numbers as if
// they were live would be dishonest.
function envStatuses() {
  const span  = envTimeSpan(0);
  const nowMs = span ? _parseDate(span.right).getTime() : Date.now();
  return ENV_SITES.map(function (s, i) {
    const lastTs = s.ts.length ? _parseDate(s.ts[s.ts.length - 1]).getTime() : 0;
    return { site: s, idx: i, color: siteColor(s, i),
             t: _lastVal(s.temp), h: _lastVal(s.rh),
             stale: !s.ts.length || nowMs - lastTs > ENV_STALE_MIN * 60000 };
  });
}

// ── View B: cards with dual mini-axis sparklines ─────────────────────────────
// Each sparkline series is auto-scaled to its OWN min/max (a single sensor's
// temp only moves ~1 °C — a shared range would flatten it to a line). The
// min/max of each series and the start/end times are printed as tiny axis
// labels so the shape has a scale.
function _sparkSeries(ts, vals, i0, fromMs, toMs, y0, y1) {
  const bin = binByTime(ts.slice(i0), vals.slice(i0), SPARK_BIN_MS);
  const pts = [];
  for (let k = 0; k < bin.x.length; k++) {
    const t = _parseDate(bin.x[k]).getTime();
    if (t >= fromMs && t <= toMs) pts.push([t, bin.y[k]]);
  }
  if (pts.length < 2) return null;
  let lo = Infinity, hi = -Infinity;
  pts.forEach(function (p) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; });
  const rawLo = lo, rawHi = hi;
  if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
  const X = function (t) { return ((t - fromMs) / (toMs - fromMs) * 100).toFixed(2); };
  const Y = function (v) { return (y0 + (1 - (v - lo) / (hi - lo)) * (y1 - y0)).toFixed(2); };
  let d = '';
  pts.forEach(function (p, k) { d += (k ? 'L' : 'M') + X(p[0]) + ',' + Y(p[1]); });
  const end = pts[pts.length - 1];
  return { d: d, ex: X(end[0]), ey: Y(end[1]), lo: rawLo, hi: rawHi };
}
function _sparkTimeLbl(ms) {
  const d = _parseDate(ms);
  const p = function (n) { return String(n).padStart(2, '0'); };
  return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function _sparkHtml(site) {
  const last  = site.ts.length ? _parseDate(site.ts[site.ts.length - 1]).getTime() : Date.now();
  const first = site.ts.length ? _parseDate(site.ts[0]).getTime() : last;
  // up to SPARK_DAYS of history, but never wider than the data actually spans —
  // a young sensor's trace should fill the sparkline, not hide at its edge
  const fromMs = Math.max(last - SPARK_DAYS * 86400000, first);
  const t = _sparkSeries(site.ts, site.temp, 0, fromMs, last, 3, 35);
  const h = _sparkSeries(site.ts, site.rh,   0, fromMs, last, 3, 35);
  let svg = '<svg class="env-spark" viewBox="0 0 100 38" preserveAspectRatio="none" aria-hidden="true">';
  if (h) svg += '<path class="sh" d="' + h.d + '"/><circle class="dh" cx="' + h.ex + '" cy="' + h.ey + '" r="1.6"/>';
  if (t) svg += '<path class="st" d="' + t.d + '"/><circle class="dt" cx="' + t.ex + '" cy="' + t.ey + '" r="1.6"/>';
  svg += '</svg>';
  // mini axes: temp min/max on the left (°C), RH min/max on the right (%),
  // window start/end along the bottom
  const axL = t ? '<span>' + t.hi.toFixed(1) + '</span><span>' + t.lo.toFixed(1) + '</span>'
               : '<span></span><span></span>';
  const axR = h ? '<span>' + h.hi.toFixed(0) + '</span><span>' + h.lo.toFixed(0) + '</span>'
               : '<span></span><span></span>';
  return '<div class="env-spark-wrap">' +
           '<div class="spark-ax ax-t">' + axL + '</div>' + svg +
           '<div class="spark-ax ax-h">' + axR + '</div>' +
         '</div>' +
         '<div class="spark-x"><span>' + _sparkTimeLbl(fromMs) + '</span>' +
           '<span>' + _sparkTimeLbl(last) + '</span></div>';
}
function renderEnvCards() {
  const box = document.getElementById('env-cards');
  if (!box) return;
  box.innerHTML = envStatuses().map(function (x) {
    const tv = x.t !== null ? x.t.toFixed(1) : '—';
    const hv = x.h !== null ? x.h.toFixed(0) : '—';
    return '<div class="env-card" data-site="' + x.site.name +
      '" role="button" tabindex="0" aria-label="' + x.site.name + ' detail">' +
      '<div class="env-card-head"><span class="env-dot" style="background:' +
        _traceColor(x.color) + '"></span>' +
      '<span class="env-card-name">' + x.site.name + '</span>' +
      (x.stale ? '<span class="env-tag stale">stale</span>' : '') + '</div>' +
      '<div class="env-card-vals"><span class="vt">' + tv + '<span class="u">°C</span></span>' +
      '<span class="vh">' + hv + '<span class="u">%</span></span></div>' +
      _sparkHtml(x.site) + '</div>';
  }).join('');
}

// ── View C: the classic combined chart — every sensor at once, temperature on
// the left axis (solid, sensor color) and humidity on the right (same color,
// lighter). 15-min bins smooth the mixed cadences (counter ~2 min, Shellys
// ~6 min); a gap > 3 bins inserts a null so lines BREAK instead of bridging
// missing data (honest-gaps rule). The drilled-into / chip-selected sensor is
// drawn bold while the rest dim for context.
const COHORT_BIN_MS = 15 * 60 * 1000;
const COHORT_GAP_MS = 3 * COHORT_BIN_MS;

function _gapBrokenXY(ts, vals, i0, leftMs) {
  const bin = binByTime(ts.slice(i0), vals.slice(i0), COHORT_BIN_MS);
  const x = [], y = [];
  let prev = null;
  for (let k = 0; k < bin.x.length; k++) {
    const t = _parseDate(bin.x[k]).getTime();
    if (leftMs && t < leftMs) continue;                  // clip to visible window
    if (prev !== null && t - prev > COHORT_GAP_MS) {     // break the line
      x.push(_toLocalStr(new Date(prev + COHORT_BIN_MS))); y.push(null);
    }
    prev = t;
    x.push(bin.x[k]); y.push(bin.y[k]);
  }
  return { x: x, y: y };
}
function renderEnvCohort(mins, DARK) {
  const infos    = envStatuses();
  const span     = envTimeSpan(mins);
  const envRange = span ? { range: [span.left, span.right], autorange: false } : {};
  const leftMs   = span ? _parseDate(span.left).getTime() : 0;
  const anySel   = !!_envHighlight;
  const traces   = [];
  infos.forEach(function (xi) {
    const i0   = sliceIdxForArray(xi.site.ts, mins);
    const tSer = _gapBrokenXY(xi.site.ts, xi.site.temp, i0, leftMs);
    const hSer = _gapBrokenXY(xi.site.ts, xi.site.rh,   i0, leftMs);
    const sel  = xi.site.name === _envHighlight;
    const col  = _traceColor(xi.color);
    const wT   = anySel ? (sel ? 2.6 : 1.1) : 1.8;
    const op   = anySel ? (sel ? 1 : 0.35) : 0.9;
    traces.push({ x: tSer.x, y: tSer.y, yaxis: 'y', name: xi.site.name + ' T',
      type: 'scatter', mode: 'lines', opacity: op, showlegend: false,
      hovertemplate: xi.site.name + ' %{y:.1f} °C<extra></extra>',
      line: { color: col, width: wT } });
    traces.push({ x: hSer.x, y: hSer.y, yaxis: 'y2', name: xi.site.name + ' RH',
      type: 'scatter', mode: 'lines', opacity: op * 0.75, showlegend: false,
      hovertemplate: xi.site.name + ' %{y:.1f} %<extra></extra>',
      line: { color: col, width: wT * 0.85 } });
  });
  return Plotly.react('chart-env', traces, Object.assign({}, DARK, {
    margin: { l: 48, r: 58, t: 26, b: 46 },
    showlegend: false,
    xaxis:  Object.assign({}, DARK.xaxis, { title: '' },
                          span ? { maxallowed: span.right } : {}, envRange),
    yaxis:  Object.assign({}, DARK.yaxis, {
      title: { text: 'Temperature (°C)', standoff: 6 },
      nticks: 6, fixedrange: true }),
    yaxis2: Object.assign({}, DARK.yaxis, {
      title: { text: 'Humidity (%)', standoff: 8 },
      nticks: 6, overlaying: 'y', side: 'right', fixedrange: true, showgrid: false }),
  }), PLOTLY_CFG).then(function (gd) {
    // first cohort render turns #chart-env into a Plotly plot — attach the
    // modebar +/- zoom interceptor now (idempotent for the other charts)
    if (window._attachRelayoutListeners) window._attachRelayoutListeners();
    return gd;
  });
}
function renderEnvChips() {
  const box = document.getElementById('env-chips');
  if (!box) return;
  box.innerHTML = envStatuses().map(function (x) {
    const sel = _envHighlight === x.site.name;
    const tv = x.t !== null ? x.t.toFixed(1) + '°C' : '—';
    const hv = x.h !== null ? x.h.toFixed(0) + '%' : '—';
    return '<button class="env-chip' + (sel ? ' sel' : '') + '" data-site="' + x.site.name + '">' +
      '<span class="env-dot" style="background:' + _traceColor(x.color) + '"></span>' +
      '<b>S' + (x.site.name.replace(/\D+/g, '') || '?') + '</b> ' + tv + '/' + hv +
      (x.stale ? ' <span class="env-tag stale">stale</span>' : '') + '</button>';
  }).join('');
}

// ── View state + dispatcher (state survives the auto-refresh page reload) ──
var _envView      = sessionStorage.getItem('wlc-env-view') === 'cohort' ? 'cohort' : 'cards';
var _envHighlight = sessionStorage.getItem('wlc-env-hl') || null;

function _setEnvView(view, highlight) {
  _envView = view;
  if (highlight !== undefined) _envHighlight = highlight;
  sessionStorage.setItem('wlc-env-view', _envView);
  sessionStorage.setItem('wlc-env-hl', _envHighlight || '');
  filterAndRender();
}
function renderEnv(mins, DARK) {
  const cards = document.getElementById('env-cards');
  const chart = document.getElementById('chart-env');
  if (!cards || !chart) return Promise.resolve();   // stale/foreign markup — skip
  const chips  = document.getElementById('env-chips');
  const legend = document.getElementById('env-legend');
  const cohort = _envView === 'cohort';
  cards.style.display = cohort ? 'none' : 'grid';
  chart.style.display = cohort ? 'block' : 'none';
  if (chips)  chips.style.display  = cohort ? 'flex' : 'none';
  if (legend) legend.style.display = cohort ? 'none' : 'flex';
  const bCards  = document.getElementById('env-btn-cards');
  const bCohort = document.getElementById('env-btn-cohort');
  if (bCards)  bCards.setAttribute('aria-pressed', String(!cohort));
  if (bCohort) bCohort.setAttribute('aria-pressed', String(cohort));
  if (cohort) { renderEnvChips(); return renderEnvCohort(mins, DARK); }
  renderEnvCards();
  return Promise.resolve();
}
(function _wireEnvViews() {
  const bCards  = document.getElementById('env-btn-cards');
  const bCohort = document.getElementById('env-btn-cohort');
  const cards   = document.getElementById('env-cards');
  const chips   = document.getElementById('env-chips');
  if (!cards) return;
  if (bCards)  bCards.addEventListener('click', function () { _setEnvView('cards'); });
  if (bCohort) bCohort.addEventListener('click', function () { _setEnvView('cohort'); });
  cards.addEventListener('click', function (e) {
    const c = e.target.closest('.env-card');
    if (c) _setEnvView('cohort', c.dataset.site);
  });
  cards.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const c = e.target.closest('.env-card');
    if (c) { e.preventDefault(); _setEnvView('cohort', c.dataset.site); }
  });
  if (chips) chips.addEventListener('click', function (e) {
    const c = e.target.closest('.env-chip');
    if (!c) return;
    _setEnvView('cohort', _envHighlight === c.dataset.site ? null : c.dataset.site);
  });
})();

function getLeftBound(divId, mins) {
  const tsArray = (divId === 'chart-env') ? LIVE_TS : TS;
  const idx = sliceIdxForArray(tsArray, mins);
  return tsArray[idx] || null;
}

function sliceTraces(traces, i) {
  return traces.map(tr => Object.assign({}, tr, {
    x: tr.x.slice(i),
    y: tr.y.slice(i),
  }));
}

// ── Bin-size aggregation for the concentration charts ─────────────────────────
// The particle/PM data is ~every 4 min. The Bin dropdown (Raw / 10 / 30 / 60 min)
// optionally aggregates it. Per bin we keep BOTH the mean (trend) and the max
// (so contamination spikes are not averaged away).
function _currentBinMins(rangeMins) {
  const sel = document.getElementById('sel-bin');
  if (!sel) return 0;                       // dropdown not present yet → Raw
  const v = parseInt(sel.value);
  return isNaN(v) ? 0 : v;                  // 0 = Raw
}

// Group one (timestamps, values) series into fixed time bins. Returns, per bin:
// the mean timestamp, the mean value, and the max value. Null/NaN are skipped.
function binMeanMax(tsArr, valArr, binMs) {
  const buckets = new Map();   // bucketStartMs -> { sumV, sumT, n, max }
  for (let k = 0; k < tsArr.length; k++) {
    const v = valArr[k];
    if (v === null || v === undefined || isNaN(v)) continue;
    const t = _parseDate(tsArr[k]).getTime();
    if (isNaN(t)) continue;
    const key = Math.floor(t / binMs) * binMs;
    let b = buckets.get(key);
    if (!b) { b = { sumV: 0, sumT: 0, n: 0, max: -Infinity }; buckets.set(key, b); }
    b.sumV += v; b.sumT += t; b.n += 1; if (v > b.max) b.max = v;
  }
  const keys = Array.from(buckets.keys()).sort((a, b) => a - b);
  const x = [], mean = [], max = [];
  for (const key of keys) {
    const b = buckets.get(key);
    x.push(_toLocalStr(new Date(b.sumT / b.n)));   // mean time within the bin
    mean.push(b.sumV / b.n);
    max.push(b.max);
  }
  return { x: x, mean: mean, max: max };
}

// Turn each raw channel trace into two binned traces: a line at the bin means
// (trend) and dots at the bin maxes (peaks), color-matched and legend-linked so
// toggling the channel hides both.
function _binnedTraces(traces, binMs) {
  const out = [];
  traces.forEach(function (tr) {
    const b = binMeanMax(tr.x, tr.y, binMs);
    const color = (tr.line && tr.line.color) || '#888';
    out.push({                       // mean → connected line (trend)
      x: b.x, y: b.mean, name: tr.name,
      type: 'scatter', mode: 'lines',
      line: { color: color, width: 2 },
      legendgroup: tr.name,
    });
    out.push({                       // max → dots riding above the line (peaks)
      x: b.x, y: b.max, name: tr.name + ' (peak)',
      type: 'scatter', mode: 'markers',
      marker: { color: color, size: 5 },
      legendgroup: tr.name, showlegend: false,
    });
  });
  return out;
}

function gapShapes(ts) {
  const GAP_THRESH_MS = 90 * 60 * 1000;
  const shapes = [];
  for (let k = 1; k < ts.length; k++) {
    if (_parseDate(ts[k]) - _parseDate(ts[k - 1]) > GAP_THRESH_MS) {
      shapes.push({
        type: 'rect', xref: 'x', yref: 'paper',
        x0: ts[k - 1], x1: ts[k], y0: 0, y1: 1,
        fillcolor: 'rgba(100,116,139,0.10)', line: { width: 0 }, layer: 'below',
      });
    }
  }
  return shapes;
}

function isoShapes() {
  return ISO_LINES.map(l => ({
    type: 'line', xref: 'paper', x0: 0, x1: 1,
    yref: 'y', y0: l.y, y1: l.y,
    line: { color: _isoColor(l.color), width: l.width, dash: l.dash },
  }));
}
function isoAnnotations() {
  return ISO_LINES.map(l => ({
    // Plotly quirk: with yref on a LOG axis, annotation y is in log10 units
    // (shapes take raw data units) — without log10() the labels land
    // off-scale and never render.
    xref: 'paper', x: 1.005, yref: 'y', y: Math.log10(l.y),
    text: l.bold ? '<b>' + l.label + '</b>' : l.label,
    showarrow: false, xanchor: 'left',
    font: { color: _isoColor(l.color), size: l.bold ? 12 : 10, family: 'Arial, "Helvetica Neue", Helvetica, sans-serif' },
  }));
}

function updateStats(i) {
  const ts    = TS.slice(i);
  const ch1   = COUNTS[0].y.slice(i).filter(v => v !== null && v !== undefined);
  const ch2   = COUNTS[1].y.slice(i).filter(v => v !== null && v !== undefined);
  const n     = ts.length;
  const fmt   = v => (v !== null && !isNaN(v))
    ? Math.round(v).toLocaleString() + ' /m³' : '--';
  const mean1 = ch1.length ? ch1.reduce((a, b) => a + b, 0) / ch1.length : null;
  const peak1 = ch1.length ? Math.max(...ch1) : null;
  // Tent target is ISO 8 — exceedance = >= 0.5 µm above the ISO 8 limit
  // (3,520,000 /m³). Counted on the plotted ≥0.5 µm series.
  const exc7  = ch2.filter(v => v > 3520000).length;
  const exc7s = ch2.length
    ? exc7 + ' / ' + ch2.length + ' (' + (exc7 / ch2.length * 100).toFixed(0) + '%)'
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

// ── LOCAL FEATURE: Dynamic PM log scale calculation ──────────────────────────
// Calculates Y-axis range for PM mass chart based on currently visible data.
// Returns [minLog10, maxLog10] with padding, suitable for a log-scale Y axis.
function calculatePMLogRange(pmTraces) {
  const visibleVals = pmTraces.flatMap(tr => tr.y).filter(v => v !== null && v > 0);

  if (visibleVals.length === 0) {
    // No positive data → default range (0.01 to 10 µg/m³)
    return [-2, 1];
  }

  const rawMax = Math.max(...visibleVals);
  const rawMin = Math.min(...visibleVals);

  // Log scale with padding: -0.3 decades below, +0.5 decades above
  // Floor at 0.01 to avoid log(0) issues
  const minLog = Math.log10(Math.max(rawMin, 0.01)) - 0.3;
  const maxLog = Math.log10(rawMax) + 0.5;

  return [minLog, maxLog];
}

// ── Main render function ──────────────────────────────────────────────────────
// Always reads the current dropdown value and renders all four charts to exactly
// that time window.  The explicit xaxis.range guarantees the viewport snaps to
// the clean selected window on every call — no drift possible.
function filterAndRender() {
  const DARK = _baseLayout();   // current theme's layout — rebuilt every render
  const sel  = document.getElementById('sel-range');
  const mins = parseInt(sel.value);
  const i    = sliceIdx(mins);
  const ts   = TS.slice(i);
  const gaps = gapShapes(ts);

  // maxallowed: prevents the chart from drifting into the future.
  // Explicit range: resets the viewport to the exact selected window on every render.
  const xBounds = ts.length > 0 ? { maxallowed: TS[TS.length - 1] } : {};
  const xRange  = ts.length > 0
    ? { range: [ts[0], ts[ts.length - 1]], autorange: false }
    : {};

  // Bin selection (0 = Raw) — available for every time window, online and local.
  const binMins = _currentBinMins(mins);
  const binMs   = binMins * 60000;
  // _themedTraces runs BEFORE binning so the binned mean/max traces inherit
  // the theme-corrected channel color too.
  const countsRaw  = _themedTraces(sliceTraces(COUNTS, i));
  const pmRaw      = _themedTraces(sliceTraces(PM, i));
  const countsData = binMins > 0 ? _binnedTraces(countsRaw, binMs) : countsRaw;
  const pmData     = binMins > 0 ? _binnedTraces(pmRaw, binMs)     : pmRaw;

  // ── Particle count chart (log scale) ─────────────────────────────────────
  // yaxis.fixedrange: true  →  scroll and +/- only move the X (time) axis.
  // autorange: false + COUNTS_Y_RANGE  →  Y never jumps on window changes.
  const p1 = Plotly.react('chart-counts', countsData,
    Object.assign({}, DARK, {
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'Counts / m³',
        type:       'log',
        autorange:  false,
        range:      COUNTS_Y_RANGE,
        fixedrange: true,
        dtick:      1,  // One tick per order of magnitude (10^0, 10^1, 10^2, ...)
      }),
      xaxis:       Object.assign({}, DARK.xaxis, { title: '' }, xBounds, xRange),
      margin:      { l: 60, r: 72, t: 30, b: 50 },
      shapes:      [...gaps, ...isoShapes()],
      annotations: isoAnnotations(),
    }), PLOTLY_CFG);

  // ── PM mass chart (LOG scale, dynamic range) ──────────────────────────────
  // Local dashboard only — the public page omits the #chart-pm div entirely.
  // Dynamic log scale adapts to visible data, preventing values from hugging zero.
  const p2 = document.getElementById('chart-pm')
    ? (function() {
        const pmRange = calculatePMLogRange(pmData);
        return Plotly.react('chart-pm', pmData,
          Object.assign({}, DARK, {
            yaxis: Object.assign({}, DARK.yaxis, {
              title:      'μg / m³',
              type:       'log',
              range:      pmRange,
              autorange:  false,
              fixedrange: true,
              dtick:      1,  // One tick per order of magnitude
            }),
            xaxis:  Object.assign({}, DARK.xaxis, { title: '' }, xBounds, xRange),
            shapes: gaps,
          }), PLOTLY_CFG);
      })()
    : Promise.resolve();

  // ── Size distribution bar chart ───────────────────────────────────────────
  // Bar colors, label and outline colors are baked into DIST by the generator;
  // re-skin them to the current theme on every render.
  if (DIST[0]) {
    DIST[0].textfont = { color: _themeMuted(), size: 11 };
    if (DIST[0].marker) {
      DIST[0].marker.line = { color: _themeBorder(), width: 1 };
      if (DIST_BASE_COLORS) DIST[0].marker.color = DIST_BASE_COLORS.map(_traceColor);
    }
  }
  const _distMax    = (DIST[0] && DIST[0].y.length) ? Math.max(...DIST[0].y) : 100;
  const _distLogMax = Math.log10(Math.max(_distMax, 1)) + 1.0;
  const p3 = Plotly.react('chart-dist', DIST,
    Object.assign({}, DARK, {
      showlegend: false,
      bargap:     0.3,
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'Counts',
        type:       'log',
        range:      [-0.5, _distLogMax],
        autorange:  false,
        fixedrange: true,
        dtick:      1,  // One tick per order of magnitude
      }),
      xaxis: Object.assign({}, DARK.xaxis, {
        title:      'Particle Size (μm)',
        fixedrange: true,
      }),
    }), PLOTLY_CFG);

  // ── Environment section: sensor cards (View B) or cohort envelope (View C).
  // All rendering lives in renderEnv() above; it no-ops on markup it doesn't
  // recognize so a stale generated page can't throw here.
  const p4 = renderEnv(mins, DARK);

  updateStats(i);

  return Promise.all([p1, p2, p3, p4]);
}

// ── Date helpers ─────────────────────────────────────────────────────────────
// Plotly relayout events return timestamps as "YYYY-MM-DD HH:MM:SS.mmm"
// (space-separated, not valid ISO 8601).  Replace the space with T so that
// new Date() parses reliably.  Numeric epoch values are handled by the else branch.
function _parseDate(str) {
  if (typeof str !== 'string') return new Date(+str);
  return new Date(str.replace(' ', 'T'));
}

// Format a JS Date as "YYYY-MM-DD HH:MM:SS" in LOCAL time, matching the
// Python-generated CSV timestamp format so Plotly reads it in the correct timezone.
function _toLocalStr(date) {
  const p = n => String(n).padStart(2, '0');
  return date.getFullYear() + '-' + p(date.getMonth() + 1) + '-' + p(date.getDate())
       + ' ' + p(date.getHours()) + ':' + p(date.getMinutes()) + ':' + p(date.getSeconds());
}

// ── Time-bin aggregation ──────────────────────────────────────────────────────
// The env data is sampled ~every 10 s, far too dense to show one error bar per
// point. binByTime() groups parallel (timestamp, value) arrays into fixed-width
// time buckets and returns the mean value per bucket, plotted at the mean
// timestamp of the points in that bucket (so a marker never lands past the
// latest sample). Empty/NaN values and empty buckets are skipped.
function binByTime(tsArr, valArr, binMs) {
  const buckets = new Map();   // bucketStartMs -> { sumV, sumT, n }
  for (let k = 0; k < tsArr.length; k++) {
    const v = valArr[k];
    if (v === null || v === undefined || isNaN(v)) continue;
    const tms = _parseDate(tsArr[k]).getTime();
    if (isNaN(tms)) continue;
    const key = Math.floor(tms / binMs) * binMs;
    let b = buckets.get(key);
    if (!b) { b = { sumV: 0, sumT: 0, n: 0 }; buckets.set(key, b); }
    b.sumV += v; b.sumT += tms; b.n += 1;
  }
  const keys = Array.from(buckets.keys()).sort((a, b) => a - b);
  const x = [], y = [];
  for (const key of keys) {
    const b = buckets.get(key);
    x.push(_toLocalStr(new Date(b.sumT / b.n)));   // mean time within the bucket
    y.push(b.sumV / b.n);                            // mean value within the bucket
  }
  return { x: x, y: y };
}

// ── Zoom state ────────────────────────────────────────────────────────────────
// _zooming is a debounce guard: while filterAndRender() is in-flight, incoming
// wheel events are ignored so rapid scrolling doesn't queue multiple renders.
let _zooming = false;

// ── Step-based zoom ───────────────────────────────────────────────────────────
// direction: -1 = zoom in  (step to smaller time window, decrease selectedIndex)
//            +1 = zoom out (step to larger time window,  increase selectedIndex)
//
// The dropdown options are ordered smallest → largest:
//   index 0: 30 min  (hard stop — can't zoom in further)
//   index 1: 1 hr
//   index 2: 6 hr
//   index 3: 12 hr
//   index 4: 24 hr
//   index 5: 7 days  (hard stop — can't zoom out further)
//
// Stepping outside [0, options.length-1] is silently ignored.
function _stepZoom(direction) {
  if (_zooming) return;
  const sel      = document.getElementById('sel-range');
  let newIndex = sel.selectedIndex + direction;
  
  // Hard stops: clamp to valid range.
  // We MUST call filterAndRender() even if already at the limit, because if this
  // was triggered by a modebar +/- click, Plotly natively zoomed the chart and
  // we need filterAndRender() to snap it back to our hard limit.
  if (newIndex < 0) newIndex = 0;
  if (newIndex >= sel.options.length) newIndex = sel.options.length - 1;
  
  sel.selectedIndex = newIndex;
  _zooming = true;
  filterAndRender().then(function () {
    _zooming = false;
  }).catch(function () {
    _zooming = false;
  });
}

// ── Attach wheel listeners ────────────────────────────────────────────────────
// Intercepts scroll-wheel / trackpad events on all three time-series chart divs
// and converts them into discrete zoom steps via _stepZoom().
//
// Convention (matches Plotly's original scroll direction):
//   deltaY < 0  (scroll up / pinch out)  → zoom in  → _stepZoom(-1)
//   deltaY > 0  (scroll down / pinch in) → zoom out → _stepZoom(+1)
//
// { passive: false } is required so that ev.preventDefault() can suppress the
// browser's default page-scroll behaviour while the cursor is over a chart.
window._attachWheelListeners = function () {
  ['chart-counts', 'chart-pm', 'chart-env'].forEach(function (divId) {
    var el = document.getElementById(divId);
    if (!el) return;   // chart-pm is absent on the public dashboard
    el.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      // deltaY > 0: scroll down = zoom out (+1); deltaY < 0: scroll up = zoom in (-1)
      _stepZoom(ev.deltaY > 0 ? +1 : -1);
    }, { passive: false });
  });
};

// ── Attach modebar +/- button listeners ──────────────────────────────────────
// Plotly's zoom-in (+) and zoom-out (-) modebar buttons fire plotly_relayout
// with a new xaxis.range.  We intercept that event, detect which button was
// pressed by comparing the new span to the current dropdown's span, and
// delegate to _stepZoom() so the behaviour is identical to the scroll wheel.
//
//   New span < current span → zoom-in  (+) pressed → _stepZoom(-1)
//   New span > current span → zoom-out (-) pressed → _stepZoom(+1)
//   New span ≈ current span (pan) → ignored
//
// The _zooming guard (set by _stepZoom / filterAndRender) prevents the
// relayout event that filterAndRender itself fires from triggering a
// second recursive call.
window._attachRelayoutListeners = function () {
  ['chart-counts', 'chart-pm', 'chart-env'].forEach(function (divId) {
    var el = document.getElementById(divId);
    // skip absent divs AND divs Plotly hasn't initialized — chart-env only
    // becomes a Plotly plot once the Cohort view is first opened (renderEnvCohort
    // re-invokes this attacher, so the guard also makes it idempotent)
    if (!el || typeof el.on !== 'function' || el._wlcRelayoutAttached) return;
    el._wlcRelayoutAttached = true;
    el.on('plotly_relayout', function (ev) {
      if (_zooming) return;  // ignore redraws triggered by our own filterAndRender

      // Extract the new xaxis range from the event (Plotly uses two formats).
      var x0 = ev['xaxis.range[0]'];
      var x1 = ev['xaxis.range[1]'];
      if (x0 === undefined && Array.isArray(ev['xaxis.range'])) {
        x0 = ev['xaxis.range'][0];
        x1 = ev['xaxis.range'][1];
      }
      if (x0 === undefined || x1 === undefined) return;  // not a range event

      var newSpanMs     = _parseDate(x1).getTime() - _parseDate(x0).getTime();
      var currentMins   = parseInt(document.getElementById('sel-range').value);
      var currentSpanMs = currentMins * 60 * 1000;

      if (newSpanMs < currentSpanMs - 1000) {
        _stepZoom(-1);   // zoom-in (+) button
      }
      // Zoom-out (-) is handled by _attachZoomOutButtonListeners() via a direct
      // click handler — span inference is unreliable for zoom-out because the
      // maxallowed right-edge clamp and the nominal-vs-actual window mismatch
      // keep the reported span from growing past currentSpanMs.
      // If spans are equal (pan), do nothing — chart stays at current window.
    });
  });
};

// ── Attach modebar zoom-out (-) button listener ───────────────────────────────
// Bind directly to Plotly's "Zoom out" modebar button (event delegation on each
// chart div, so it survives modebar re-creation) and step to the next-larger
// time window — identical behaviour to scroll-out and the '-' keyboard shortcut.
//
// Capture phase + stopPropagation: the modebar button is a NATIVE Plotly button,
// so a click would otherwise ALSO run Plotly's built-in zoom-out, which fires a
// plotly_relayout that races our _stepZoom(+1) through the shared _zooming guard
// (intermittently swallowing the step). Intercepting in the capture phase lets us
// cancel the native handler before it runs, so only our discrete step happens.
window._attachZoomOutButtonListeners = function () {
  ['chart-counts', 'chart-pm', 'chart-env'].forEach(function (divId) {
    var el = document.getElementById(divId);
    if (!el) return;   // chart-pm is absent on the public dashboard
    el.addEventListener('click', function (ev) {
      if (ev.target.closest('[data-attr="zoom"][data-val="out"]')) {
        ev.stopPropagation();   // block Plotly's native zoom-out handler
        ev.preventDefault();
        _stepZoom(+1);          // zoom-out (-) button → next-larger time window
      }
    }, true);   // true = capture phase, runs before the button's own handler
  });
};

// ── Auto-refresh + manual refresh control ─────────────────────────────────────
// The dashboard data is baked into index.html (regenerated by the daemon), so
// "refresh to latest" means re-fetching the page. To keep that smooth:
//   • the selected time range is preserved across reloads (sessionStorage) so a
//     refresh — auto or manual — never throws away the user's current zoom,
//   • the page auto-reloads every 60 s (skipping a cycle while the Alerts panel
//     is open, so it isn't yanked away mid-read),
//   • a manual refresh button is injected into the header (next to Time Range).
//     Kept entirely here so particle_plus.py is untouched.
var AUTO_REFRESH_MS = 60000;
var _refreshTimer   = null;

function _restoreTimeRange() {
  try {
    var saved = sessionStorage.getItem('wlc-range');
    if (saved === null) return;
    var sel = document.getElementById('sel-range');
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === saved) { sel.selectedIndex = i; break; }
    }
  } catch (e) { /* sessionStorage unavailable → keep the default range */ }
}

function _saveTimeRange() {
  try {
    sessionStorage.setItem('wlc-range', document.getElementById('sel-range').value);
  } catch (e) { /* ignore */ }
}

function _restoreBinSize() {
  try {
    var saved = sessionStorage.getItem('wlc-bin');
    if (saved === null) return;
    var sel = document.getElementById('sel-bin');
    if (!sel) return;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === saved) { sel.selectedIndex = i; break; }
    }
  } catch (e) { /* sessionStorage unavailable → keep the default bin */ }
}

function _saveBinSize() {
  try {
    var sel = document.getElementById('sel-bin');
    if (sel) sessionStorage.setItem('wlc-bin', sel.value);
  } catch (e) { /* ignore */ }
}

function _refreshNow() {
  var btn = document.getElementById('wlc-refresh-btn');
  if (btn) btn.classList.add('spinning');   // brief spin so the reload feels intentional
  _saveTimeRange();
  _saveBinSize();
  if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
  setTimeout(function () { window.location.reload(); }, 450);
}

function _autoRefreshTick() {
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open')) return;  // don't close the panel mid-read
  _refreshNow();
}

function _attachRefreshControl() {
  if (document.getElementById('wlc-refresh-style')) return;  // idempotent

  var css = document.createElement('style');
  css.id = 'wlc-refresh-style';
  css.textContent =
    '@keyframes wlc-spin{to{transform:rotate(360deg)}}' +
    '.wlc-refresh-wrap{display:flex;align-items:center;align-self:flex-end;margin-bottom:0}' +
    // Theme-aware square (reads the CSS variables defined on :root); sized to
    // match the Time Range dropdown's height.
    '.wlc-refresh-btn{display:inline-flex;align-items:center;justify-content:center;' +
      'width:24px;height:24px;background:var(--bg-card);border:1px solid var(--border-color);border-radius:5px;' +
      'color:var(--text-primary);cursor:pointer;padding:0;' +
      'transition:border-color .15s,color .15s,transform .15s,box-shadow .15s,background .15s}' +
    '.wlc-refresh-btn:hover{background:var(--bg-card-alt);border-color:var(--accent-yale-light);' +
      'transform:rotate(-40deg);box-shadow:0 0 0 1px rgba(40,109,192,.35),0 0 12px rgba(40,109,192,.2)}' +
    '.wlc-refresh-btn:active{transform:scale(.92)}' +
    '.wlc-refresh-btn svg{width:14px;height:14px;display:block}' +
    '.wlc-refresh-btn.spinning svg{animation:wlc-spin .6s linear infinite}' +
    // Larger, more readable "Last pushed" label (overrides the .updated rule
    // from particle_plus.py, which is left untouched).
    '.updated{font-size:14px}';
  document.head.appendChild(css);

  var wrap = document.createElement('div');
  wrap.className = 'wlc-refresh-wrap';
  wrap.innerHTML =
    '<button id="wlc-refresh-btn" class="wlc-refresh-btn" type="button" ' +
      'title="Refresh to latest data" aria-label="Refresh to latest data">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>' +
    '</button>';

  var updated = document.querySelector('.updated');
  if (updated && updated.parentNode) {
    updated.parentNode.insertBefore(wrap, updated);  // before "Last pushed" → next to Time Range
  } else {
    var controls = document.querySelector('.controls');
    if (!controls) return;
    controls.appendChild(wrap);
  }

  var btn = document.getElementById('wlc-refresh-btn');
  btn.addEventListener('click', _refreshNow);

  // Size the square to the Time Range dropdown's ACTUAL rendered height in this
  // browser — native <select> heights vary by OS/browser, so measuring live is
  // the only reliable way to make the square's side equal the rectangle's height.
  var selBox = document.getElementById('sel-range');
  if (selBox) {
    var h = Math.round(selBox.getBoundingClientRect().height);
    if (h >= 16 && h <= 60) {            // sanity-bound the measurement
      btn.style.width  = h + 'px';
      btn.style.height = h + 'px';
      var svg = btn.querySelector('svg');
      if (svg) {
        var icon = Math.round(h * 0.55);  // keep the glyph proportional
        svg.style.width  = icon + 'px';
        svg.style.height = icon + 'px';
      }
    }
  }
}

// ── LOCAL FEATURE: Custom Time Range Modal ────────────────────────────────────
// Allows users to specify arbitrary time ranges (e.g., "Last 5 days", "Last 18 hours")
function initCustomRangeModal() {
  var modal = document.getElementById('custom-range-modal');
  var sel = document.getElementById('sel-range');
  var applyBtn = document.getElementById('custom-range-apply');
  var cancelBtn = document.getElementById('custom-range-cancel');
  var valueInput = document.getElementById('custom-range-value');
  var unitSelect = document.getElementById('custom-range-unit');
  var errorDiv = document.getElementById('custom-range-error');

  if (!modal || !sel) return;  // Modal not present (public dashboard)

  // When dropdown changes to "Custom..." (-1), show modal
  sel.addEventListener('change', function() {
    if (parseInt(sel.value) === -1) {
      showCustomRangeModal();
    }
  });

  // Cancel button
  if (cancelBtn) {
    cancelBtn.addEventListener('click', function() {
      hideCustomRangeModal();
      restoreLastValidRange();
    });
  }

  // Apply button
  if (applyBtn) {
    applyBtn.addEventListener('click', function() {
      var value = parseInt(valueInput.value);
      var unitSeconds = parseInt(unitSelect.value);

      // Validation
      if (isNaN(value) || value <= 0) {
        showCustomRangeError('Please enter a positive number');
        return;
      }
      if (value > 365 && unitSeconds === 86400) {
        showCustomRangeError('Maximum 365 days allowed');
        return;
      }

      var totalMinutes = Math.floor((value * unitSeconds) / 60);
      applyCustomRange(totalMinutes, value, unitSeconds);
    });
  }

  // Enter key to apply
  if (valueInput) {
    valueInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter') applyBtn.click();
    });
  }

  // Restore custom range on page load if it exists
  restoreCustomRange();
}

function showCustomRangeModal() {
  var modal = document.getElementById('custom-range-modal');
  if (modal) {
    modal.style.display = 'flex';
    document.getElementById('custom-range-value').focus();
    document.getElementById('custom-range-error').style.display = 'none';
  }
}

function hideCustomRangeModal() {
  var modal = document.getElementById('custom-range-modal');
  if (modal) modal.style.display = 'none';
}

function showCustomRangeError(msg) {
  var errorDiv = document.getElementById('custom-range-error');
  if (errorDiv) {
    errorDiv.textContent = msg;
    errorDiv.style.display = 'block';
  }
}

function applyCustomRange(totalMinutes, value, unitSeconds) {
  var sel = document.getElementById('sel-range');

  // Store custom range in sessionStorage
  try {
    sessionStorage.setItem('wlc-custom-mins', totalMinutes);
    sessionStorage.setItem('wlc-custom-value', value);
    sessionStorage.setItem('wlc-custom-unit', unitSeconds);
  } catch (e) { /* ignore */ }

  // Update dropdown: find if this value already exists, otherwise add it
  var found = false;
  for (var i = 0; i < sel.options.length; i++) {
    if (parseInt(sel.options[i].value) === totalMinutes) {
      sel.selectedIndex = i;
      found = true;
      break;
    }
  }

  if (!found) {
    // Add custom option before "Custom..." option (which is last)
    var unitName = unitSeconds === 86400 ? 'day' : unitSeconds === 3600 ? 'hour' : 'min';
    if (value > 1) unitName += 's';
    var customOption = document.createElement('option');
    customOption.value = totalMinutes;
    customOption.text = 'Last ' + value + ' ' + unitName;
    sel.insertBefore(customOption, sel.options[sel.options.length - 1]);
    sel.value = totalMinutes;
  }

  hideCustomRangeModal();
  filterAndRender();
}

function restoreLastValidRange() {
  var sel = document.getElementById('sel-range');
  var saved = sessionStorage.getItem('wlc-range');
  if (saved && saved !== '-1') {
    sel.value = saved;
  } else {
    sel.value = '1440';  // Default to 24 hours
  }
}

function restoreCustomRange() {
  try {
    var customMins = sessionStorage.getItem('wlc-custom-mins');
    var customValue = sessionStorage.getItem('wlc-custom-value');
    var customUnit = sessionStorage.getItem('wlc-custom-unit');

    if (customMins && customValue && customUnit) {
      applyCustomRange(parseInt(customMins), parseInt(customValue), parseInt(customUnit));
    }
  } catch (e) { /* ignore */ }
}

// ── Bin-size dropdown ─────────────────────────────────────────────────────────
// Injected next to Time Range (so particle_plus.py's header stays untouched); it
// inherits the existing <select> styling. Changing it re-renders the charts.
function _attachBinControl() {
  if (document.getElementById('sel-bin')) return;          // idempotent
  var rangeSel = document.getElementById('sel-range');
  if (!rangeSel) return;
  var rangeGroup = rangeSel.closest('.ctrl-group') || rangeSel.parentNode;

  var group = document.createElement('div');
  group.className = 'ctrl-group';
  group.innerHTML =
    '<label>Bin</label>' +
    '<select id="sel-bin">' +
      '<option value="0">Raw</option>' +
      '<option value="10" selected>10 min</option>' +
      '<option value="30">30 min</option>' +
      '<option value="60">1 hr</option>' +
    '</select>';
  rangeGroup.parentNode.insertBefore(group, rangeGroup.nextSibling);
  var binSel = document.getElementById('sel-bin');
  binSel.addEventListener('change', function() {
    _saveBinSize();
    filterAndRender();
  });
  _restoreBinSize();  // restore saved bin size after creating the dropdown
}

// ── Theme toggle ──────────────────────────────────────────────────────────────
// The <html data-theme> attribute is set before render by the no-flash script
// in <head> (localStorage 'wlc-theme', falling back to the OS preference).
// The button label swaps via pure CSS (.tt-light / .tt-dark spans), so the
// click handler only has to flip the attribute, persist it, and re-render.
function _applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('wlc-theme', theme); } catch (e) { /* ignore */ }
  // filterAndRender() rebuilds every chart layout from _baseLayout(), which
  // re-reads getPlotlyTheme() — this re-skins all CHART_IDS in one pass
  // (including yaxis2 and bar-label colors, which a bare relayout patch misses).
  filterAndRender();
}

function _attachThemeToggle() {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  btn.addEventListener('click', function () {
    _applyTheme(_isLightTheme() ? 'dark' : 'light');
  });
}

// ── Initial render ────────────────────────────────────────────────────────────
_restoreTimeRange();   // keep the user's zoom level across reloads
initCustomRangeModal();   // LOCAL ONLY: initialize custom time range modal
_attachBinControl();   // inject the Bin dropdown before the first render
filterAndRender();
// Attach wheel listeners after charts exist in the DOM.
window._attachWheelListeners();
// Attach modebar +/- listeners after charts exist in the DOM.
window._attachRelayoutListeners();
// Attach the direct zoom-out (-) modebar button handler.
window._attachZoomOutButtonListeners();
// Wire up the dark/light theme toggle button in the header.
_attachThemeToggle();
// Inject the refresh control and start the 60 s auto-refresh.
_attachRefreshControl();
window.addEventListener('beforeunload', function() {
  _saveTimeRange();
  _saveBinSize();
});
_refreshTimer = setInterval(_autoRefreshTick, AUTO_REFRESH_MS);

document.addEventListener('click', function (e) {
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open') && !drop.parentElement.contains(e.target)) {
    drop.classList.remove('open');
  }
});

// ── Attach keyboard shortcuts (+/-) ───────────────────────────────────────────
window.addEventListener('keydown', function(ev) {
  // Ignore if user is typing in an input/textarea
  if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'TEXTAREA') return;

  // '-' or '_' zooms out (+1)
  if (ev.key === '-' || ev.key === '_') {
    _stepZoom(+1);
  }
  // '+' or '=' zooms in (-1)
  else if (ev.key === '+' || ev.key === '=') {
    _stepZoom(-1);
  }
});
