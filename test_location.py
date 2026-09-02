#!/usr/bin/env python3
"""
Self-check for the --location feature. No framework, just asserts:

    python3 test_location.py

Covers the three pieces that can silently rot:

1. run_all.py builds the counter command correctly, and SHELL-QUOTES the
   location. That string is interpolated into a command tmux runs through a
   shell, so an unquoted location with spaces would split into stray argv
   entries and one with a `;` would execute.

2. particle_plus.py encodes the per-record location as RUN-LENGTH SPANS.
   A flat one-entry-per-record array would add ~1.1 MB of the same repeated
   string to a 45k-record page, undoing the Change-5 page-size work. The span
   count must track the number of MOVES, never the number of records.

3. chart_interactions_local.js resolves those spans back to the locations
   visible in the selected window (the JS half runs under node; skipped with a
   loud notice if node is absent).
"""

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import particle_plus as pp
import run_all

failures = 0


def check(name, cond):
    global failures
    if cond:
        print(f'  ok  {name}')
    else:
        print(f'  FAIL {name}')
        failures += 1


# ── 1. run_all.py command construction ───────────────────────────────────────

def test_run_all_command():
    print('\nrun_all.programs()')

    counter = dict(run_all.programs(None))['counter']
    check('no --location when none given (particle_plus owns the default)',
          '--location' not in counter)

    counter = dict(run_all.programs('Clean tent 1'))['counter']
    check('--location present when given', '--location' in counter)

    # The whole point of shlex.quote: the location survives the shell as ONE
    # argument. Parse it back the way a shell would.
    import shlex
    argv = shlex.split(counter)
    check('location survives the shell as a single argv entry',
          argv[argv.index('--location') + 1] == 'Clean tent 1')

    # Trust boundary: this string reaches a shell. It must stay data.
    evil = 'tent"; touch /tmp/wlc_pwned; #'
    argv = shlex.split(dict(run_all.programs(evil))['counter'])
    check('a location containing shell metacharacters stays one inert argument',
          argv[argv.index('--location') + 1] == evil)
    check('no /tmp/wlc_pwned side effect was possible',
          not os.path.exists('/tmp/wlc_pwned'))


# ── 2. span encoding, end to end through the real generator ──────────────────

_CH = [f'ch{i}_{k}' for i in range(1, 7)
       for k in ('size_um', 'diff_m3', 'pm_ugm3', 'sum_m3', 'diff_counts')]
_COLS = ['record_number', 'date', 'time', 'location', 'temp_C', 'RH_pct',
         'flow_CFM', 'laser_ok', 'flow_ok'] + _CH


def _write_csv(path, locations):
    """One record per location entry, spaced 5 min apart (so each is its own
    batch and the generator keeps them in order)."""
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=_COLS)
        w.writeheader()
        for n, loc in enumerate(locations, start=1):
            row = {c: '' for c in _COLS}
            row.update({
                'record_number': n,
                'date': '2026-09-01',
                'time': f'{8 + (n * 5) // 60:02d}:{(n * 5) % 60:02d}:00',
                'location': loc,
                'temp_C': 21.0, 'RH_pct': 45.0, 'flow_CFM': 0.1,
                'laser_ok': 'True', 'flow_ok': 'True',
            })
            for i in range(1, 7):
                row[f'ch{i}_size_um'] = [0.3, 0.5, 1.0, 2.5, 5.0, 10.0][i - 1]
                row[f'ch{i}_diff_m3'] = 1000 * i
                row[f'ch{i}_sum_m3'] = 5000 * i
                row[f'ch{i}_diff_counts'] = 10 * i
                row[f'ch{i}_pm_ugm3'] = 0.5 * i
            w.writerow(row)


def _spans_from_page(tmp, locations, local=True):
    src = os.path.join(tmp, 'in.csv')
    out = os.path.join(tmp, 'out.html')
    _write_csv(src, locations)
    pp.generate_dashboard_html(src, out, days=None, env_days=None, local=local)
    html = open(out, encoding='utf-8').read()
    m = re.search(r'^const LOC_SPANS = (.*);$', html, re.M)
    return (json.loads(m.group(1)) if m else None), html


