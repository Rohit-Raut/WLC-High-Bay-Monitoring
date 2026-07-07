#!/usr/bin/env python3
"""
Shelly H&T Gen3 MQTT logger — ALL sensors → ONE csv. Run under tmux on noether:

    python3 shelly_ht_logger.py

One MQTT client subscribes to <prefix>/events/rpc for every sensor listed in
sensors.yaml (same directory as this script). Each battery-powered Shelly
sleeps and pushes a NotifyFullStatus when it wakes (~every 5 min or on a
±0.5 °C change); on_message tells the sensors apart by topic prefix and
appends one row per report to the shared csv:

    Location,Date and Time,Temp,Humidity

Requires: pip3 install paho-mqtt pyyaml
"""

import csv
import json
import os
import sys
from datetime import datetime

import paho.mqtt.client as mqtt
import yaml

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH  = os.path.join(SCRIPT_DIR, 'sensors.yaml')
DEFAULT_CSV  = '/project/dune/slow_control/temp_humidity/temp_humidity.csv'
FIELDS       = ['Location', 'Date and Time', 'Temp', 'Humidity']


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"config not found: {CONFIG_PATH} — copy sensors.example.yaml "
                 "to sensors.yaml and fill in your Shelly prefixes")
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get('sensors'):
        sys.exit("no sensors defined in sensors.yaml")
    return cfg


def ensure_header(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, 'w', newline='') as f:
            csv.writer(f).writerow(FIELDS)


def main():
    cfg      = load_config()
    csv_path = cfg.get('output_csv', DEFAULT_CSV)
    # topic prefix → location label, e.g. 'shellyhtg3-d885ac14bad4' → 'North Wall'
    locations = {s['prefix']: s.get('label', sid)
                 for sid, s in cfg['sensors'].items() if s and s.get('prefix')}
    ensure_header(csv_path)

    def on_connect(client, userdata, flags, rc, properties=None):
        print(f"[{datetime.now():%H:%M:%S}] connected rc={rc}, "
              f"subscribing to {len(locations)} sensors")
        for prefix in locations:
            client.subscribe(f"{prefix}/events/rpc")

    def on_message(client, userdata, msg):
        prefix = msg.topic[:-len('/events/rpc')]
        location = locations.get(prefix)
        if location is None:
            return
        try:
            params = json.loads(msg.payload.decode()).get('params', {})
        except json.JSONDecodeError:
            return
        temp = params.get('temperature:0', {}).get('tC')
        rh   = params.get('humidity:0', {}).get('rh')
        if temp is None and rh is None:
            return  # partial NotifyStatus with no reading, skip
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(csv_path, 'a', newline='') as f:
            csv.writer(f).writerow([location, now, temp, rh])
        print(f"Time: {now}, Location: {location}, Temp: {temp} C, Humidity: {rh} %")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if cfg.get('username'):
        client.username_pw_set(cfg['username'], cfg.get('password'))
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect(cfg.get('broker', 'broker.hivemq.com'),
                   int(cfg.get('port', 1883)), keepalive=60)
    print(f"[{datetime.now():%H:%M:%S}] logging {len(locations)} sensors → {csv_path}")
    client.loop_forever()   # auto-reconnects on drop


if __name__ == '__main__':
    main()
