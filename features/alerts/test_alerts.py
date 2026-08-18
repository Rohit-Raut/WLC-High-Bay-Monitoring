#!/usr/bin/env python3
"""Self-check for the alert ruleset in alerts.py.

    python3 features/alerts/test_alerts.py        # prints OK or raises

Two layers:
  * evaluate() is pure — readings in, findings out — so most of the ruleset is
    checked by calling it directly, with no mail server and no data files.
  * check_alerts() is checked only for the things evaluate() cannot show:
    that several conditions arrive as ONE email, and how cooldowns behave.

Fixtures derive from the CONFIGURED thresholds, so retuning a threshold (or
leaving a test value behind) never fails this. No framework, no fixtures,
nothing sent, nothing written.
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


def _f_to_c(f):
    return (f - 32) * 5 / 9


MID_TF  = (alerts.TEMP_LOW_F + alerts.TEMP_HIGH_F) / 2
MID_RH  = (alerts.RH_LOW_PCT + alerts.RH_HIGH_PCT) / 2
COLD_C  = _f_to_c(alerts.TEMP_LOW_F - 10)
HOT_C   = _f_to_c(alerts.TEMP_HIGH_F + 10)
DRY_RH  = alerts.RH_LOW_PCT - 5
WET_RH  = alerts.RH_HIGH_PCT + 5
SILENT  = alerts.SENSOR_SILENT_HOURS + 1


def sensor(name, silent_h=0.02, temp_c=None, rh=None, never=False):
    temp_c = _f_to_c(MID_TF) if temp_c is None else temp_c
    rh     = MID_RH if rh is None else rh
    return {'name': name, 'never': never,
            'last_dt': datetime.now() - timedelta(hours=silent_h),
            'silent_h': silent_h,
            'temp_c': temp_c, 'temp_f': round(temp_c * 9 / 5 + 32, 1), 'rh': rh}


def readings(sensors=(), **kw):
    """A healthy lab, overridable field by field."""
    r = {'have_data': True, 'rh': MID_RH, 'temp_c': _f_to_c(MID_TF), 'temp_f': MID_TF,
         'ch1_m3': 1000.0, 'last_meas_dt': datetime.now(), 'offline_min': 1.0,
         'sensors': list(sensors)}
    r.update(kw)
    return r


def keys(r):
    return sorted(c['key'] for c in alerts.evaluate(r))


def demo():
    # ── the ruleset, via the pure function ────────────────────────────────────
    assert keys(readings()) == [], 'a healthy lab raised an alert'

    assert keys(readings(temp_f=alerts.TEMP_LOW_F - 1)) == ['temp_low']
    assert keys(readings(temp_f=alerts.TEMP_HIGH_F + 1)) == ['temp_high']
    assert keys(readings(rh=alerts.RH_LOW_PCT - 1)) == ['rh_low']
    assert keys(readings(rh=alerts.RH_HIGH_PCT + 1)) == ['rh_high']
    assert keys(readings(ch1_m3=alerts.PARTICLE_HIGH_M3 + 1)) == ['particle_high']
    assert keys(readings(offline_min=alerts.OFFLINE_ALERT_MIN + 1)) == ['counter_offline']

    # thresholds are exclusive: exactly at the limit is still fine
    assert keys(readings(temp_f=alerts.TEMP_LOW_F)) == [], 'fired exactly at the limit'
    assert keys(readings(offline_min=alerts.OFFLINE_ALERT_MIN)) == []

    # ── the Shelly sensors ────────────────────────────────────────────────────
    assert keys(readings([sensor('Storage')])) == [], 'healthy sensor alerted'
    assert keys(readings([sensor('Storage', silent_h=SILENT)])) == ['sensor_silent:Storage']

    # the door-left-open-in-winter case
    assert keys(readings([sensor('Entrance', temp_c=COLD_C)])) == ['sensor_band:Entrance']
    assert keys(readings([sensor('Entrance', temp_c=HOT_C)])) == ['sensor_band:Entrance']
    assert keys(readings([sensor('Entrance', rh=DRY_RH)])) == ['sensor_band:Entrance']
    assert keys(readings([sensor('Entrance', rh=WET_RH)])) == ['sensor_band:Entrance']

    # out of band BUT silent → reported as silent only; a dead sensor's last
    # reading must never be presented as if it were current
    assert keys(readings([sensor('Entrance', silent_h=SILENT, temp_c=COLD_C)])) \
        == ['sensor_silent:Entrance']

    # configured but never seen → a deployment gap, logged and never mailed
    assert keys(readings([sensor('CF Prep', never=True)])) == []

    # ── the digest: everything wrong at once arrives as ONE mail ──────────────
    storm = readings([sensor('Entrance', temp_c=COLD_C),
                      sensor('Storage', silent_h=SILENT)],
                     temp_f=alerts.TEMP_LOW_F - 1,
                     offline_min=alerts.OFFLINE_ALERT_MIN + 1)
    found = keys(storm)
    assert found == ['counter_offline', 'sensor_band:Entrance',
                     'sensor_silent:Storage', 'temp_low'], found

    sent = _run_check(storm)
    assert len(sent) == 1, f'{len(sent)} emails for one event, expected a single digest'
    subject, body = sent[0]
    assert subject.startswith('4 ALERTS'), subject
    for k in ('LOW TEMPERATURE', 'COUNTER OFFLINE', 'SENSOR SILENT', 'OUT OF RANGE'):
        assert k in body, f'{k} missing from the digest body'
    assert 'CURRENT CONDITIONS' in body, 'summary block missing'
    assert 'Entrance' in body and 'Storage' in body, 'sensor rows missing from summary'

    # a single alert keeps its own descriptive subject
    sent = _run_check(readings(temp_f=alerts.TEMP_LOW_F - 1))
    assert sent[0][0].startswith('LOW TEMPERATURE:'), sent[0][0]

    # ── cooldowns ─────────────────────────────────────────────────────────────
    fresh = {'temp_low': datetime.now().isoformat()}
    assert _run_check(readings(temp_f=alerts.TEMP_LOW_F - 1), fresh) == [], \
        'a fresh cooldown should suppress a normal run'

    alerts.DRY_RUN = True
    try:
        assert len(_run_check(readings(temp_f=alerts.TEMP_LOW_F - 1), fresh)) == 1, \
            'dry run hid an active condition behind a cooldown'
    finally:
        alerts.DRY_RUN = False

    print('OK — alert ruleset, digest grouping and cooldowns behave as specified')


def _run_check(r, state=None):
    """Run check_alerts() against fixed readings; return [(subject, body), ...]."""
    sent = []
    orig = (alerts.send_email, alerts.gather_readings,
            alerts.load_state, alerts.save_state)
    alerts.send_email      = lambda s, b: (sent.append((s, b)), True)[1]
    alerts.gather_readings = lambda: r
    alerts.load_state      = lambda: dict(state or {})
    alerts.save_state      = lambda s: None      # never touch the real state file
    try:
        alerts.check_alerts()
    finally:
        (alerts.send_email, alerts.gather_readings,
         alerts.load_state, alerts.save_state) = orig
    return sent


if __name__ == '__main__':
    demo()
