#!/usr/bin/env python3
"""
WLC High Bay Clean Room - Environmental Alert System
=====================================================
Reads the latest measurements from the live CSV written by particle_plus.py
and sends an email alert when any monitored parameter crosses a threshold.

Run from cron — the check every 10 minutes, the summary once a week:
    */10 * * * * cd /home/rraut/particle_plus && python3 features/alerts/alerts.py
    0 8 * * 1    cd /home/rraut/particle_plus && python3 features/alerts/alerts.py --weekly-summary

Alert conditions (thresholds all configurable below). From the counter:
    - Relative humidity < RH_LOW_PCT or > RH_HIGH_PCT
    - Temperature < TEMP_LOW_F or > TEMP_HIGH_F
    - Cumulative >=0.3 µm count > PARTICLE_HIGH_M3 (worse than ISO 9)
    - No new record for > OFFLINE_ALERT_MIN minutes
From each distributed Shelly H&T sensor:
    - Silent for > SENSOR_SILENT_HOURS (flat battery, broker or logger down)
    - Last reading outside the same temp/RH limits, per location

Everything active in one run is sent as ONE digest email, followed by a table
of current readings from the counter and every sensor: a real HVAC failure
trips several conditions at once, and one mail showing the whole bay is more
actionable than four mails each showing a fragment. Cooldowns stay
per-condition, so a new problem still notifies immediately while another is
mid-cooldown, and a condition that recovers drops its cooldown so its next
occurrence is not delayed.

Email goes out over SMTP (Gmail app password by default). Shape of the code:
gather_readings() reads, evaluate() decides (pure — no I/O, no mail, which is
what makes the ruleset testable), check_alerts() delivers. See test_alerts.py.
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



# ── Reading the current state of the lab ─────────────────────────────────────

def _row_dt(row):
    """Timestamp of a measurement row: date+time, else sync_time. None if neither."""
    d = (row.get('date') or '').strip()
    t = (row.get('time') or '').strip()
    if d and t:
        try:
            return datetime.strptime(f"{d} {t}", '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
    sync = (row.get('sync_time') or '').strip()
    if sync:
        try:
            return datetime.fromisoformat(sync)
        except ValueError:
            pass
    return None


def gather_readings():
    """Everything the checks AND the summary need, read once.

    Keeping this separate from evaluate() means the summary block printed in
    every email is built from exactly the same numbers the alerts were judged
    on — the mail can never contradict itself.
    """
    live = read_latest_live()
    meas = read_latest_measurement()
    # live.csv for temp/RH (finer cadence); fall back to the last completed sample
    source = live if live is not None else meas

    r = {'have_data': source is not None, 'rh': None, 'temp_c': None, 'temp_f': None,
         'ch1_m3': None, 'last_meas_dt': None, 'offline_min': None, 'sensors': []}

    if source is not None:
        r['rh']     = safe_float(source.get('RH_pct'))
        r['temp_c'] = safe_float(source.get('temp_C'))
        r['temp_f'] = (round(r['temp_c'] * 9 / 5 + 32, 1)
                       if r['temp_c'] is not None else None)

    if meas:
        # CUMULATIVE >= 0.3 µm count — ISO limits are defined on cumulative
        # counts, not the differential per-bin ch1_diff_m3. Taken from the last
        # completed sample, never from live (which may be mid-sample).
        r['ch1_m3'] = safe_float(meas.get('ch1_sum_m3'))
        r['last_meas_dt'] = _row_dt(meas)
        if r['last_meas_dt'] is not None:
            r['offline_min'] = (datetime.now() - r['last_meas_dt']).total_seconds() / 60

    for s in sensor_series():
        loc = s.get('name') or 'unknown'
        entry = {'name': loc, 'last_dt': None, 'silent_h': None,
                 'temp_c': None, 'temp_f': None, 'rh': None, 'never': not s.get('ts')}
        if s.get('ts'):
            try:
                entry['last_dt'] = datetime.strptime(s['ts'][-1], '%Y-%m-%d %H:%M:%S')
                entry['silent_h'] = ((datetime.now() - entry['last_dt'])
                                     .total_seconds() / 3600.0)
            except (ValueError, TypeError):
                log(f"Sensor '{loc}' has an unparseable timestamp — treating as no data")
                entry['never'] = True
            entry['temp_c'] = next((v for v in reversed(s.get('temp') or [])
                                    if v is not None), None)
            entry['rh']     = next((v for v in reversed(s.get('rh') or [])
                                    if v is not None), None)
            if entry['temp_c'] is not None:
                entry['temp_f'] = round(entry['temp_c'] * 9 / 5 + 32, 1)
        r['sensors'].append(entry)

    return r


# ── Deciding what is wrong ───────────────────────────────────────────────────

def evaluate(r):
    """Every condition currently active, as a list of dicts.

    Pure: takes readings, returns findings, sends nothing and touches no state.
    That is what makes the whole alert ruleset testable without a mail server.

    Each condition carries `key` (its cooldown identity), `subject` (used when
    it is the only alert in the mail), and the lines shown in the digest.
    """
    out = []

    def add(key, title, subject, reading, limit, why, hours=None):
        out.append({'key': key, 'title': title, 'subject': subject,
                    'reading': reading, 'limit': limit, 'why': why, 'hours': hours})

    rh, temp_f, temp_c = r.get('rh'), r.get('temp_f'), r.get('temp_c')

    if rh is not None and rh < RH_LOW_PCT:
        add('rh_low', 'LOW HUMIDITY',
            f"LOW HUMIDITY: {rh:.1f}% (limit {RH_LOW_PCT:.0f}%)",
            f"{rh:.1f} %", f"below {RH_LOW_PCT:.0f} %",
            "Low humidity raises electrostatic discharge risk, which can damage "
            "detector components during assembly. Check the clean room HVAC.")

    if rh is not None and rh > RH_HIGH_PCT:
        add('rh_high', 'HIGH HUMIDITY',
            f"HIGH HUMIDITY: {rh:.1f}% (limit {RH_HIGH_PCT:.0f}%)",
            f"{rh:.1f} %", f"above {RH_HIGH_PCT:.0f} %",
            "High humidity can condense on detector surfaces and increases "
            "particle adhesion. Check the clean room HVAC.")

    if temp_f is not None and temp_f < TEMP_LOW_F:
        add('temp_low', 'LOW TEMPERATURE',
            f"LOW TEMPERATURE: {temp_f:.1f}F (limit {TEMP_LOW_F:.0f}F)",
            f"{temp_f:.1f} degF ({temp_c:.1f} degC)", f"below {TEMP_LOW_F:.0f} degF",
            "Abnormally cold. Most likely a door left open or a heating failure. "
            "Check the bay doors first, then the HVAC.")

    if temp_f is not None and temp_f > TEMP_HIGH_F:
        add('temp_high', 'HIGH TEMPERATURE',
            f"HIGH TEMPERATURE: {temp_f:.1f}F (limit {TEMP_HIGH_F:.0f}F)",
            f"{temp_f:.1f} degF ({temp_c:.1f} degC)", f"above {TEMP_HIGH_F:.0f} degF",
            "Elevated temperature suggests an HVAC failure or an unusual thermal "
            "load in the clean room.")

    ch1 = r.get('ch1_m3')
    if ch1 is not None and ch1 > PARTICLE_HIGH_M3:
        add('particle_high', 'HIGH PARTICLE COUNT',
            f"HIGH PARTICLE COUNT: {ch1:,.0f} /m3 at 0.3um",
            f"{ch1:,.0f} /m3 cumulative >=0.3 um",
            f"above {PARTICLE_HIGH_M3:,} /m3",
            "Dirtier than ISO 9, the worst classified level. Likely a "
            "contamination event, heavy personnel activity, or filter "
            "degradation. Check the size distribution on the dashboard.")

    off = r.get('offline_min')
    if off is not None and off > OFFLINE_ALERT_MIN:
        last = r['last_meas_dt'].strftime('%Y-%m-%d %H:%M:%S')
        add('counter_offline', 'COUNTER OFFLINE',
            f"COUNTER OFFLINE: no data for {off:.0f} min",
            f"silent {off:.0f} min (last record {last})",
            f"more than {OFFLINE_ALERT_MIN} min",
            "No new record from the particle counter. Check that "
            "particle_plus.py is running on noether (tmux session 'wlc') and "
            f"that the counter is reachable at {COUNTER_HINT}.")

    # ── the distributed Shelly sensors ────────────────────────────────────────
    # A sensor that has NEVER reported is logged, never mailed: that is a
    # deployment gap (a prefix in sensors.yaml with no device behind it), and
    # mailing it would repeat forever with nothing fixable by email.
    for s in r.get('sensors') or []:
        loc = s['name']
        if s['never']:
            log(f"Sensor '{loc}' has never reported — not alerting (check sensors.yaml)")
            continue

        if s['silent_h'] is not None and s['silent_h'] > SENSOR_SILENT_HOURS:
            add(f'sensor_silent:{loc}', 'SENSOR SILENT',
                f"SENSOR SILENT: {loc} ({s['silent_h']:.0f} h)",
                f"{loc}: no report for {s['silent_h']:.1f} h "
                f"(last {s['last_dt'].strftime('%Y-%m-%d %H:%M')})",
                f"more than {SENSOR_SILENT_HOURS:.0f} h",
                "Most likely a flat battery. Also check the MQTT broker and that "
                "shelly_ht_logger.py is running on noether (tmux 'wlc', window "
                "'ht-logger').",
                hours=SENSOR_SILENT_COOLDOWN_H)
            continue          # a silent sensor's last reading is not news

        # Only reached while the sensor is reporting, so these values are current.
        why = ("One location is out of range while the rest of the bay may look "
               "normal — check for a door left open, a local heat source, or an "
               "HVAC fault serving that area.")
        reasons = []
        if s['temp_f'] is not None and s['temp_f'] < TEMP_LOW_F:
            reasons.append((f"{s['temp_f']:.1f} degF ({s['temp_c']:.1f} degC)",
                            f"below {TEMP_LOW_F:.0f} degF"))
        if s['temp_f'] is not None and s['temp_f'] > TEMP_HIGH_F:
            reasons.append((f"{s['temp_f']:.1f} degF ({s['temp_c']:.1f} degC)",
                            f"above {TEMP_HIGH_F:.0f} degF"))
        if s['rh'] is not None and s['rh'] < RH_LOW_PCT:
            reasons.append((f"{s['rh']:.1f} % RH", f"below {RH_LOW_PCT:.0f} %"))
        if s['rh'] is not None and s['rh'] > RH_HIGH_PCT:
            reasons.append((f"{s['rh']:.1f} % RH", f"above {RH_HIGH_PCT:.0f} %"))
        if reasons:
            add(f'sensor_band:{loc}', 'OUT OF RANGE',
                f"OUT OF RANGE at {loc}: {reasons[0][0]} {reasons[0][1]}",
                f"{loc}: " + ', '.join(v for v, _ in reasons),
                ', '.join(lim for _, lim in reasons), why)

    return out


# ── Formatting the mail ──────────────────────────────────────────────────────

DASHBOARD_URL = 'https://rohit-raut.github.io/WLC-High-Bay-Monitoring/'
COUNTER_HINT  = '10.66.66.68:502'
_RULE = '-' * 68


def _ago(dt):
    """'4 min ago' / '3.2 h ago' — how fresh a reading is, in words."""
    if dt is None:
        return '--'
    mins = (datetime.now() - dt).total_seconds() / 60
    if mins < 1:
        return 'just now'
    if mins < 90:
        return f'{mins:.0f} min ago'
    return f'{mins / 60:.1f} h ago'


def conditions_block(r):
    """The CURRENT CONDITIONS table appended to every alert mail.

    An alert saying "humidity is low" is far more actionable next to the whole
    bay's readings — often you can diagnose without opening the dashboard.
    """
    lines = ['CURRENT CONDITIONS', _RULE,
             f"  {'LOCATION':<20}{'TEMP':>12}{'HUMIDITY':>11}   NOTE",
             '']

    if r.get('temp_f') is not None or r.get('rh') is not None:
        t = f"{r['temp_f']:.1f} degF" if r.get('temp_f') is not None else '--'
        h = f"{r['rh']:.0f} %"        if r.get('rh')     is not None else '--'
        note = (f"{r['ch1_m3']:,.0f} /m3 >=0.3um"
                if r.get('ch1_m3') is not None else 'no particle data')
        if r.get('offline_min') is not None and r['offline_min'] > OFFLINE_ALERT_MIN:
            note += f"  [OFFLINE {r['offline_min'] / 60:.0f} h]"
        lines.append(f"  {'Particle Counter':<20}{t:>12}{h:>11}   {note}")
    else:
        lines.append(f"  {'Particle Counter':<20}{'--':>12}{'--':>11}   no data")

    for s in r.get('sensors') or []:
        if s['never']:
            lines.append(f"  {s['name']:<20}{'--':>12}{'--':>11}   never reported")
            continue
        silent = s['silent_h'] is not None and s['silent_h'] > SENSOR_SILENT_HOURS
        t = f"{s['temp_f']:.1f} degF" if s['temp_f'] is not None else '--'
        h = f"{s['rh']:.0f} %"        if s['rh']     is not None else '--'
        if silent:
            lines.append(f"  {s['name']:<20}{'--':>12}{'--':>11}   "
                         f"SILENT {s['silent_h']:.0f} h")
        else:
            lines.append(f"  {s['name']:<20}{t:>12}{h:>11}   {_ago(s['last_dt'])}")

    if not r.get('sensors'):
        lines.append('  (no distributed sensors configured)')
    return '\n'.join(lines)


def digest_subject(due):
    """One alert keeps its own descriptive subject; several get a count."""
    if len(due) == 1:
        return due[0]['subject']
    return f"{len(due)} ALERTS: {due[0]['title']} + {len(due) - 1} more"


def digest_body(due, r, now_str):
    """Every condition active in this run, then the full current readings."""
    head = [f"{len(due)} alert{'s' if len(due) != 1 else ''} active at {now_str}",
            '', 'ACTIVE ALERTS', _RULE, '']
    for i, a in enumerate(due, 1):
        head += [f"  [{i}] {a['title']}",
                 f"      Reading:  {a['reading']}",
                 f"      Limit:    {a['limit']}"]
        # wrap the explanation to a readable width without importing textwrap
        words, line = a['why'].split(), ''
        why_lines = []
        for w in words:
            if len(line) + len(w) + 1 > 60:
                why_lines.append(line)
                line = w
            else:
                line = f'{line} {w}'.strip()
        why_lines.append(line)
        head += [f"      {'Why:' if n == 0 else '    '}      {wl}"
                 for n, wl in enumerate(why_lines)]
        head.append('')

    tail = ['', conditions_block(r), '', _RULE,
            f"Dashboard:  {DASHBOARD_URL}",
            f"Sent:       {now_str}",
            "Location:   WLC High Bay (Wright Lab, Yale University)",
            "Instrument: Particles Plus Model 7301",
            "",
            "Each condition re-notifies at most once every "
            f"{COOLDOWN_HOURS} h while it stays active.",
            ]
    return '\n'.join(head + tail)


# ── The 10-minute check ──────────────────────────────────────────────────────

def check_alerts():
    """Evaluate every condition and send ONE digest covering those that are due.

    Grouping matters: a real HVAC failure trips temperature, humidity and
    several sensors at once, and three separate emails with no overview are
    harder to act on than one that shows the whole picture. Cooldowns stay
    per-condition, so a new problem still mails immediately even if another
    condition is mid-cooldown.
    """
    state   = load_state()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    r = gather_readings()
    if not r['have_data']:
        log('No data available to check.')
        return

    log(f"Check: RH={r['rh']}%  Temp={r['temp_f']}F  ch1={r['ch1_m3']}/m³  "
        f"last_meas={r['last_meas_dt'].strftime('%H:%M') if r['last_meas_dt'] else 'unknown'}")

    active = evaluate(r)
    active_keys = {a['key'] for a in active}

    # a condition that recovered drops its cooldown, so the next occurrence
    # notifies immediately instead of waiting out the old timer
    for key in [k for k in state if k not in active_keys]:
        state.pop(key, None)

    if not active:
        if not DRY_RUN:
            save_state(state)
        log('All parameters within normal range.')
        return

    due     = [a for a in active if cooldown_expired(state, a['key'], a['hours'])]
    waiting = [a for a in active if a not in due]
    for a in waiting:
        log(f"{a['key']} active but cooldown not expired")

    if due:
        if send_email(digest_subject(due), digest_body(due, r, now_str)):
            for a in due:
                state[a['key']] = datetime.now().isoformat()
    if not DRY_RUN:
        save_state(state)


# ── Weekly summary ───────────────────────────────────────────────────────────

def _stats(values):
    """(min, mean, max) of the non-None values, or None if there are none."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return min(vals), sum(vals) / len(vals), max(vals)


