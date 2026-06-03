#!/usr/bin/env python3
"""
formlabs_tank_layers.py
=======================
Pull resin-tank lifetime usage from the Formlabs Web API, build one analysis 
spreadsheet, and flag tanks that look like they arebfailing early.

WHAT IT PULLS
  Per tank (always):
    layers_printed, layer_count, write_count, print_time (hours), material,
    tank_type, manufacturer, lot_number, mechanical_version, manufacture_date,
    first_fill_date, last_print_date, age, which printer it's in, etc.
  Per tank (only with --check-prints):
    number of prints, finished / aborted / error counts, print FAILURE count,
    and a failure_rate_pct -- so you can see if a low-layer tank is throwing a
    lot of failed prints (a sign it's degrading / failing early).

MODELS  
  -f4 / --form4 selects Form 4 tanks. On this account the Form 4 tanks are
  labelled TANK_TYPE_DAGUERRE_* and *_DIESEL_*, so the Form 4 preset matches
  "daguerre" and "diesel" (plus the literal "form 4" spellings). Run with
  --list-types to see every tank_type the API returns.

macOS SSL NOTE
  python.org Python doesn't use the system root certs -> "CERTIFICATE_VERIFY_FAILED".
  Fix once:   /Applications/Python\\ 3.13/Install\\ Certificates.command
  or:         pip3 install certifi      (picked up automatically)
  or rerun with --insecure (last resort).

USAGE
    Open your CLI and navigate to the location of this file.
    Next, input your credentials:
  export FORMLABS_CLIENT_ID="your_client_id"
  export FORMLABS_CLIENT_SECRET="your_client_secret"

    Run one of the following commands:
  python3 formlabs_tank_layers.py -f4                       # all Form 4 tanks, fast
  python3 formlabs_tank_layers.py -f4 --check-prints        # + per-tank failure rates
  python3 formlabs_tank_layers.py -f4 --check-prints --since 2025-06-01
  python3 formlabs_tank_layers.py -f4 --check-prints --review-below 40000
  python3 formlabs_tank_layers.py --list-types              # show tank_type values
  python3 formlabs_tank_layers.py --csv tanks.csv           # only serials in a CSV

  Key options:
      -f4, --form4      Only Form 4 tanks (Daguerre + Diesel + "form 4")
      --model TEXT      Extra model-filter substring (repeatable)
      --exclude TEXT    Drop tanks whose text matches this substring (repeatable)
      --list-types      Print distinct tank_type / display_name values and exit
      --threshold N     "Lifespan" layer threshold for OVER/UNDER (default 75000)
      --review-below N  Flag tanks under N layers as early-failure suspects
                        (default = same as --threshold)
      --check-prints    Pull the prints feed and compute per-tank failure rates
      --since DATE      Only pull prints on/after DATE (YYYY-MM-DD); bounds the pull
      --printer SERIAL  Only pull prints from this printer serial (repeatable)
      --csv PATH        Restrict to serials in this CSV's tank column
      --tank-col NAME   Serial column name in the CSV (default "Tank")
      --metric          layers_printed (default) or layer_count
      --out PATH        Output CSV (default tank_layer_report.csv)
      --cacert PATH     CA bundle for SSL verification
      --insecure        Disable SSL verification (last resort)
"""

import argparse
import csv
import os
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

API_BASE = "https://api.formlabs.com/developer/v1"
TOKEN_URL = f"{API_BASE}/o/token/"
TANKS_URL = f"{API_BASE}/tanks/"
PRINTS_URL = f"{API_BASE}/prints/"

# Model filter shorthands. Patterns are lower-cased substrings matched against a
# tank's combined text (tank_type + display_name + material).
# Form 4 tanks are labelled with the "Daguerre" and "Diesel" code names.
MODEL_PRESETS = {
    "form4": ["form 4", "form4", "form-4", "daguerre", "diesel"],
}

# Print statuses that mean the print did not complete successfully.
FAILED_STATUSES = {"ABORTED", "ERROR"}
# Print statuses that are still in progress (excluded from success/fail rates).
INPROGRESS_STATUSES = {"QUEUED", "PREPRINT", "PRINTING", "PAUSED", "PAUSING",
                       "ABORTING", "WAITING_FOR_RESOLUTION", "PREHEAT",
                       "PRECOAT", "POSTCOAT"}

