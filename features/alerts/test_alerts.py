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


def run(series):
    """Run check_alerts() against `series`, returning the subjects it would send."""
    sent = []
    orig_send, orig_series, orig_state = (
        alerts.send_email, alerts.sensor_series, alerts.load_state)
    alerts.send_email    = lambda subject, body: (sent.append(subject), True)[1]
    alerts.sensor_series = lambda: series
    alerts.load_state    = lambda: {}            # every cooldown starts expired
    alerts.save_state    = lambda state: None    # never touch the real state file
    try:
        alerts.check_alerts()
    finally:
        alerts.send_email, alerts.sensor_series, alerts.load_state = (
            orig_send, orig_series, orig_state)
    return [s for s in sent if 'SENSOR' in s or 'OUT OF RANGE' in s]


def demo():
    # 20 degC / 45% reported a minute ago — healthy, says nothing
    assert run([_series('Storage', 0.02, 20.0, 45.0)]) == [], 'healthy sensor alerted'

    # silent past SENSOR_SILENT_HOURS — one silence alert
    out = run([_series('Storage', alerts.SENSOR_SILENT_HOURS + 1, 20.0, 45.0)])
    assert len(out) == 1 and out[0].startswith('SENSOR SILENT'), out

    # 2 degC (~36 degF) — below TEMP_LOW_F 40: the door-open-in-winter case
    out = run([_series('Entrance', 0.02, 2.0, 45.0)])
    assert len(out) == 1 and out[0].startswith('OUT OF RANGE'), out

    # out of band BUT silent → reported as silent only, never as a live reading
    out = run([_series('Entrance', alerts.SENSOR_SILENT_HOURS + 1, 2.0, 45.0)])
    assert len(out) == 1 and out[0].startswith('SENSOR SILENT'), out

    # configured but never seen (no rows) — logged, never mailed
    assert run([{'name': 'CF Prep', 'ts': [], 'temp': [], 'rh': []}]) == [], \
        'a sensor that never reported must not mail'

    # humidity band, and both ends of temperature
    assert run([_series('Storage', 0.02, 20.0, 92.0)])[0].startswith('OUT OF RANGE')
    assert run([_series('Storage', 0.02, 20.0, 9.0)])[0].startswith('OUT OF RANGE')
    assert run([_series('Storage', 0.02, 35.0, 45.0)])[0].startswith('OUT OF RANGE')

    print('OK — sensor alert conditions behave as specified')


if __name__ == '__main__':
    demo()
