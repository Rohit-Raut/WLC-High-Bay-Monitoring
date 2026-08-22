#!/usr/bin/env python3
"""
local_serve.py — noether-only FULL-HISTORY dashboard server.

Unlike the public GitHub Pages dashboard (30-day window), this serves the
complete local measurement archive — including data that must never leave
noether (and, later, coldbox / slow-control datasets too large to publish).

SECURITY MODEL
    The server binds STRICTLY to 127.0.0.1 — it is never reachable from the
    network, even from other machines on the lab LAN. To view it from your
    own computer, forward the port over SSH:

        ssh -L 8800:localhost:8800 <user>@noether
        # then open  http://localhost:8800  in your local browser

    Do NOT change the bind address to 0.0.0.0 / '' — that would expose the
    archive to anyone who can reach the machine.

BEHAVIOR
    * Regenerates index_local.html every REGEN_INTERVAL_S seconds from the full
      archive (data/measurement_archive.csv), so the page's auto-reload always
      shows fresh data while the daemon keeps sampling. Keep that constant in
      step with AUTO_REFRESH_MS in features/dashboard/chart_interactions_local.js
      — a page that reloads faster than the rebuild just re-renders stale data.
    * index_local.html is gitignored — it never reaches GitHub.
    * Reuses particle_plus.generate_dashboard_html() with days=None /
      env_days=None / local=True (extended time ranges incl. "All data",
      LOCAL header badge, binning available on every window).
    * The running daemon (particle_plus.py --all) is untouched; this script
      only READS the data files.

Usage (on noether, e.g. inside tmux):
    python3 local_serve.py              # default port 8800
    python3 local_serve.py --port 9000
"""

import argparse
import gzip
import os
import sys
import threading
import time
import http.server

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
BIND_ADDR = '127.0.0.1'          # loopback ONLY — see security note above
LOCAL_HTML_NAME = 'index_local.html'
LOCAL_HTML = os.path.join(BASE_DIR, LOCAL_HTML_NAME)
REGEN_INTERVAL_S = 300           # 5 min — the Shellys only report every ~5 min,
                                 # so rebuilding faster just regenerates the same
                                 # page. Keep in step with AUTO_REFRESH_MS in
                                 # features/dashboard/chart_interactions_local.js.

# gzip level 6: measured on the real 45k-record page (19.6 MB) it gives 8.4x in
# 0.37 s. Level 9 costs 2.5 s for only 9 % more, level 1 gives 7.4x — 6 is the
# knee. The page is viewed over an SSH tunnel, where 19.6 MB is the entire
# reason a refresh takes seconds; 2.3 MB is not.
GZIP_LEVEL = 6

sys.path.insert(0, BASE_DIR)
import particle_plus as pp


def csv_source():
    """Full archive if present (noether), else the 30-day live file."""
    return pp.ARCHIVE_CSV if os.path.exists(pp.ARCHIVE_CSV) else pp.LIVE_CSV


def rebuild():
    """Regenerate index_local.html over the FULL history."""
    src = csv_source()
    print(f'[local] Rebuilding {LOCAL_HTML_NAME} from {os.path.basename(src)} (full history) …')
    return pp.generate_dashboard_html(src, LOCAL_HTML,
                                      days=None, env_days=None, local=True)


def _regen_loop():
    """Background thread: refresh the page every REGEN_INTERVAL_S seconds."""
    while True:
        time.sleep(REGEN_INTERVAL_S)
        try:
            rebuild()
        except Exception as e:                          # keep serving old page
            print(f'[local] WARNING: rebuild failed: {e}')


# ── gzip cache ────────────────────────────────────────────────────────────────
# SimpleHTTPRequestHandler sends everything uncompressed. The page is mostly
# JSON digits, which gzip crushes ~8.4x, and it is read over an SSH tunnel where
# that difference is seconds per refresh.
#
# Compressed once per rebuild rather than once per request: the file only
# changes every REGEN_INTERVAL_S, but several people may be watching at once,
# and re-running a 0.37 s compression for every one of them would be silly.
# Keyed on (mtime_ns, size), which generate_dashboard_html's os.replace() bumps
# on every rebuild, so the cache cannot go stale.
_gz_cache = None                 # ((mtime_ns, size), gzipped bytes)
_gz_lock  = threading.Lock()


def _gzipped_page():
    """Gzip of index_local.html, recompressed only when the file changes."""
    global _gz_cache
    try:
        stat = os.stat(LOCAL_HTML)
    except OSError:
        return None                                  # no page yet — fall back
    key = (stat.st_mtime_ns, stat.st_size)

    cached = _gz_cache                               # fast path, no lock
    if cached is not None and cached[0] == key:
        return cached[1]

    with _gz_lock:
        if _gz_cache is None or _gz_cache[0] != key:  # re-check: another thread
            with open(LOCAL_HTML, 'rb') as f:         # may have just done it
                _gz_cache = (key, gzip.compress(f.read(), GZIP_LEVEL))
        return _gz_cache[1]


class LocalHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the repo dir but maps the root URL to index_local.html."""

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            if 'gzip' in self.headers.get('Accept-Encoding', ''):
                body = _gzipped_page()
                if body is not None:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Encoding', 'gzip')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self.path = '/' + LOCAL_HTML_NAME        # no gzip → serve the file
        return super().do_GET()

    def do_HEAD(self):
        if self.path in ('/', '/index.html'):
            self.path = '/' + LOCAL_HTML_NAME
        return super().do_HEAD()

    def log_message(self, fmt, *args):
        pass                                            # silence request logs


def serve(port):
    os.chdir(BASE_DIR)
    httpd = http.server.ThreadingHTTPServer((BIND_ADDR, port), LocalHandler)
    print(f'[local] Full-history dashboard → http://localhost:{port}')
    print(f'[local] Bound to {BIND_ADDR} only. From another machine:')
    print(f'[local]     ssh -L {port}:localhost:{port} <user>@noether')
    print(f'[local] Regenerating every {REGEN_INTERVAL_S} s. Ctrl-C to stop.')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n[local] Stopped.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='noether-only full-history dashboard')
    parser.add_argument('--port', type=int, default=8800,
                        help='Port on 127.0.0.1 (default: 8800)')
    args = parser.parse_args()

    if not rebuild():
        print('[local] WARNING: initial rebuild reported failure — serving anyway.')

    threading.Thread(target=_regen_loop, daemon=True).start()
    serve(args.port)
