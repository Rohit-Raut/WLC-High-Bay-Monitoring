"""
Read the combined Shelly H&T csv (written by shelly_ht_logger.py) into
per-location series for the dashboard's Temperature & Humidity chart.

    csv columns: Location, Date and Time, Temp, Humidity
    returns:     [{name, ts: [...], temp: [...], rh: [...]}, ...]  (name-sorted)

Values are embedded verbatim — the sensors are configured to report the same
units the chart displays (°F / %RH). Never raises: a missing or malformed csv
returns [] so the dashboard build cannot break, and the env chart renders
exactly as it did before sensors existed.
"""

import csv
import os
from datetime import datetime, timedelta

MODULE_DIR  = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = '/project/dune/slow_control/temp_humidity/temp_humidity.csv'


def _load_cfg():
    try:
        import yaml
        with open(os.path.join(MODULE_DIR, 'sensors.yaml')) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _configured_csv_path():
    """output_csv from sensors.yaml (same folder as the logger), else default."""
    return _load_cfg().get('output_csv', DEFAULT_CSV)


def _configured_labels():
    """Every label defined in sensors.yaml — a configured sensor that hasn't
    reported yet must still appear (as an empty series) so the dashboard can
    show a 'no data' card instead of silently omitting it."""
    return [(s or {}).get('label') for s in (_load_cfg().get('sensors') or {}).values()
            if (s or {}).get('label')]


def _sf(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_sensor_series(days=None, csv_path=None):
    """Per-location time series, cut to the last `days` days (None = all)."""
    path = csv_path or _configured_csv_path()
    cutoff = datetime.now() - timedelta(days=days) if days is not None else None
    # legacy Location values (pre-rename rows / not-yet-restarted logger) fold
    # into the current labels so each location stays one series
    aliases = _load_cfg().get('aliases') or {}

    by_loc = {}
    if os.path.exists(path):
        try:
            with open(path, newline='') as f:
                for row in csv.DictReader(f):
                    try:
                        dt = datetime.strptime((row.get('Date and Time') or '').strip(),
                                               '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        continue
                    loc = (row.get('Location') or '').strip()
                    loc = aliases.get(loc, loc)
                    if not loc or (cutoff is not None and dt < cutoff):
                        continue
                    by_loc.setdefault(loc, []).append(
                        (dt, _sf(row.get('Temp')), _sf(row.get('Humidity'))))
        except OSError:
            pass

    # configured sensors with no rows yet (or no csv at all) → empty series,
    # so the dashboard shows a "no data" card instead of silently omitting them
    cfg_labels = _configured_labels()
    for _lbl in cfg_labels:
        by_loc.setdefault(_lbl, [])

    # sensors.yaml order first (labels are locations, ordered physically),
    # then any unexpected csv locations (e.g., pre-rename rows) alphabetically
    ordered = ([l for l in cfg_labels if l in by_loc] +
               sorted(l for l in by_loc if l not in cfg_labels))

    out = []
    for loc in ordered:
        rows = sorted(by_loc[loc], key=lambda r: r[0])   # chronological per location
        out.append({
            'name': loc,
            'ts':   [r[0].strftime('%Y-%m-%d %H:%M:%S') for r in rows],
            'temp': [r[1] for r in rows],
            'rh':   [r[2] for r in rows],
        })
    return out


if __name__ == '__main__':
    for s in load_sensor_series():
        print(f"{s['name']:<12} points={len(s['ts'])} "
              f"latest={s['ts'][-1] if s['ts'] else '—'}")
