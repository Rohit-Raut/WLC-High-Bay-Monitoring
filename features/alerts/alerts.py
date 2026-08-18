#!/usr/bin/env python3
"""
WLC High Bay Clean Room - Environmental Alert System
=====================================================
Reads the latest measurements from the live CSV written by particle_plus.py
and sends an email alert when any monitored parameter crosses a threshold.

Run this script on a cron schedule (e.g., every 10 minutes):
    */10 * * * * python3 /home/rraut/particle_plus/features/alerts/alerts.py

Alert conditions (all configurable below):
    - Relative humidity < RH_LOW_PCT   (default < 20%, dry air / static risk)
    - Relative humidity > RH_HIGH_PCT  (default > 90%, condensation / moisture risk)
    - Temperature < TEMP_LOW_F         (default < 33 degF, abnormal cold)
    - Temperature > TEMP_HIGH_F        (default > 120 degF, thermal excursion)
    - Particle count (0.3 µm) > PARTICLE_HIGH_M3 (contamination event)
    - Counter offline for > OFFLINE_ALERT_MIN minutes

Email is sent via SMTP (Gmail app password by default). A state file
prevents repeat alerts; each condition must recover before re-triggering.
"""

import csv
import json
import os
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

# Paths. BASE_DIR is derived from this file's location (features/alerts/ -> repo
# root) so the script runs unmodified on noether, on a laptop, or from cron with
# any working directory.
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The permanent archive lives OUTSIDE the repo (config paths.project_data_dir,
# e.g. /project/dune/slow_control/... on noether) and falls back to the repo's
# data/ dir where that path doesn't exist — the same resolution particle_plus.py
# does, reusing the same loader so the two can't drift apart.
try:
    sys.path.insert(0, BASE_DIR)
    from features.config_loader import load_config
    _PROJECT_DATA_DIR = ((load_config(BASE_DIR).get('paths') or {})
                         .get('project_data_dir') or '')
except Exception:
    _PROJECT_DATA_DIR = ''
ARCHIVE_DIR = _PROJECT_DATA_DIR if os.path.isdir(_PROJECT_DATA_DIR) else f'{BASE_DIR}/data'

LIVE_CSV    = f'{BASE_DIR}/data/live.csv'
# measurement_archive.csv, NOT the legacy measurements.csv — nothing has written
# that file since the June 2026 migration (data_manager.migrate_old_files), so
# reading it meant the particle check re-evaluated one frozen row forever and the
# offline check saw a months-old timestamp and mailed "COUNTER OFFLINE" endlessly.
MEAS_CSV    = f'{ARCHIVE_DIR}/measurement_archive.csv'
STATE_FILE  = f'{BASE_DIR}/data/alert_state.json'
LOG_FILE    = f'{BASE_DIR}/alert_log.txt'

# ── Alert thresholds ──────────────────────────────────────────────────────────
# These are EMERGENCY limits, deliberately wider than the dashboard's coloured
# bands in config.yaml: the dashboard warns, this wakes someone up. Only fire
# when something is genuinely wrong in the lab (HVAC dead, door left open in
# winter, contamination event) — not on ordinary drift.
RH_LOW_PCT          = 15.0    # % - severe static discharge risk
RH_HIGH_PCT         = 85.0    # % - condensation on detector surfaces
TEMP_LOW_F          = 40.0    # degF - door left open in winter / heating failure
TEMP_HIGH_F         = 90.0    # degF - no clean room should ever reach this
# ISO 14644-1 stops at class 9 — there is no class 10, so "off the scale" means
# ABOVE the ISO 9 limit. The standard defines no 0.3 µm limit for classes 7-9,
# so this uses the class formula 10^9 x (0.1/0.3)^2.08 ~= 102M /m³ (CUMULATIVE
# >= 0.3 µm counts). The dashboard already reds at the ISO 9 line; this fires
# only once the room is dirtier than the worst classified level.
PARTICLE_HIGH_M3    = 102_000_000  # counts/m³ cumulative at 0.3 µm - worse than ISO 9
OFFLINE_ALERT_MIN   = 90      # minutes without a new record before alerting

# ── Distributed Shelly H&T sensors (features/temp_humidity_sensor) ────────────
# The Shellys sit at fixed locations around the High Bay and report every ~5 min.
# They catch what the counter alone cannot: a loading door left open in winter
# cools one end of the bay long before the counter's own sensor notices.
SENSOR_SILENT_HOURS = 24.0    # hours without a report before alerting (dead battery,
                              # broker outage). Well above the ~5 min report cadence,
                              # so a few missed wake-ups never trigger it.
SENSOR_SILENT_COOLDOWN_H = 24.0   # a dead sensor stays dead — re-mail daily, not 2-hourly

# Email settings
SMTP_HOST = 'smtp.gmail.com'
SMTP_PORT = 465   # SSL port

# Credentials are loaded from alerts_secrets.py (gitignored, lives only on noether).
# Copy alerts_secrets.example.py -> alerts_secrets.py and fill in your values.
# If alerts_secrets.py is missing, falls back to environment variables:
#   EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENTS (comma-separated).
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from alerts_secrets import EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENTS
except ImportError:
    import os as _os
    EMAIL_SENDER     = _os.environ.get('EMAIL_SENDER', '')
    EMAIL_PASSWORD   = _os.environ.get('EMAIL_PASSWORD', '')
    _rcpt            = _os.environ.get('EMAIL_RECIPIENTS', '')
    EMAIL_RECIPIENTS = [r.strip() for r in _rcpt.split(',') if r.strip()]
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECIPIENTS:
        print("ERROR: Email credentials not configured.")
        print("  Copy features/alerts/alerts_secrets.example.py to")
        print("  features/alerts/alerts_secrets.py and fill in your Gmail details.")
        raise SystemExit(1)

# Google displays an App Password as four space-separated groups
# ("abcd efgh ijkl mnop") and accepts it with or without them, but a space or
# newline picked up from a copy-paste or an editor fails as 535 BadCredentials,
# which reads exactly like a wrong password. Normalise it here.
EMAIL_PASSWORD = (EMAIL_PASSWORD or '').replace(' ', '').strip()

ALERT_SUBJECT_PREFIX = '[WLC Clean Room]'

# --dry-run: report what would be mailed, send nothing, and leave the cooldown
# state file untouched (a dry run that recorded cooldowns would silence the real
# cron run that follows it).
DRY_RUN = False

# Cooldown: once an alert fires, do not re-fire the SAME condition for this long
COOLDOWN_HOURS = 2

# ──────────────────────────────────────────────────────────────────────────────


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def cooldown_expired(state, key, hours=None):
    """Return True if enough time has passed since the last alert for this key."""
    if DRY_RUN:
        return True     # a dry run must show every active condition, never hide
    last = state.get(key)
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return datetime.now() - last_dt > timedelta(
            hours=COOLDOWN_HOURS if hours is None else hours)
    except Exception:
        return True


def send_email(subject, body):
    """Send a plain-text alert email via SMTP SSL.

    With --dry-run nothing is sent: the alert is logged instead and reported as
    delivered, so a run on real data shows exactly who would have been mailed
    and why. Use it to verify thresholds before adding the cron entry.
    """
    if DRY_RUN:
        log(f"DRY RUN — would send to {', '.join(EMAIL_RECIPIENTS)}: {subject}")
        for line in body.strip().splitlines():
            log(f"  | {line}")
        return True

    msg = EmailMessage()
    msg['Subject'] = f'{ALERT_SUBJECT_PREFIX} {subject}'
    msg['From']    = EMAIL_SENDER
    msg['To']      = ', '.join(EMAIL_RECIPIENTS)
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        log(f"Alert email sent: {subject}")
        return True
    except Exception as e:
        log(f"ERROR sending email: {e}")
        return False


def send_test_email():
    """One-off delivery check — proves credentials and routing work, nothing else.

    Deliberately separate from check_alerts(): it touches no thresholds, reads no
    data and records no cooldown, so testing delivery can never leave the alert
    system in a changed state.
    """
    body = (
        "This is a test message from the WLC High Bay alert system.\n\n"
        "If you are reading this, the sender credentials and the recipient list\n"
        "are correct, and real alerts will reach you.\n\n"
        "If it arrived in spam, mark it 'not spam' NOW — otherwise every future\n"
        "alert is filed away silently, which is worse than no alerting at all.\n\n"
        f"Sent:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"From:   {EMAIL_SENDER}\n"
        f"To:     {', '.join(EMAIL_RECIPIENTS)}\n"
        f"SMTP:   {SMTP_HOST}:{SMTP_PORT}\n"
    )
    ok = send_email('TEST - alert system configured correctly', body)
    log('Test email sent — check the inbox AND the spam folder.' if ok else
        'Test email FAILED — see the SMTP error above.')
    return ok


def fire(state, key, subject, body, hours=None):
    """Send one alert if its cooldown has expired; record the time if it went out.

    Same cooldown/state contract the counter checks above use inline — factored
    out here because the per-location sensor checks would otherwise repeat it
    once per sensor per condition.
    """
    if not cooldown_expired(state, key, hours):
        log(f"{key} active but cooldown not expired")
        return False
    if send_email(subject, body):
        state[key] = datetime.now().isoformat()
        return True
    return False


def sensor_series():
    """Per-location Shelly series via the dashboard's own reader (never raises).

    Returns [] if the reader, its config, or the csv is unavailable — a missing
    sensor pipeline must never stop the counter alerts from running.
    """
    try:
        from features.temp_humidity_sensor.reader import load_sensor_series
        return load_sensor_series()
    except Exception as e:
        log(f"Sensor series unavailable ({e}) — skipping sensor checks")
        return []


def read_latest_live():
    """Return the most recent row from live.csv as a dict, or None."""
    if not os.path.exists(LIVE_CSV):
        return None
    try:
        with open(LIVE_CSV) as f:
            rows = list(csv.DictReader(f))
        return rows[-1] if rows else None
    except Exception:
        return None


def read_latest_measurement():
    """Return the most recent completed sample from measurements.csv, or None."""
    if not os.path.exists(MEAS_CSV):
        return None
    try:
        with open(MEAS_CSV) as f:
            rows = list(csv.DictReader(f))
        return rows[-1] if rows else None
    except Exception:
        return None


def safe_float(val):
    try:
        return float(val) if val not in (None, '', 'None') else None
    except (ValueError, TypeError):
        return None


def check_alerts():
    state   = load_state()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fired   = False

    live = read_latest_live()
    meas = read_latest_measurement()

    # Use live reading for RH and temperature (10-second resolution)
    # Fall back to latest completed sample if live CSV is unavailable
    source = live if live is not None else meas
    if source is None:
        log("No data available to check.")
        return

    rh      = safe_float(source.get('RH_pct'))
    temp_c  = safe_float(source.get('temp_C'))
    temp_f  = round(temp_c * 9/5 + 32, 1) if temp_c is not None else None

    # Particle count from latest completed sample (not live, which is mid-sample).
    # CUMULATIVE (>= 0.3 µm) count — ISO limits are defined on cumulative counts,
    # not the differential per-bin ch1_diff_m3.
    ch1_m3  = safe_float(meas.get('ch1_sum_m3')) if meas else None

    # Timestamp of latest measurement to check offline status
    last_meas_dt = None
    if meas:
        d = (meas.get('date') or '').strip()
        t = (meas.get('time') or '').strip()
        if d and t:
            try:
                last_meas_dt = datetime.strptime(f"{d} {t}", '%Y-%m-%d %H:%M:%S')
            except ValueError:
                pass
        if last_meas_dt is None:
            sync = (meas.get('sync_time') or '').strip()
            if sync:
                try:
                    last_meas_dt = datetime.fromisoformat(sync)
                except ValueError:
                    pass

    log(f"Check: RH={rh}%  Temp={temp_f}F  ch1={ch1_m3}/m³  "
        f"last_meas={last_meas_dt.strftime('%H:%M') if last_meas_dt else 'unknown'}")

    # ── RH LOW ────────────────────────────────────────────────────────────────
    if rh is not None and rh < RH_LOW_PCT:
        key = 'rh_low'
        if cooldown_expired(state, key):
            body = (
                f"ALERT: Low Relative Humidity\n\n"
                f"Current RH:   {rh:.1f}%\n"
                f"Threshold:    < {RH_LOW_PCT:.0f}%\n\n"
                f"Low humidity increases static discharge risk, which can damage\n"
                f"detector components during assembly. Verify clean room HVAC status.\n\n"
                f"Timestamp:    {now_str}\n"
                f"Location:     WLC High Bay (Wright Lab, Yale University)\n"
                f"Instrument:   Particles Plus Model 7301\n"
            )
            if send_email(f"LOW HUMIDITY: {rh:.1f}% (threshold {RH_LOW_PCT:.0f}%)", body):
                state[key] = datetime.now().isoformat()
                fired = True
        else:
            log(f"RH low ({rh:.1f}%) but cooldown active for 'rh_low'")
    else:
        # Clear cooldown once condition recovers
        state.pop('rh_low', None)

    # ── RH HIGH ───────────────────────────────────────────────────────────────
    if rh is not None and rh > RH_HIGH_PCT:
        key = 'rh_high'
        if cooldown_expired(state, key):
            body = (
                f"ALERT: High Relative Humidity\n\n"
                f"Current RH:   {rh:.1f}%\n"
                f"Threshold:    > {RH_HIGH_PCT:.0f}%\n\n"
                f"High humidity can cause condensation on detector surfaces and\n"
                f"increase particle adhesion. Verify clean room HVAC status.\n\n"
                f"Timestamp:    {now_str}\n"
                f"Location:     WLC High Bay (Wright Lab, Yale University)\n"
                f"Instrument:   Particles Plus Model 7301\n"
            )
            if send_email(f"HIGH HUMIDITY: {rh:.1f}% (threshold {RH_HIGH_PCT:.0f}%)", body):
                state[key] = datetime.now().isoformat()
                fired = True
        else:
            log(f"RH high ({rh:.1f}%) but cooldown active for 'rh_high'")
    else:
        state.pop('rh_high', None)

    # ── TEMP LOW ──────────────────────────────────────────────────────────────
    if temp_f is not None and temp_f < TEMP_LOW_F:
        key = 'temp_low'
        if cooldown_expired(state, key):
            body = (
                f"ALERT: Low Temperature\n\n"
                f"Current temp: {temp_f:.1f} degF ({temp_c:.1f} degC)\n"
                f"Threshold:    < {TEMP_LOW_F:.0f} degF\n\n"
                f"Abnormally low temperature may indicate HVAC failure or\n"
                f"unintended cold exposure in the clean room. Verify environmental\n"
                f"controls and check that heating is functioning correctly.\n\n"
                f"Timestamp:    {now_str}\n"
                f"Location:     WLC High Bay (Wright Lab, Yale University)\n"
                f"Instrument:   Particles Plus Model 7301\n"
            )
            if send_email(f"LOW TEMPERATURE: {temp_f:.1f}F (threshold {TEMP_LOW_F:.0f}F)", body):
                state[key] = datetime.now().isoformat()
                fired = True
        else:
            log(f"Temp low ({temp_f:.1f}F) but cooldown active for 'temp_low'")
    else:
        state.pop('temp_low', None)

    # ── TEMP HIGH ─────────────────────────────────────────────────────────────
    if temp_f is not None and temp_f > TEMP_HIGH_F:
        key = 'temp_high'
        if cooldown_expired(state, key):
            body = (
                f"ALERT: High Temperature\n\n"
                f"Current temp: {temp_f:.1f} degF ({temp_c:.1f} degC)\n"
                f"Threshold:    > {TEMP_HIGH_F:.0f} degF\n\n"
                f"Elevated temperature may indicate HVAC failure or increased\n"
                f"thermal load in the clean room. Verify environmental controls.\n\n"
                f"Timestamp:    {now_str}\n"
                f"Location:     WLC High Bay (Wright Lab, Yale University)\n"
                f"Instrument:   Particles Plus Model 7301\n"
            )
            if send_email(f"HIGH TEMPERATURE: {temp_f:.1f}F (threshold {TEMP_HIGH_F:.0f}F)", body):
                state[key] = datetime.now().isoformat()
                fired = True
        else:
            log(f"Temp high ({temp_f:.1f}F) but cooldown active for 'temp_high'")
    else:
        state.pop('temp_high', None)

    # ── PARTICLE COUNT HIGH ───────────────────────────────────────────────────
    if ch1_m3 is not None and ch1_m3 > PARTICLE_HIGH_M3:
        key = 'particle_high'
        if cooldown_expired(state, key):
            body = (
                f"ALERT: Elevated Particle Count\n\n"
                f"0.3 µm channel: {ch1_m3:,.0f} counts/m³\n"
                f"Threshold:      > {PARTICLE_HIGH_M3:,} counts/m³\n\n"
                f"An elevated particle count at 0.3 µm may indicate a contamination\n"
                f"event, personnel activity, or filter degradation. Review the\n"
                f"dashboard for the full size distribution and trend.\n\n"
                f"Dashboard: https://rohit-raut.github.io/WLC-High-Bay-Monitoring/\n\n"
                f"Timestamp:    {now_str}\n"
                f"Location:     WLC High Bay (Wright Lab, Yale University)\n"
                f"Instrument:   Particles Plus Model 7301\n"
            )
            if send_email(f"HIGH PARTICLE COUNT: {ch1_m3:,.0f} /m³ at 0.3µm", body):
                state[key] = datetime.now().isoformat()
                fired = True
        else:
            log(f"Particle high ({ch1_m3:,.0f}/m³) but cooldown active")
    else:
        state.pop('particle_high', None)

    # ── COUNTER OFFLINE ───────────────────────────────────────────────────────
    if last_meas_dt is not None:
        offline_min = (datetime.now() - last_meas_dt).total_seconds() / 60
        if offline_min > OFFLINE_ALERT_MIN:
            key = 'counter_offline'
            if cooldown_expired(state, key):
                body = (
                    f"ALERT: Particle Counter Appears Offline\n\n"
                    f"Last measurement: {last_meas_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Time since last record: {offline_min:.0f} minutes\n"
                    f"Threshold: > {OFFLINE_ALERT_MIN} minutes\n\n"
                    f"The particle counter has not produced a new record for an\n"
                    f"extended period. Check that particle_plus.py is running on\n"
                    f"noether (tmux session 'particle') and that the counter is\n"
                    f"powered and reachable at 10.66.66.68:502.\n\n"
                    f"Dashboard: https://rohit-raut.github.io/WLC-High-Bay-Monitoring/\n\n"
                    f"Timestamp:    {now_str}\n"
                    f"Location:     WLC High Bay (Wright Lab, Yale University)\n"
                )
                if send_email(f"COUNTER OFFLINE: no data for {offline_min:.0f} min", body):
                    state[key] = datetime.now().isoformat()
                    fired = True
            else:
                log(f"Counter offline ({offline_min:.0f} min) but cooldown active")
        else:
            state.pop('counter_offline', None)

    # ── DISTRIBUTED SHELLY SENSORS ────────────────────────────────────────────
    # Two conditions per location: gone silent, and last reading out of band.
    # Locations are keyed by name so each one carries its own cooldown.
    #
    # A sensor that has NEVER reported is logged, not mailed: that is a
    # deployment/config gap (a prefix in sensors.yaml with no device behind it),
    # and mailing it would repeat forever with nothing anyone can fix by email.
    for s in sensor_series():
        loc = s.get('name') or 'unknown'
        if not s.get('ts'):
            log(f"Sensor '{loc}' has never reported — not alerting (check sensors.yaml)")
            continue

        try:
            last_dt = datetime.strptime(s['ts'][-1], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            log(f"Sensor '{loc}' has an unparseable timestamp — skipping")
            continue
        silent_h = (datetime.now() - last_dt).total_seconds() / 3600.0

        # ── sensor silent ─────────────────────────────────────────────────────
        if silent_h > SENSOR_SILENT_HOURS:
            key = f'sensor_silent:{loc}'
            body = (
                f"ALERT: Sensor Not Reporting\n\n"
                f"Location:     {loc}\n"
                f"Last report:  {last_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Silent for:   {silent_h:.1f} hours\n"
                f"Threshold:    > {SENSOR_SILENT_HOURS:.0f} hours\n\n"
                f"This Shelly H&T has stopped reporting. Most likely a flat\n"
                f"battery; also check the MQTT broker and that\n"
                f"shelly_ht_logger.py is still running on noether (tmux session\n"
                f"'wlc', window 'ht-logger').\n\n"
                f"Timestamp:    {now_str}\n"
                f"Location:     WLC High Bay (Wright Lab, Yale University)\n"
            )
            if fire(state, key,
                    f"SENSOR SILENT: {loc} ({silent_h:.0f} h)", body,
                    hours=SENSOR_SILENT_COOLDOWN_H):
                fired = True
            continue          # a silent sensor's last reading is not news

        state.pop(f'sensor_silent:{loc}', None)

        # ── sensor out of band ────────────────────────────────────────────────
        # Only reached while the sensor is reporting, so the values are current.
        # The Shellys log °C; thresholds are °F.
        s_tc = next((v for v in reversed(s.get('temp') or []) if v is not None), None)
        s_rh = next((v for v in reversed(s.get('rh')   or []) if v is not None), None)
        s_tf = round(s_tc * 9 / 5 + 32, 1) if s_tc is not None else None

        reasons = []
        if s_tf is not None and s_tf < TEMP_LOW_F:
            reasons.append(f"temperature {s_tf:.1f} degF ({s_tc:.1f} degC) "
                           f"below {TEMP_LOW_F:.0f} degF")
        if s_tf is not None and s_tf > TEMP_HIGH_F:
            reasons.append(f"temperature {s_tf:.1f} degF ({s_tc:.1f} degC) "
                           f"above {TEMP_HIGH_F:.0f} degF")
        if s_rh is not None and s_rh < RH_LOW_PCT:
            reasons.append(f"humidity {s_rh:.1f}% below {RH_LOW_PCT:.0f}%")
        if s_rh is not None and s_rh > RH_HIGH_PCT:
            reasons.append(f"humidity {s_rh:.1f}% above {RH_HIGH_PCT:.0f}%")

        key = f'sensor_band:{loc}'
        if reasons:
            body = (
                f"ALERT: Environment Out of Range at {loc}\n\n"
                + ''.join(f"  - {r}\n" for r in reasons) +
                f"\nReading taken: {last_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"One location is outside the emergency band while the rest of\n"
                f"the bay may look normal — check for a door left open, a local\n"
                f"heat source, or an HVAC fault serving that area.\n\n"
                f"Timestamp:    {now_str}\n"
                f"Location:     WLC High Bay (Wright Lab, Yale University)\n"
                f"Sensor:       Shelly H&T Gen3 ({loc})\n"
            )
            if fire(state, key, f"OUT OF RANGE at {loc}: {reasons[0]}", body):
                fired = True
        else:
            state.pop(key, None)

    if not DRY_RUN:
        save_state(state)
    if not fired:
        log("All parameters within normal range.")


if __name__ == '__main__':
    # argparse, not a manual sys.argv scan: an unrecognised flag must fail loudly.
    # The manual version silently fell through to a REAL run, so a typo'd flag
    # sent live alert mail while appearing to do something safe.
    import argparse

    _p = argparse.ArgumentParser(
        description='WLC High Bay environmental alerts. '
                    'With no options: check every condition and email any that fire. '
                    'See features/alerts/README.md.')
    _p.add_argument('--dry-run', action='store_true',
                    help='report what WOULD be sent and send nothing: cooldowns are '
                         'neither recorded nor respected, so every active condition '
                         'is shown')
    _p.add_argument('--test-email', action='store_true',
                    help='send one test message to confirm delivery works, then exit '
                         '(reads no data, changes no state)')
    _args = _p.parse_args()

    DRY_RUN = _args.dry_run

    if _args.test_email:
        raise SystemExit(0 if send_test_email() else 1)

    if DRY_RUN:
        log('DRY RUN — sending nothing; cooldowns neither recorded nor respected')
    check_alerts()
