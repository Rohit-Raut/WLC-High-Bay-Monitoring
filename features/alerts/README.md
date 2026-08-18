# Environmental Alert System

Sends email alerts when any monitored clean room parameter crosses a threshold.
Reads directly from the CSV files written by `particle_plus.py` — no changes to
the core logger are needed.

---

## Alert Conditions

These are **emergency** limits, deliberately wider than the coloured bands the
dashboard uses (`config.yaml` → `thresholds`). The dashboard warns; this wakes
someone up. It should fire only when something is genuinely wrong in the lab.

### From the particle counter

| Condition | Threshold | Reason |
|-----------|-----------|--------|
| RH too low | < 15% | Severe electrostatic discharge risk to detector components |
| RH too high | > 85% | Condensation and moisture risk |
| Temperature too low | < 40 degF | Door left open in winter, heating failure |
| Temperature too high | > 90 degF | No clean room should ever reach this |
| Particle count high | > 102,000,000 /m³ cumulative at 0.3 µm | Dirtier than ISO 9 — off the classified scale |
| Counter offline | > 90 min since last record | Instrument or logger failure |

ISO 14644-1 stops at class 9, so "worse than the worst class" is the particle
trigger. The standard defines no 0.3 µm limit for classes 7–9, so the number
comes from the class formula: 10⁹ × (0.1/0.3)^2.08 ≈ 102M /m³.

### From the distributed Shelly H&T sensors

| Condition | Threshold | Reason |
|-----------|-----------|--------|
| Sensor silent | > 24 h without a report | Flat battery, MQTT broker down, or logger stopped |
| Sensor out of band | same temp/RH limits as above | One location can go bad while the rest of the bay looks fine — a loading door left open in winter cools that end long before the counter notices |

Locations come from `features/temp_humidity_sensor/sensors.yaml` via the
dashboard's own `reader.py`, so there is no second copy of the sensor list.
Each location carries its own cooldown. Two deliberate behaviours:

- a sensor that has **never** reported is logged, never mailed — that is a
  config gap (a prefix with no device behind it), and mailing it would repeat
  forever with nothing anyone could fix by email;
- a silent sensor reports only its silence, never its last reading, so stale
  numbers are never presented as current.

Silence alerts repeat daily rather than every 2 hours: a flat battery stays
flat, and hourly mail about it helps nobody.

All thresholds are configurable at the top of `alerts.py`.

A 2-hour cooldown prevents repeat emails for the same active condition.
Once a condition recovers, the cooldown resets so the next occurrence will
trigger a fresh alert.

---

## Setup (one-time)

### Step 1: Create a Gmail App Password

Standard Gmail passwords do not work with SMTP. You need an App Password:

1. Go to your Google Account at myaccount.google.com
2. Enable 2-Step Verification if not already on
3. Go to Security, then App passwords (search for it in the search bar)
4. Create a new app password, name it something like "WLC Alerts"
5. Copy the 16-character password shown (format: `xxxx xxxx xxxx xxxx`)

### Step 2: Create alerts_secrets.py

Credentials live in `alerts_secrets.py`, which is gitignored and never leaves
the host:

```bash
cd /home/rraut/particle_plus/features/alerts
cp alerts_secrets.example.py alerts_secrets.py
nano alerts_secrets.py
```

```python
EMAIL_SENDER     = 'your.sender@gmail.com'    # the Gmail account sending alerts
EMAIL_PASSWORD   = 'xxxx xxxx xxxx xxxx'      # the app password from Step 1
EMAIL_RECIPIENTS = ['your.name@yale.edu']     # who receives the alerts
```

You can add multiple recipients to the list:
```python
EMAIL_RECIPIENTS = ['you@yale.edu', 'advisor@yale.edu', 'labmate@yale.edu']
```

(If the file is absent the script falls back to the `EMAIL_SENDER`,
`EMAIL_PASSWORD` and `EMAIL_RECIPIENTS` environment variables.)

### Step 3: Dry run — see what would be sent, send nothing

```bash
python3 /home/rraut/particle_plus/features/alerts/alerts.py --dry-run
```

Every alert that would fire is printed in full, no mail leaves the machine and
the cooldown state file is left untouched. Use this to sanity-check the
thresholds against real lab data before anything is delivered.

### Step 4: Test a real send

```bash
python3 /home/rraut/particle_plus/features/alerts/alerts.py
```

Expect `All parameters within normal range.` To prove delivery works, briefly
set `RH_LOW_PCT = 99.0`, run again, confirm the mail arrives (check spam and
mark it not-spam), then set it back.

### Step 5: Add to cron on noether

Run `crontab -e` and add:

```
*/10 * * * * cd /home/rraut/particle_plus && python3 features/alerts/alerts.py >> alert_cron.log 2>&1
```

This runs the check every 10 minutes. The script is fast (reads CSV, checks
values, exits) so cron overhead is negligible.

---

## Self-check

```bash
python3 features/alerts/test_alerts.py
```

Runs the sensor conditions against synthetic series (healthy, silent, out of
band, silent-and-out-of-band, never-reported) and asserts which alerts come
out. Sends nothing and writes no state. The counter conditions need no stub —
every real run exercises them.

---

## State File

The script writes a JSON state file at `data/alert_state.json`. It stores the
ISO 8601 timestamp of the last alert for each condition key. Do not edit this
file manually. To reset all cooldowns (force re-alert on next check):

```bash
rm /home/rraut/particle_plus/data/alert_state.json
```

---

## Logs

- `alert_log.txt` in `BASE_DIR`: every check is logged with current values
- `alert_cron.log`: cron stdout/stderr (only if configured as shown above)

---

## Adjusting Thresholds

All thresholds are in the configuration block at the top of `alerts.py`:

```python
RH_LOW_PCT          = 20.0    # % RH lower limit
RH_HIGH_PCT         = 90.0    # % RH upper limit
TEMP_LOW_F          = 33.0    # degF lower limit
TEMP_HIGH_F         = 120.0   # degF upper limit
PARTICLE_HIGH_M3    = 100000  # counts/m³ at 0.3 µm
OFFLINE_ALERT_MIN   = 90      # minutes before offline alert
COOLDOWN_HOURS      = 2       # hours between repeat alerts per condition
```
