# formlabs-tank-layers

Pull resin-tank lifetime usage from the **Formlabs Web API**, build one analysis
spreadsheet, and flag tanks that may be **failing early**. Form 4 tanks are
selected by their code names (Daguerre + Diesel).

## Setup

1. Get API credentials (Client ID + Secret) at https://dashboard.formlabs.com/#developer
2. Set them as env vars:

```bash
export FORMLABS_CLIENT_ID="your_client_id"
export FORMLABS_CLIENT_SECRET="your_client_secret"
```

> Requires Python 3.8+ (standard library only).

## Run

```bash
# All Form 4 tanks → tank_layer_report.csv  (fast, tanks only)
python3 formlabs_tank_layers.py -f4

# + per-tank print failure rates (pulls the prints feed — heavier)
python3 formlabs_tank_layers.py -f4 --check-prints --since 2025-06-01

# Flag tanks under 40k layers as early-failure suspects
python3 formlabs_tank_layers.py -f4 --check-prints --review-below 40000

# See every tank_type the API returns
python3 formlabs_tank_layers.py --list-types
```

## Useful flags

| Flag | What it does |
|------|--------------|
| `-f4`, `--form4` | Only Form 4 tanks (Daguerre + Diesel) |
| `--check-prints` | Pull prints and compute per-tank failure rates |
| `--since YYYY-MM-DD` | Bound the prints pull to a date range |
| `--printer SERIAL` | Only pull prints from this printer (repeatable) |
| `--review-below N` | Flag tanks under N layers as suspects (default = `--threshold`) |
| `--threshold N` | Lifespan layer threshold for OVER/UNDER (default 75000) |
| `--out PATH` | Output CSV path (default `tank_layer_report.csv`) |
| `--list-types` | Print distinct tank_type / display_name values and exit |

**Note:** the API can't filter prints by tank (a tank moves between printers), so
`--check-prints` pulls the prints feed and groups by tank. Bound it with `--since`
/ `--printer` when you can.

### macOS SSL
If you hit `CERTIFICATE_VERIFY_FAILED`, run `pip3 install certifi` (picked up
automatically), or `/Applications/Python\ 3.13/Install\ Certificates.command`.
