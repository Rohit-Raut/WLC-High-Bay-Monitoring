#!/usr/bin/env python3
"""
Self-check for local_serve.py's gzip transfer encoding. No framework, just asserts:

    python3 test_local_serve.py

The dashboard is read over an SSH tunnel, where the uncompressed page (19.6 MB
at 45k records) is the entire reason a refresh takes seconds. SimpleHTTPRequest-
Handler never compresses, so local_serve adds it — served from a cache keyed on
the file's mtime+size so a 0.37 s compression isn't repeated per viewer.

Checks: a gzip-capable client gets a correctly-encoded, smaller response whose
decompressed bytes are byte-identical to the file; a client that does NOT accept
gzip still gets the plain page; and the cache re-compresses after a rebuild
instead of serving the previous page forever.
"""

import gzip
import http.client
import os
import sys
import threading
import http.server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import local_serve as ls

PORT = 8899
failures = 0


def check(name, cond, detail=''):
    global failures
    if cond:
        print(f'  ok  {name}{detail}')
    else:
        print(f'  FAIL {name}{detail}')
        failures += 1


def get(accept_encoding):
    """GET / and return (status, headers, raw body bytes)."""
    conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=60)
    headers = {'Accept-Encoding': accept_encoding} if accept_encoding else {}
    conn.request('GET', '/', headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), body


def main():
    if not os.path.exists(ls.LOCAL_HTML):
        print('index_local.html missing — building it first')
        ls.rebuild()
    disk = open(ls.LOCAL_HTML, 'rb').read()

    httpd = http.server.ThreadingHTTPServer((ls.BIND_ADDR, PORT), ls.LocalHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    os.chdir(BASE_DIR)                      # LocalHandler serves the cwd
    try:
        status, hdrs, body = get('gzip, deflate')
        check('gzip request → 200', status == 200)
        check('Content-Encoding: gzip', hdrs.get('Content-Encoding') == 'gzip')
        check('Content-Length matches body',
              int(hdrs.get('Content-Length', -1)) == len(body))
        # http.client does NOT auto-decompress, so body is the raw gzip stream.
        check('decompresses to exactly the file on disk',
              gzip.decompress(body) == disk)
        ratio = len(disk) / max(len(body), 1)
        check('smaller than the raw page', ratio > 2,
              f'  ({len(disk):,} → {len(body):,} bytes, {ratio:.1f}x)')

        status, hdrs, plain = get(None)
        check('non-gzip client → 200', status == 200)
        check('non-gzip client gets no Content-Encoding',
              'Content-Encoding' not in hdrs)
        check('non-gzip client gets the whole page', plain == disk)

        # Cache invalidation: a rebuild must not keep serving the old bytes.
        # Touching the file the way generate_dashboard_html does (write + replace)
        # changes mtime and size, which is what the cache is keyed on.
        before = get('gzip')[2]
        marker = b'<!--cache-invalidation-probe-->'
        tmp = ls.LOCAL_HTML + '.probe'
        with open(tmp, 'wb') as f:
            f.write(disk + marker)
        os.replace(tmp, ls.LOCAL_HTML)
        after = get('gzip')[2]
        try:
            check('rebuild invalidates the gzip cache',
                  after != before and gzip.decompress(after).endswith(marker))
        finally:
            with open(ls.LOCAL_HTML, 'wb') as f:   # restore the real page
                f.write(disk)
    finally:
        httpd.shutdown()

    print('\nall checks passed' if failures == 0 else f'\n{failures} CHECK(S) FAILED')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