def test_spans():
    print('\nparticle_plus.generate_dashboard_html() — LOC_SPANS')
    tmp = tempfile.mkdtemp(prefix='wlc_loc_')
    try:
        A, B = 'Assembly Clean Tent', 'Clean tent 1'

        spans, html = _spans_from_page(tmp, [A] * 40)
        check('one location over 40 records → exactly ONE span',
              spans == [[0, A]])

        spans, _ = _spans_from_page(tmp, [A] * 30 + [B] * 20)
        check('one move → two spans, at the right index',
              spans == [[0, A], [30, B]])

        # The regression that matters: spans must scale with MOVES, not records.
        spans, _ = _spans_from_page(tmp, [A] * 300)
        check('300 records, no move → still ONE span (not 300)',
              spans == [[0, A]])

        spans, _ = _spans_from_page(tmp, [A] * 5 + [''] * 5 + [A] * 5)
        check('a blank location continues the span rather than breaking it',
              spans == [[0, A]])

        spans, _ = _spans_from_page(tmp, [A] * 5 + [B] * 5 + [A] * 5)
        check('moving back re-opens a span (3 spans, A B A)',
              spans == [[0, A], [5, B], [10, A]])

        # Scope: local only — the public page is pinned to one TV viewport.
        _, local_html = _spans_from_page(tmp, [A] * 5, local=True)
        _, pub_html = _spans_from_page(tmp, [A] * 5, local=False)
        check('local page carries the #chart-loc div',
              'id="chart-loc"' in local_html)
        check('public page does NOT carry it',
              'id="chart-loc"' not in pub_html)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 3. the JS window lookup, from the real source file ───────────────────────

_JS_PROBE = r'''
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const decl = src.match(/function _locationsInWindow[\s\S]*?\n}/);
if (!decl) { console.log(JSON.stringify({error: 'function not found'})); process.exit(0); }
const make = (spans, n) =>
  new Function('LOC_SPANS', 'TS', `${decl[0]}\nreturn _locationsInWindow;`)
    (spans, new Array(n).fill('t'));
const A = 'Assembly Clean Tent', B = 'Clean tent 1';
console.log(JSON.stringify({
  single:      make([[0, A]], 50)(0),
  bothInView:  make([[0, A], [30, B]], 50)(0),
  afterMove:   make([[0, A], [30, B]], 50)(35),
  exactBound:  make([[0, A], [30, B]], 50)(30),
  lastOfOld:   make([[0, A], [30, B]], 50)(29),
  backAndForth: make([[0, A], [20, B], [40, A]], 50)(0),
  empty:       make([], 50)(0),
}));
'''


def test_js_window():
    print('\nchart_interactions_local.js — _locationsInWindow()')
    node = shutil.which('node')
    if not node:
        print('  SKIP (node not installed) — JS half unverified')
        return
    js = os.path.join(BASE_DIR, 'features', 'dashboard',
                      'chart_interactions_local.js')
    probe = os.path.join(tempfile.gettempdir(), 'wlc_loc_probe.js')
    with open(probe, 'w') as f:
        f.write(_JS_PROBE)
    try:
        r = subprocess.run([node, probe, js], capture_output=True, text=True)
        if r.returncode != 0:
            check(f'node probe ran (stderr: {r.stderr.strip()[:120]})', False)
            return
        got = json.loads(r.stdout)
    finally:
        os.path.exists(probe) and os.remove(probe)

    if 'error' in got:
        check('_locationsInWindow found in the source file', False)
        return

    A, B = 'Assembly Clean Tent', 'Clean tent 1'
    check('one span, whole window → that location', got['single'] == [A])
    check('window spanning a move → both, current last',
          got['bothInView'] == [A, B])
    check('window starting AFTER the move → only the new location',
          got['afterMove'] == [B])
    check('window starting exactly at the move index → only the new location',
          got['exactBound'] == [B])
    check('window starting one record earlier → both',
          got['lastOfOld'] == [A, B])
    check('moved away and back → listed once, current (A) last',
          got['backAndForth'] == [B, A])
    check('no spans → nothing to render', got['empty'] == [])


if __name__ == '__main__':
    test_run_all_command()
    test_spans()
    test_js_window()
    print('\nall checks passed' if failures == 0
          else f'\n{failures} CHECK(S) FAILED')
    sys.exit(0 if failures == 0 else 1)
