# Features

This directory contains optional add-on modules for the WLC clean room monitoring system.
Each feature lives in its own subdirectory and is fully independent of `particle_plus.py`.

Features read from the CSV files that `particle_plus.py` writes. They do not modify
the core logger, ensuring that the base monitoring system remains stable.

---

## Available Features

| Feature | Description |
|---------|-------------|
| `alerts/` | Email alerts for out-of-range RH, temperature, particle count, and counter offline |

---

## Adding a New Feature

1. Create a new subdirectory: `features/<feature-name>/`
2. Write a standalone Python script that reads from `data/measurements.csv` or `data/live.csv`
3. Add a `README.md` in the subdirectory with setup and usage instructions
4. Do not modify `particle_plus.py` unless absolutely necessary

This pattern keeps the core logging system at a stable, releasable state while
allowing new capabilities to be developed and tested independently.
