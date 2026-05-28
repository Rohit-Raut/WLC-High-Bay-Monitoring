# WLC High Bay — DUNE Clean Room Particle Monitor

Automated particle counter logger and live dashboard for the Wright Lab High Bay clean room.
Data is collected from a **Particles Plus 7000 Series** counter over Modbus TCP and published
to GitHub Pages automatically after every sample cycle.

**Live dashboard:** https://rohit-raut.github.io/WLC-High-Bay-Monitoring/

---

## Hardware

| Item | Value |
|------|-------|
| Instrument | Particles Plus Model 7301 |
| IP address | 10.66.66.68 |
| Port | 502 (Modbus TCP) |
| Host | noether cluster (`rraut@noether`) |

---

## Setup on noether

```bash
# 1. Clone this repo into the working directory
cd /home/rraut/particle_plus
git clone git@github.com:Rohit-Raut/WLC-High-Bay-Monitoring.git dashboard

# 2. Install dependency
pip install pymodbus>=3.5

# 3. Start in a persistent tmux session
tmux new -s particle
cd /home/rraut/particle_plus/dashboard
python3 particle_plus.py --all
# Ctrl+B, D  to detach
```

---

## Usage

```
python3 particle_plus.py --sample     24/7 sampling scheduler (writes to CSV)
python3 particle_plus.py --sync       one-shot: pull all records from counter to CSV
python3 particle_plus.py --live       stream live in-progress data every 10 s
python3 particle_plus.py --dashboard  generate HTML and push to GitHub Pages
python3 particle_plus.py --all        run everything — recommended for tmux
```

---

## Repo layout

```
WLC-High-Bay-Monitoring/
├── particle_plus.py          main logger + dashboard generator
├── flush_and_erase.py        standalone sync / erase utility
├── sample_particle.py        lightweight live poller (prototype)
├── test.py                   quick Modbus connectivity test
├── requirements.txt
├── index.html                generated dashboard (served by GitHub Pages)
└── data/
    └── particle_data.csv     latest data snapshot (pushed with each update)
```

`particle_data_archive.csv` and `sync_log.txt` are created at runtime and excluded from git.

---

## How it works

1. `--sample` triggers the counter, waits for the 1-min sample + 30-min hold, then syncs all records to `particle_data_archive.csv`.
2. After each successful sync, `--dashboard` runs automatically: generates `index.html` from the last 7 days of data, copies a CSV snapshot to `data/`, commits, and pushes to GitHub.
3. GitHub Pages serves `index.html` from the `main` branch root.
4. If the counter is unreachable, the dashboard is still pushed with an **OFFLINE** banner and the last recorded data, and the script retries every 30 minutes.