def _fmt_stats(st, unit, dp=1):
    if st is None:
        return 'no data'
    return f"{st[0]:,.{dp}f} / {st[1]:,.{dp}f} / {st[2]:,.{dp}f} {unit}"


def archive_rows_since(days):
    """Measurement rows from the last `days` days (empty list on any problem)."""
    if not os.path.exists(MEAS_CSV):
        return []
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    try:
        with open(MEAS_CSV) as f:
            for row in csv.DictReader(f):
                dt = _row_dt(row)
                if dt is not None and dt >= cutoff:
                    out.append(row)
    except OSError:
        pass
    return out


def send_weekly_summary(days=7):
    """Periodic report — and proof the alert pipeline itself still works.

    A monitoring system that has silently died looks exactly like a lab where
    nothing is wrong. This is the difference between the two.
    """
    now   = datetime.now()
    since = now - timedelta(days=days)
    rows  = archive_rows_since(days)

    temps = [safe_float(x.get('temp_C')) for x in rows]
    temps_f = [round(t * 9 / 5 + 32, 1) for t in temps if t is not None]
    rhs   = [safe_float(x.get('RH_pct')) for x in rows]
    ch1s  = [safe_float(x.get('ch1_sum_m3')) for x in rows]

    lines = [f"Weekly summary  {since.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}",
             '', 'PARTICLE COUNTER', _RULE,
             f"  Samples:      {len(rows):,}",
             f"  Temperature:  {_fmt_stats(_stats(temps_f), 'degF')}   (min / mean / max)",
             f"  Humidity:     {_fmt_stats(_stats(rhs), '%', 0)}   (min / mean / max)",
             f"  Particles:    {_fmt_stats(_stats(ch1s), '/m3 >=0.3um', 0)}",
             '']

    if not rows:
        lines.append('  NO SAMPLES THIS WEEK — the counter or the logger was down.')
        lines.append('')

    lines += ['SENSOR LOCATIONS', _RULE,
              f"  {'LOCATION':<20}{'REPORTS':>9}   TEMP degF (min/mean/max)"
              f"      RH % (min/mean/max)", '']
    try:
        from features.temp_humidity_sensor.reader import load_sensor_series
        series = load_sensor_series(days=days)
    except Exception as e:
        series = []
        lines.append(f'  (sensor data unavailable: {e})')

    for s in series:
        n = len(s.get('ts') or [])
        if not n:
            lines.append(f"  {s['name']:<20}{0:>9}   no reports this week")
            continue
        tf = [round(t * 9 / 5 + 32, 1) for t in (s.get('temp') or []) if t is not None]
        st, sh = _stats(tf), _stats(s.get('rh') or [])
        lines.append(f"  {s['name']:<20}{n:>9}   "
                     f"{_fmt_stats(st, ''):<28}{_fmt_stats(sh, '', 0)}")
    if not series:
        lines.append('  (no distributed sensors configured)')

    state = load_state()
    lines += ['', 'ALERTS CURRENTLY ACTIVE', _RULE]
    if state:
        for key, when in sorted(state.items()):
            lines.append(f"  {key:<28} since {when[:19].replace('T', ' ')}")
    else:
        lines.append('  none')

    lines += ['', '', conditions_block(gather_readings()), '', _RULE,
              f"Dashboard:  {DASHBOARD_URL}",
              f"Sent:       {now.strftime('%Y-%m-%d %H:%M:%S')}",
              "Location:   WLC High Bay (Wright Lab, Yale University)",
              '',
              "This report also proves the alert system is alive. If it stops",
              "arriving, the cron entry or the mail path has failed — a silent",
              "monitor and a healthy lab look identical until you need one.",
              ]

    ok = send_email(f"Weekly summary  {since.strftime('%b %d')} - {now.strftime('%b %d')}",
                    '\n'.join(lines))
    log('Weekly summary sent.' if ok else 'Weekly summary FAILED — see the error above.')
    return ok


