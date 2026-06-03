# Environmental Alert System

Sends email alerts when any monitored clean room parameter crosses a threshold.
Reads directly from the CSV files written by `particle_plus.py` — no changes to
the core logger are needed.

---

## Alert Conditions

| Condition | Default threshold | Reason |
|-----------|-------------------|--------|
| RH too low | < 30% | Electrostatic discharge risk to detector components |
| RH too high | > 60% | Condensation and particle adhesion risk |
| Temperature high | > 80 degF | HVAC failure or thermal excursion |
| Particle count high | > 100,000 /m³ at 0.3 µm | Contamination event |
| Counter offline | > 90 min since last record | Instrument or logger failure |

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

### Step 2: Edit alerts.py

Open `alerts.py` and fill in the configuration block:

```python
EMAIL_SENDER     = 'your.sender@gmail.com'    # the Gmail account sending alerts
EMAIL_PASSWORD   = 'xxxx xxxx xxxx xxxx'      # the app password from Step 1
EMAIL_RECIPIENTS = ['your.name@yale.edu']     # who receives the alerts
```

You can add multiple recipients to the list:
```python
EMAIL_RECIPIENTS = ['you@yale.edu', 'advisor@yale.edu', 'labmate@yale.edu']
```

### Step 3: Test it manually

```bash
python3 /home/rraut/particle_plus/features/alerts/alerts.py
```

Check the output and verify an email arrives. If you want to test the alert
firing, temporarily lower a threshold (e.g., set `RH_LOW_PCT = 99.0`) and run again.

### Step 4: Add to cron on noether

Run `crontab -e` and add:

```
*/10 * * * * python3 /home/rraut/particle_plus/features/alerts/alerts.py >> /home/rraut/particle_plus/alert_cron.log 2>&1
```

This runs the check every 10 minutes. The script is fast (reads CSV, checks
values, exits) so cron overhead is negligible.

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
RH_LOW_PCT          = 30.0    # % RH lower limit
RH_HIGH_PCT         = 60.0    # % RH upper limit
TEMP_HIGH_F         = 80.0    # degF upper limit
PARTICLE_HIGH_M3    = 100000  # counts/m³ at 0.3 µm
OFFLINE_ALERT_MIN   = 90      # minutes before offline alert
COOLDOWN_HOURS      = 2       # hours between repeat alerts per condition
```
