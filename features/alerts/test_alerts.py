#!/usr/bin/env python3
"""Self-check for the Shelly sensor alert conditions in alerts.py.

The counter checks are exercised every time the script runs for real; the two
sensor conditions are not, because they need a sensor that is silent or out of
band. This stubs the sensor reader with synthetic series and asserts which
alerts come out.

    python3 features/alerts/test_alerts.py        # prints OK or raises

No framework, no fixtures — nothing is sent, nothing is written.
"""

import os
import sys
from datetime import datetime, timedelta

# credentials must exist before importing alerts (it exits if they don't)
os.environ.setdefault('EMAIL_SENDER', 'selfcheck@example.com')
os.environ.setdefault('EMAIL_PASSWORD', 'unused')
os.environ.setdefault('EMAIL_RECIPIENTS', 'selfcheck@example.com')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alerts


def _series(name, ago_hours, temp_c, rh):
    ts = (datetime.now() - timedelta(hours=ago_hours)).strftime('%Y-%m-%d %H:%M:%S')
    return {'name': name, 'ts': [ts], 'temp': [temp_c], 'rh': [rh]}


def run(series, state=None):
    """Run check_alerts() against `series`, returning the subjects it would send.

    `state` seeds the cooldown file (default: empty, so every condition is free
    to fire).
    """
    sent = []
    orig_send, orig_series, orig_state = (
        alerts.send_email, alerts.sensor_series, alerts.load_state)
    alerts.send_email    = lambda subject, body: (sent.append(subject), True)[1]
    alerts.sensor_series = lambda: series
    alerts.load_state    = lambda: dict(state or {})
    alerts.save_state    = lambda s: None        # never touch the real state file
    try:
        alerts.check_alerts()
    finally:
        alerts.send_email, alerts.sensor_series, alerts.load_state = (
            orig_send, orig_series, orig_state)
    return [s for s in sent if 'SENSOR' in s or 'OUT OF RANGE' in s]


def _f_to_c(f):
    return (f - 32) * 5 / 9


def demo():
    # Fixtures are derived from the CONFIGURED thresholds, not hardcoded numbers,
    # so this checks the logic rather than today's tuning — retuning a threshold
    # (or leaving a test value like TEMP_LOW_F = 80 behind) must not fail it.
    mid_c   = _f_to_c((alerts.TEMP_LOW_F + alerts.TEMP_HIGH_F) / 2)
    mid_rh  = (alerts.RH_LOW_PCT + alerts.RH_HIGH_PCT) / 2
    cold_c  = _f_to_c(alerts.TEMP_LOW_F - 10)
    hot_c   = _f_to_c(alerts.TEMP_HIGH_F + 10)
    dry_rh  = alerts.RH_LOW_PCT - 5
    wet_rh  = alerts.RH_HIGH_PCT + 5
    silent  = alerts.SENSOR_SILENT_HOURS + 1

    # mid-band reading a minute ago — healthy, says nothing
    assert run([_series('Storage', 0.02, mid_c, mid_rh)]) == [], 'healthy sensor alerted'

    # silent past SENSOR_SILENT_HOURS — one silence alert
    out = run([_series('Storage', silent, mid_c, mid_rh)])
    assert len(out) == 1 and out[0].startswith('SENSOR SILENT'), out

    # below TEMP_LOW_F — the door-left-open-in-winter case
    out = run([_series('Entrance', 0.02, cold_c, mid_rh)])
    assert len(out) == 1 and out[0].startswith('OUT OF RANGE'), out

    # out of band BUT silent → reported as silent only, never as a live reading
    out = run([_series('Entrance', silent, cold_c, mid_rh)])
    assert len(out) == 1 and out[0].startswith('SENSOR SILENT'), out

    # configured but never seen (no rows) — logged, never mailed
    assert run([{'name': 'CF Prep', 'ts': [], 'temp': [], 'rh': []}]) == [], \
        'a sensor that never reported must not mail'

    # the other three band edges
    assert run([_series('Storage', 0.02, mid_c, wet_rh)])[0].startswith('OUT OF RANGE')
    assert run([_series('Storage', 0.02, mid_c, dry_rh)])[0].startswith('OUT OF RANGE')
    assert run([_series('Storage', 0.02, hot_c,  mid_rh)])[0].startswith('OUT OF RANGE')

    # --dry-run must ignore an active cooldown, or it would report "nothing wrong"
    # about a condition that is very much wrong
    just_fired = {'sensor_band:Entrance': datetime.now().isoformat()}
    assert run([_series('Entrance', 0.02, cold_c, mid_rh)], just_fired) == [], \
        'a fresh cooldown should suppress a normal run'

    alerts.DRY_RUN = True
    try:
        out = run([_series('Entrance', 0.02, cold_c, mid_rh)], just_fired)
        assert len(out) == 1, f'dry run hid an active condition behind a cooldown: {out}'
    finally:
        alerts.DRY_RUN = False

    print('OK — sensor alert conditions behave as specified')


if __name__ == '__main__':
    demo()