if __name__ == '__main__':
    # argparse, not a manual sys.argv scan: an unrecognised flag must fail loudly.
    # The manual version silently fell through to a REAL run, so a typo'd flag
    # sent live alert mail while appearing to do something safe.
    import argparse

    _p = argparse.ArgumentParser(
        description='WLC High Bay environmental alerts. '
                    'With no options: check every condition and email a digest of '
                    'any that fire. See features/alerts/README.md.')
    _p.add_argument('--dry-run', action='store_true',
                    help='report what WOULD be sent and send nothing: cooldowns are '
                         'neither recorded nor respected, so every active condition '
                         'is shown')
    _p.add_argument('--test-email', action='store_true',
                    help='send one test message to confirm delivery works, then exit '
                         '(reads no data, changes no state)')
    _p.add_argument('--weekly-summary', action='store_true',
                    help='send the periodic summary report and exit (run from cron '
                         'once a week; combine with --dry-run to preview it)')
    _args = _p.parse_args()

    DRY_RUN = _args.dry_run

    if _args.test_email:
        raise SystemExit(0 if send_test_email() else 1)

    if DRY_RUN:
        log('DRY RUN — sending nothing; cooldowns neither recorded nor respected')

    if _args.weekly_summary:
        raise SystemExit(0 if send_weekly_summary() else 1)

    check_alerts()
