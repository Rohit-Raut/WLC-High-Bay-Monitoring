#!/usr/bin/env python3
"""
WLC High Bay Clean Room - Environmental Alert System
=====================================================
Reads the latest measurements from the live CSV written by particle_plus.py
and sends an email alert when any monitored parameter crosses a threshold.

Run this script on a cron schedule (e.g., every 10 minutes):
    */10 * * * * python3 /home/rraut/particle_plus/features/alerts/alerts.py

Alert conditions (all configurable below):
    - Relative humidity < RH_LOW_PCT  (dry air risk)
    - Relative humidity > RH_HIGH_PCT (condensation risk)
    - Temperature > TEMP_HIGH_F       (thermal excursion)
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

# Paths (must match particle_plus.py config)
BASE_DIR    = '/home/rraut/particle_plus'
LIVE_CSV    = f'{BASE_DIR}/data/live.csv'
MEAS_CSV    = f'{BASE_DIR}/data/measurements.csv'
STATE_FILE  = f'{BASE_DIR}/data/alert_state.json'
LOG_FILE    = f'{BASE_DIR}/alert_log.txt'

# Alert thresholds
RH_LOW_PCT          = 30.0    # % - dry air / static risk
RH_HIGH_PCT         = 60.0    # % - condensation / mold risk
TEMP_HIGH_F         = 80.0    # degF - thermal excursion
PARTICLE_HIGH_M3    = 100000  # counts/m³ at 0.3 µm - contamination event
OFFLINE_ALERT_MIN   = 90      # minutes without a new record before alerting

# Email settings
SMTP_HOST       = 'smtp.gmail.com'
SMTP_PORT       = 465                          # SSL port
EMAIL_SENDER    = 'your.sender@gmail.com'      # Gmail address that sends alerts
EMAIL_PASSWORD  = 'xxxx xxxx xxxx xxxx'        # Gmail app password (16 chars, spaces ok)
EMAIL_RECIPIENTS = [
    'your.name@yale.edu',                      # add more addresses as needed
]

ALERT_SUBJECT_PREFIX = '[WLC Clean Room]'

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


def cooldown_expired(state, key):
    """Return True if enough time has passed since the last alert for this key."""
    last = state.get(key)
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return datetime.now() - last_dt > timedelta(hours=COOLDOWN_HOURS)
    except Exception:
        return True


def send_email(subject, body):
    """Send a plain-text alert email via SMTP SSL."""
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

    # Particle count from latest completed sample (not live, which is mid-sample)
    ch1_m3  = safe_float(meas.get('ch1_diff_m3')) if meas else None

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

    save_state(state)
    if not fired:
        log("All parameters within normal range.")


if __name__ == '__main__':
    check_alerts()
