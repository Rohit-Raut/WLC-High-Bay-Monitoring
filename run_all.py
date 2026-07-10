#!/usr/bin/env python3
"""
Launch the whole monitoring stack in ONE tmux session ('wlc'), one window
per program, without touching any of the existing scripts:

    window 0  counter    python3 particle_plus.py --all
    window 1  server     python3 local_serve.py --port=9900
    window 2  ht-logger  python3 features/temp_humidity_sensor/shelly_ht_logger.py

Usage (on noether, after stopping any old sessions running these programs):

    python3 run_all.py          # creates the session, detached
    tmux attach -t wlc          # watch; Ctrl-b 0/1/2 switches windows

The script only sets up tmux and exits — the programs keep running inside
the session. If a program dies, its window stays open showing the traceback
(a fresh shell is left behind so the scrollback survives); restart just that
one by re-running its command in the window, or kill the session and rerun:

    tmux kill-session -t wlc && python3 run_all.py

It refuses to run if a 'wlc' session already exists, and warns (never kills)
if it sees one of the programs already running outside the session.
"""

import os
import subprocess
import sys

SESSION  = 'wlc'
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# window name → command (run from the repo root)
PROGRAMS = [
    ('counter',   'python3 particle_plus.py --all'),
    ('server',    'python3 local_serve.py --port=9900'),
    ('ht-logger', 'python3 features/temp_humidity_sensor/shelly_ht_logger.py'),
]


def tmux(*args, check=True):
    return subprocess.run(['tmux', *args], check=check,
                          capture_output=True, text=True)


def session_exists():
    # '=' prefix = exact-match, so 'wlc' doesn't match a 'wlc-old' session
    return tmux('has-session', '-t', f'={SESSION}', check=False).returncode == 0


def warn_if_already_running(cmd):
    script = next((w for w in cmd.split() if w.endswith('.py')), None)
    if script is None:
        return
    r = subprocess.run(['pgrep', '-f', script], capture_output=True, text=True)
    pids = [p for p in r.stdout.split() if p != str(os.getpid())]
    if pids:
        print(f"  ! {script} already running (pid {', '.join(pids)}) — "
              f"you may be double-starting it")


def main():
    if session_exists():
        sys.exit(f"tmux session '{SESSION}' already exists — attach with "
                 f"'tmux attach -t {SESSION}' or remove it first with "
                 f"'tmux kill-session -t {SESSION}'")

    for name, cmd in PROGRAMS:
        warn_if_already_running(cmd)
        # keep the window (and its scrollback) alive if the program exits
        wrapped = (f"{cmd}; echo; "
                   f"echo '[{name} exited — scroll up for output, window kept open]'; "
                   f"exec bash")
        if name == PROGRAMS[0][0]:
            tmux('new-session', '-d', '-s', SESSION, '-n', name,
                 '-c', REPO_DIR, wrapped)
        else:
            tmux('new-window', '-t', f'{SESSION}:', '-n', name,
                 '-c', REPO_DIR, wrapped)
        # pin the window name so tmux doesn't rename it after the command
        tmux('set-option', '-t', f'{SESSION}:{name}', 'automatic-rename', 'off',
             check=False)

    print(f"created tmux session '{SESSION}':")
    for i, (name, cmd) in enumerate(PROGRAMS):
        print(f"  window {i}: {name:<10} → {cmd}")
    print(f"\nattach with:  tmux attach -t {SESSION}   (Ctrl-b 0/1/2 to switch)")


if __name__ == '__main__':
    main()
