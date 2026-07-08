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


def _configured_csv_path():
    """output_csv from sensors.yaml (same folder as the logger), else default."""
    try:
        import yaml
        with open(os.path.join(MODULE_DIR, 'sensors.yaml')) as f:
            return (yaml.safe_load(f) or {}).get('output_csv', DEFAULT_CSV)
    except Exception:
        return DEFAULT_CSV


def _sf(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_sensor_series(days=None, csv_path=None):
    """Per-location time series, cut to the last `days` days (None = all)."""
    path = csv_path or _configured_csv_path()
    if not os.path.exists(path):
        return []
    cutoff = datetime.now() - timedelta(days=days) if days is not None else None

    by_loc = {}
    try:
        with open(path, newline='') as f:
            for row in csv.DictReader(f):
                try:
                    dt = datetime.strptime((row.get('Date and Time') or '').strip(),
                                           '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
                loc = (row.get('Location') or '').strip()
                if not loc or (cutoff is not None and dt < cutoff):
                    continue
                by_loc.setdefault(loc, []).append(
                    (dt, _sf(row.get('Temp')), _sf(row.get('Humidity'))))
    except OSError:
        return []

    out = []
    for loc in sorted(by_loc):
        rows = sorted(by_loc[loc], key=lambda r: r[0])   # chronological per location
        out.append({
            # display name only — csv Location values stay as logged, so the
            # rename doesn't split historical series
            'name': loc.replace('Sensor', 'Site'),
            'ts':   [r[0].strftime('%Y-%m-%d %H:%M:%S') for r in rows],
            'temp': [r[1] for r in rows],
            'rh':   [r[2] for r in rows],
        })
    return out


if __name__ == '__main__':
    for s in load_sensor_series():
        print(f"{s['name']:<12} points={len(s['ts'])} "
              f"latest={s['ts'][-1] if s['ts'] else '—'}")
