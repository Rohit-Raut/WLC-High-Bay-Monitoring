#!/usr/bin/env python3
"""
Self-check for the two dashboard-generation fixes. No framework, just asserts:

    python3 test_atomic_write.py

1. generate_dashboard_html() must publish atomically. local_serve.py rebuilds
   the page on a background thread while ThreadingHTTPServer serves that same
   file; the old in-place write truncated it to 0 and streamed 5 MB back in, so
   a request landing mid-write got truncated HTML, the JS died mid-literal, and
   the page sat dead with no charts. This reads the file continuously while
   rebuilds run and asserts every read is a complete document.

2. The local page must not carry the env_live.csv series (LIVE_TS / TEMP_F /
   RH_VALS) — its env section renders from ENV_SENSORS, so those were ~2.6 MB
   of dead weight on every 5-minute reload. The PUBLIC page still needs them.
"""

import os
import re
import sys
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import particle_plus as pp

OUT_DIR = os.path.join(BASE_DIR, '.test_out')
ROUNDS  = 3


def _csv_source():
    for path in (pp.ARCHIVE_CSV, pp.LIVE_CSV):
        if os.path.exists(path):
            return path
    sys.exit('no measurement csv to build from — run this where data/ exists')


def test_atomic_publish(src):
    """Every read during concurrent rebuilds must see a complete document."""
    out = os.path.join(OUT_DIR, 'index_race.html')
    pp.generate_dashboard_html(src, out, days=None, env_days=None, local=True)

    done = threading.Event()

    def rebuild_loop():
        try:
            for _ in range(ROUNDS):
                pp.generate_dashboard_html(src, out, days=None, env_days=None, local=True)
        finally:
            done.set()

    t = threading.Thread(target=rebuild_loop)
    t.start()

    reads = torn = 0
    while not done.is_set():
        with open(out, encoding='utf-8', errors='replace') as f:
            html = f.read()
        reads += 1
        # A complete page ends with the closing tag; a truncated one does not.
        if not html.rstrip().endswith('</html>'):
            torn += 1
    t.join()

    assert reads > 0, 'race window never sampled — rebuild finished too fast'
    assert torn == 0, f'{torn}/{reads} reads saw a truncated page'
    print(f'  ok  {reads} concurrent reads during {ROUNDS} rebuilds, 0 truncated')


def _const(html, name):
    """The JSON array literal assigned to `const <name>` in the page."""
    m = re.search(r'\bconst %s\s*=\s*(\[.*?\]);' % name, html)
    assert m, f'const {name} missing from generated page'
    return m.group(1)


def test_dead_series_local_only(src):
    """LIVE_TS/TEMP_F/RH_VALS: empty on the local page, populated on the public one."""
    local_path  = os.path.join(OUT_DIR, 'index_local.html')
    public_path = os.path.join(OUT_DIR, 'index_public.html')
    # Both built with no cutoff so the only difference under test is local=,
    # not how fresh env_live.csv happens to be on this host (the real public
    # build passes env_days=8, which starves this assertion on a stale archive).
    pp.generate_dashboard_html(src, local_path,  days=None, env_days=None, local=True)
    pp.generate_dashboard_html(src, public_path, days=None, env_days=None, local=False)

    local_html  = open(local_path,  encoding='utf-8', errors='replace').read()
    public_html = open(public_path, encoding='utf-8', errors='replace').read()

    for name in ('LIVE_TS', 'TEMP_F', 'RH_VALS'):
        assert _const(local_html, name) == '[]', f'{name} still shipped to the local page'
    assert _const(public_html, 'LIVE_TS') != '[]', \
        'public env chart lost LIVE_TS — chart_interactions.js needs it'

    # the local summary cards read the same source server-side, so they must survive
    assert re.search(r'\bconst TS\s*=\s*\[".+?"', local_html), 'local page lost its TS series'

    saved = (len(public_html) - len(local_html)) / 1e6
    print(f'  ok  local page {len(local_html)/1e6:.2f} MB, public {len(public_html)/1e6:.2f} MB '
          f'(local carries {saved:.2f} MB less)')


if __name__ == '__main__':
    os.makedirs(OUT_DIR, exist_ok=True)
    source = _csv_source()
    print(f'building from {os.path.basename(source)}')
    test_atomic_publish(source)
    test_dead_series_local_only(source)
    print('all checks passed')