SSL_CONTEXT = None  # built in main()


# --------------------------------------------------------------------------- #
# SSL
# --------------------------------------------------------------------------- #
def build_ssl_context(cacert=None, insecure=False):
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("  WARNING: SSL verification disabled (--insecure).")
        return ctx
    if cacert:
        return ssl.create_default_context(cafile=cacert)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def get_access_token(client_id, client_secret):
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR getting token ({e.code}): {e.read().decode(errors='replace')}\n"
                 f"Check your Client ID / Secret at https://dashboard.formlabs.com/#developer")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR connecting to {TOKEN_URL}: {e.reason}\n"
                 "If this is an SSL certificate error on macOS, run:\n"
                 "    /Applications/Python\\ 3.13/Install\\ Certificates.command\n"
                 "or:  pip3 install certifi   (used automatically)\n"
                 "or rerun with --insecure as a last resort.")
    token = payload.get("access_token")
    if not token:
        sys.exit(f"ERROR: no access_token in response: {payload}")
    return token


# --------------------------------------------------------------------------- #
# Generic paginated GET
# --------------------------------------------------------------------------- #
def fetch_paginated(url, token, extra_params=None, label="items",
                    per_page=100, max_pages=None):
    """Walk a DRF-style paginated list endpoint and return all results."""
    headers = {"Authorization": f"Bearer {token}"}
    out, page = [], 1
    while True:
        params = {"page": page, "per_page": per_page}
        if extra_params:
            params.update(extra_params)
        full = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(full, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-after", "5"))
                print(f"  rate limited; waiting {wait}s ...")
                time.sleep(wait)
                continue
            sys.exit(f"ERROR fetching {label} page {page} ({e.code}): "
                     f"{e.read().decode(errors='replace')}")
        if isinstance(body, dict):
            results = body.get("results", [])
            out.extend(results)
            total = body.get("count")
            print(f"  {label} page {page}: +{len(results)} (total {len(out)}"
                  f"{'/' + str(total) if total is not None else ''})")
            if not body.get("next") or not results:
                break
            page += 1
        elif isinstance(body, list):
            out.extend(body)
            break
        else:
            break
        if max_pages and page > max_pages:
            print(f"  reached --max-pages safety limit ({max_pages}); stopping.")
            break
        time.sleep(0.1)
    return out


def fetch_all_tanks(token):
    return fetch_paginated(TANKS_URL, token, label="tanks")


def fetch_all_prints(token, since=None, printers=None, max_pages=None):
    """Pull prints. If `printers` is given, query each printer serial separately
    (the API supports a printer filter but NOT a tank filter)."""
    extra = {}
    if since:
        # ISO 8601; server filters by date__gt
        extra["date__gt"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    if printers:
        all_prints = []
        for serial in printers:
            print(f"  prints for printer {serial} ...")
            p = dict(extra)
            p["printer"] = serial
            all_prints += fetch_paginated(PRINTS_URL, token, extra_params=p,
                                          label=f"prints[{serial}]", max_pages=max_pages)
        return all_prints
    return fetch_paginated(PRINTS_URL, token, extra_params=extra,
                           label="prints", max_pages=max_pages)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize(serial):
    return (serial or "").strip().lower()


def tank_text(t):
    """Combined lower-case text used for model matching."""
    return " ".join(str(t.get(k, "") or "") for k in ("tank_type", "display_name", "material")).lower()


def model_family(t):
    txt = tank_text(t)
    if any(p in txt for p in MODEL_PRESETS["form4"]):
        return "Form 4"
    return "Other"


def matches_model(t, include_patterns, exclude_patterns):
    txt = tank_text(t)
    if exclude_patterns and any(p in txt for p in exclude_patterns):
        return False
    if not include_patterns:          # no filter -> include everything
        return True
    return any(p in txt for p in include_patterns)


def load_csv_serials(path, tank_col):
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        cwd = os.getcwd()
        here = sorted(fn for fn in os.listdir(cwd) if fn.lower().endswith(".csv"))
        msg = [f"ERROR: CSV not found: {path}",
               f"       (looked in current folder: {cwd})"]
        msg += (["       CSV files in this folder you could use with --csv:"]
                + [f"         - {fn}" for fn in here]) if here else \
               ["       No .csv files in this folder."]
        msg.append('       Tip: pass the full path, e.g. --csv ~/Downloads/"your file.csv"')
        sys.exit("\n".join(msg))
    seen, ordered = set(), []
    with open(expanded, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit(f"ERROR: {path} appears to be empty (no header row).")
        if tank_col not in reader.fieldnames:
            sys.exit(f"ERROR: column '{tank_col}' not found. "
                     f"Available columns: {reader.fieldnames}\n"
                     f"Pass the right one with --tank-col.")
        for row in reader:
            raw = (row.get(tank_col) or "").strip()
            if raw and normalize(raw) not in seen:
                seen.add(normalize(raw))
                ordered.append(raw)
    return ordered


def status_for(metric_val, threshold):
    if metric_val is None:
        return "NO DATA"
    return "OVER" if metric_val > threshold else "UNDER"


def hours(ms):
    return round(ms / 3_600_000, 1) if isinstance(ms, (int, float)) else ""


def parse_dt(s):
    """Parse an ISO 8601 timestamp from the API into an aware datetime, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def days_between(later, earlier):
    if not later or not earlier:
        return ""
    return round((later - earlier).total_seconds() / 86400, 1)


# --------------------------------------------------------------------------- #
# Print aggregation (per tank)
# --------------------------------------------------------------------------- #
def aggregate_prints_by_tank(prints):
    """Return {normalized_tank_serial: stats dict}."""
    by_tank = defaultdict(lambda: {
        "prints_total": 0, "prints_finished": 0, "prints_aborted": 0,
        "prints_error": 0, "prints_failure_flag": 0, "prints_success_flag": 0,
        "prints_inprogress": 0, "last_print_status": "", "_last_dt": None,
    })
    for p in prints:
        tserial = normalize(p.get("tank"))
        if not tserial_ok(tserial):
            continue
        s = by_tank[tserial]
        s["prints_total"] += 1
        status = (p.get("status") or "").upper()
        success_obj = p.get("print_run_success") or {}
        success_val = (success_obj.get("print_run_success") or "").upper() \
            if isinstance(success_obj, dict) else ""

        if status in FAILED_STATUSES:
            if status == "ABORTED":
                s["prints_aborted"] += 1
            elif status == "ERROR":
                s["prints_error"] += 1
        elif status == "FINISHED":
            s["prints_finished"] += 1
        elif status in INPROGRESS_STATUSES:
            s["prints_inprogress"] += 1

        if success_val == "FAILURE":
            s["prints_failure_flag"] += 1
        elif success_val == "SUCCESS":
            s["prints_success_flag"] += 1

        # track most recent print status
        dt = parse_dt(p.get("print_started_at") or p.get("created_at"))
        if dt and (s["_last_dt"] is None or dt > s["_last_dt"]):
            s["_last_dt"] = dt
            s["last_print_status"] = status
    return by_tank


def tserial_ok(s):
    return bool(s) and s != "none" and s != "null"


def failed_count(s):
    """A print counts as failed if it errored/aborted OR was marked FAILURE."""
    return s["prints_aborted"] + s["prints_error"] + s["prints_failure_flag"]


def failure_rate_pct(s):
    failed = s["prints_aborted"] + s["prints_error"]
    finished = s["prints_finished"]
    # also fold in user-marked FAILURE/SUCCESS on otherwise-finished prints
    terminal = failed + finished
    if terminal == 0:
        return ""
    # Count user-marked FAILUREs not already counted as aborted/error.
    extra_fail = max(0, s["prints_failure_flag"] - 0)
    fails = failed + min(extra_fail, finished)  # cap so rate stays <=100
    return round(100.0 * fails / terminal, 1)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    global SSL_CONTEXT
    ap = argparse.ArgumentParser(
        description="Pull Formlabs tank usage, build an analysis spreadsheet, "
                    "and flag tanks that may be failing early.")
    ap.add_argument("-f4", "--form4", action="store_true",
                    help="Only Form 4 tanks (Daguerre + Diesel + 'form 4')")
    ap.add_argument("--model", action="append", default=[],
                    help="Extra model-filter substring (tank_type/display_name/material); repeatable")
    ap.add_argument("--exclude", action="append", default=[],
                    help="Exclude tanks whose text matches this substring; repeatable")
    ap.add_argument("--list-types", action="store_true",
                    help="Print distinct tank_type/display_name values and exit")
    ap.add_argument("--csv", default=None, help="Restrict to serials in this CSV's tank column")
    ap.add_argument("--tank-col", default="Tank", help="Serial column name in the CSV")
    ap.add_argument("--threshold", type=int, default=75000,
                    help="Lifespan layer threshold for OVER/UNDER (default 75000)")
    ap.add_argument("--review-below", type=int, default=None,
                    help="Flag tanks under N layers as early-failure suspects "
                         "(default = --threshold)")
    ap.add_argument("--check-prints", action="store_true",
                    help="Pull the prints feed and compute per-tank failure rates")
    ap.add_argument("--since", default=None,
                    help="Only pull prints on/after this date (YYYY-MM-DD); bounds the pull")
    ap.add_argument("--printer", action="append", default=[],
                    help="Only pull prints from this printer serial; repeatable")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Safety cap on pages fetched per list endpoint")
    ap.add_argument("--metric", choices=["layers_printed", "layer_count"], default="layers_printed")
    ap.add_argument("--out", default="tank_layer_report.csv", help="Output CSV path")
    ap.add_argument("--cacert", default=None, help="CA bundle for SSL verification")
    ap.add_argument("--insecure", action="store_true", help="Disable SSL verification (last resort)")
    ap.add_argument("--client-id", default=os.environ.get("FORMLABS_CLIENT_ID"))
    ap.add_argument("--client-secret", default=os.environ.get("FORMLABS_CLIENT_SECRET"))
    args = ap.parse_args()

    if not args.client_id or not args.client_secret:
        sys.exit("ERROR: provide credentials via --client-id/--client-secret "
                 "or the FORMLABS_CLIENT_ID / FORMLABS_CLIENT_SECRET env vars.\n"
                 "Create them at https://dashboard.formlabs.com/#developer")

    review_below = args.review_below if args.review_below is not None else args.threshold

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            sys.exit(f"ERROR: --since must be YYYY-MM-DD, got {args.since!r}")

    # Build model include/exclude pattern lists
    include_patterns = [p.lower() for p in args.model]
    if args.form4:
        include_patterns += MODEL_PRESETS["form4"]
    exclude_patterns = [p.lower() for p in args.exclude]

    SSL_CONTEXT = build_ssl_context(cacert=args.cacert, insecure=args.insecure)

    print("Authenticating with Formlabs Web API ...")
    token = get_access_token(args.client_id, args.client_secret)

    print("Fetching all tanks on the account ...")
    tanks = fetch_all_tanks(token)

    # Show what tank types exist (handy for choosing a filter)
    type_counts = Counter((t.get("tank_type") or "(blank)") for t in tanks)
    print("\nTank types on the account:")
    for name, n in type_counts.most_common():
        print(f"    {n:5d}  {name}")
    if args.list_types:
        dn = Counter((t.get("display_name") or "(blank)") for t in tanks)
        print("\nDisplay names on the account:")
        for name, n in dn.most_common(30):
            print(f"    {n:5d}  {name}")
        print("\n(--list-types) Done. Re-run with -f4 or --model \"<text>\" to filter.")
        return

    # Optional CSV restriction
    csv_serials = None
    if args.csv:
        csv_serials = set(normalize(s) for s in load_csv_serials(args.csv, args.tank_col))
        print(f"\nCSV restriction: {len(csv_serials)} unique serials from {args.csv}")

    # Apply model filter (and CSV restriction, if any)
    selected = []
    for t in tanks:
        if not matches_model(t, include_patterns, exclude_patterns):
            continue
        if csv_serials is not None and normalize(t.get("serial")) not in csv_serials:
            continue
        selected.append(t)

    filt_desc = []
    if args.form4:
        filt_desc.append("Form 4")
    if args.model:
        filt_desc.append("model~" + "/".join(args.model))
    if args.exclude:
        filt_desc.append("exclude~" + "/".join(args.exclude))
    if args.csv:
        filt_desc.append("in CSV")
    filt_desc = ", ".join(filt_desc) if filt_desc else "ALL tanks"

    print(f"\nFilter: {filt_desc}  ->  {len(selected)} of {len(tanks)} tanks selected.")
    if not selected:
        print("\nNo tanks matched the filter. The tank-type list above shows what's\n"
              "available — re-run with --model \"<exact text>\", or --list-types for more.")
        return

    # ----------------------------------------------------------------- #
    # Optional: pull prints and compute per-tank failure stats
    # ----------------------------------------------------------------- #
    prints_by_tank = {}
    if args.check_prints:
        n_suspect = sum(1 for t in selected
                        if (t.get(args.metric) or 0) < review_below)
        print(f"\n--check-prints: pulling prints feed to compute failure rates.")
        print(f"  ({n_suspect} of {len(selected)} selected tanks are under "
              f"{review_below:,} layers and flagged for review)")
        if since_dt:
            print(f"  bounded to prints since {args.since}")
        if args.printer:
            print(f"  restricted to printers: {', '.join(args.printer)}")
        else:
            print("  NOTE: prints can't be filtered by tank, so this pulls the whole\n"
                  "        feed once and groups by tank. Use --since / --printer to bound it.")
        prints = fetch_all_prints(token, since=since_dt,
                                  printers=args.printer or None,
                                  max_pages=args.max_pages)
        print(f"  fetched {len(prints):,} prints total; grouping by tank ...")
        prints_by_tank = aggregate_prints_by_tank(prints)

    # ----------------------------------------------------------------- #
    # Build rows
    # ----------------------------------------------------------------- #
    now = datetime.now(timezone.utc)
    rows, n_over, n_under, n_nodata, n_suspect = [], 0, 0, 0, 0
    for t in sorted(selected, key=lambda x: -(x.get(args.metric) or 0)):
        metric_val = t.get(args.metric)
        st = status_for(metric_val, args.threshold)
        if st == "OVER":
            n_over += 1
        elif st == "UNDER":
            n_under += 1
        else:
            n_nodata += 1

        layers = t.get("layers_printed")
        suspect = isinstance(layers, (int, float)) and layers < review_below
        if suspect:
            n_suspect += 1

        first_fill = parse_dt(t.get("first_fill_date"))
        manufacture = parse_dt(t.get("manufacture_date"))
        last_print = parse_dt(t.get("last_print_date"))
        age_anchor = first_fill or manufacture
        ph = hours(t.get("print_time_ms"))
        lpph = ""
        if isinstance(layers, (int, float)) and isinstance(ph, (int, float)) and ph > 0:
            lpph = round(layers / ph, 1)

        row = {
            "tank_serial": t.get("serial", ""),
            "model_family": model_family(t),
            "tank_type": t.get("tank_type", ""),
            "display_name": t.get("display_name", ""),
            "material": t.get("material", ""),
            "manufacturer": t.get("manufacturer", ""),
            "lot_number": t.get("lot_number", ""),
            "mechanical_version": t.get("mechanical_version", ""),
            "layers_printed": t.get("layers_printed", ""),
            "layer_count": t.get("layer_count", ""),
            "write_count": t.get("write_count", ""),
            "metric_value": metric_val if metric_val is not None else "",
            "status_vs_threshold": st,
            "review_suspect": "YES" if suspect else "",
            "print_time_hours": ph,
            "layers_per_print_hour": lpph,
            "manufacture_date": t.get("manufacture_date", ""),
            "first_fill_date": t.get("first_fill_date", ""),
            "last_print_date": t.get("last_print_date", ""),
            "age_days": days_between(now, age_anchor),
            "days_since_last_print": days_between(now, last_print),
            "inside_printer": t.get("inside_printer", ""),
            "connected_group": t.get("connected_group", ""),
        }

        if args.check_prints:
            s = prints_by_tank.get(normalize(t.get("serial")))
            if s:
                row.update({
                    "prints_total": s["prints_total"],
                    "prints_finished": s["prints_finished"],
                    "prints_aborted": s["prints_aborted"],
                    "prints_error": s["prints_error"],
                    "prints_marked_failure": s["prints_failure_flag"],
                    "prints_failed_total": failed_count(s),
                    "failure_rate_pct": failure_rate_pct(s),
                    "last_print_status": s["last_print_status"],
                })
            else:
                row.update({
                    "prints_total": 0, "prints_finished": "", "prints_aborted": "",
                    "prints_error": "", "prints_marked_failure": "",
                    "prints_failed_total": "", "failure_rate_pct": "",
                    "last_print_status": "",
                })

        if csv_serials is not None:
            row["in_csv"] = "YES" if normalize(t.get("serial")) in csv_serials else "NO"
        rows.append(row)

    fieldnames = ["tank_serial", "model_family", "tank_type", "display_name", "material",
                  "manufacturer", "lot_number", "mechanical_version",
                  "layers_printed", "layer_count", "write_count", "metric_value",
                  "status_vs_threshold", "review_suspect",
                  "print_time_hours", "layers_per_print_hour",
                  "manufacture_date", "first_fill_date", "last_print_date",
                  "age_days", "days_since_last_print",
                  "inside_printer", "connected_group"]
    if args.check_prints:
        fieldnames += ["prints_total", "prints_finished", "prints_aborted",
                       "prints_error", "prints_marked_failure", "prints_failed_total",
                       "failure_rate_pct", "last_print_status"]
    if csv_serials is not None:
        fieldnames.append("in_csv")

    with open(os.path.expanduser(args.out), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # ----------------------------------------------------------------- #
    # Summary
    # ----------------------------------------------------------------- #
    print("\n================  SUMMARY  ================")
    print(f"Filter              : {filt_desc}")
    print(f"Tanks checked       : {len(selected)}")
    print(f"  OVER  {args.threshold:,} layers : {n_over}")
    print(f"  UNDER {args.threshold:,} layers : {n_under}")
    if n_nodata:
        print(f"  No layer data       : {n_nodata}")
    print(f"  Review suspects (<{review_below:,} layers): {n_suspect}")
    print(f"Metric              : {args.metric}")

    # Quick fleet usage stats
    layer_vals = [t.get("layers_printed") for t in selected
                  if isinstance(t.get("layers_printed"), (int, float))]
    if layer_vals:
        layer_vals_sorted = sorted(layer_vals)
        mid = layer_vals_sorted[len(layer_vals_sorted) // 2]
        print(f"\nLayers printed      : min {min(layer_vals):,}  "
              f"median {mid:,}  max {max(layer_vals):,}  "
              f"avg {int(sum(layer_vals)/len(layer_vals)):,}")

    if args.check_prints:
        # Surface the suspect tanks with the worst failure rates.
        suspects = [r for r in rows if r.get("review_suspect") == "YES"
                    and isinstance(r.get("failure_rate_pct"), (int, float))
                    and r.get("prints_total", 0) > 0]
        suspects.sort(key=lambda r: (-r["failure_rate_pct"], r.get("layers_printed") or 0))
        if suspects:
            print(f"\nEarly-failure suspects with prints, worst failure rate first:")
            print(f"  {'serial':<22} {'layers':>8} {'prints':>7} {'failed':>7} {'fail%':>6}")
            for r in suspects[:20]:
                print(f"  {str(r['tank_serial']):<22} "
                      f"{str(r.get('layers_printed','')):>8} "
                      f"{str(r.get('prints_total','')):>7} "
                      f"{str(r.get('prints_failed_total','')):>7} "
                      f"{str(r.get('failure_rate_pct','')):>6}")
            if len(suspects) > 20:
                print(f"  ... and {len(suspects) - 20} more (see CSV).")
        else:
            print("\nNo suspect tanks had prints in the pulled window "
                  "(try widening --since or removing --printer).")

    print(f"\nFull report written to: {args.out}")


if __name__ == "__main__":
    main()
