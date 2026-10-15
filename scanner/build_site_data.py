#!/usr/bin/env python3
"""
Turn a scanner run into the files the site and the public JSON endpoints serve.

Reads   scanner/results/holder_quality.json   (output of holder_quality.py)
Writes  holder_quality.json                    latest snapshot, read by index.html
        data/history.json                      one compact entry per day
        data/index.json                        summary + list of token endpoints
        data/tokens/<address>.json             latest row + daily history per token

Refuses to publish (exit code 1) when the run looks broken, for example when the
explorer API was down and most tokens came back empty. The previous snapshot then
stays live.

Usage (from the repo root):
  python3 scanner/build_site_data.py
"""

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "scanner", "results", "holder_quality.json")
LATEST = os.path.join(ROOT, "holder_quality.json")
DATA = os.path.join(ROOT, "data")
HISTORY = os.path.join(DATA, "history.json")
TOKENS_DIR = os.path.join(DATA, "tokens")
SITE = "https://elysium-headcount.vercel.app"

MIN_OK_TOKENS = 30          # absolute floor for a usable run
MIN_OK_RATIO = 0.8          # and at least 80% of the previous snapshot's scored tokens
MAX_DAYS = 365


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def dump(path, obj, pretty=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, indent=2)
        else:
            json.dump(obj, f, separators=(",", ":"))
        f.write("\n")


def totals(tokens):
    ok = [t for t in tokens if t.get("status") == "ok"]
    return {
        "tokens": len(ok),
        "raw": sum(t["raw_holders"] for t in ok),
        "eoa": sum(t["eoa_holders"] for t in ok),
        "organic": sum(t["est_organic"] for t in ok),
    }


def main():
    run = load(SRC, None)
    if not run or not run.get("tokens"):
        print(f"No scanner output at {SRC}", file=sys.stderr)
        return 1
    meta, tokens = run["meta"], run["tokens"]

    history = load(HISTORY, {"days": []})
    days = history.get("days", [])
    t = totals(tokens)

    prev_ok = days[-1]["totals"]["tokens"] if days else 0
    floor = max(MIN_OK_TOKENS, int(MIN_OK_RATIO * prev_ok))
    if t["tokens"] < floor:
        print(f"Refusing to publish: only {t['tokens']} scored tokens (need {floor}).", file=sys.stderr)
        return 1

    generated = meta.get("generated_at") or datetime.now(timezone.utc).isoformat()
    day = generated[:10]

    entry = {
        "date": day,
        "generated_at": generated,
        "totals": t,
        # per token: [organic share %, organic holders, reported holders]
        "tokens": {
            x["address"].lower(): [x["quality_pct"], x["est_organic"], x["raw_holders"]]
            for x in tokens if x.get("status") == "ok"
        },
    }
    days = [d for d in days if d["date"] != day] + [entry]
    days.sort(key=lambda d: d["date"])
    days = days[-MAX_DAYS:]
    dump(HISTORY, {"days": days})

    dump(LATEST, run, pretty=True)

    os.makedirs(TOKENS_DIR, exist_ok=True)
    listed = []
    for x in tokens:
        addr = x["address"].lower()
        series = [
            {"date": d["date"], "organic_share_pct": v[0], "organic_holders": v[1], "reported_holders": v[2]}
            for d in days if (v := d["tokens"].get(addr))
        ]
        dump(os.path.join(TOKENS_DIR, addr + ".json"), {
            "address": x["address"],
            "symbol": x["symbol"],
            "name": x["name"],
            "snapshot": generated,
            "latest": x,
            "history": series,
            "method": f"{SITE}/#m-h",
            "note": meta.get("note"),
        }, pretty=True)
        listed.append({
            "address": x["address"],
            "symbol": x["symbol"],
            "status": x["status"],
            "organic_share_pct": x["quality_pct"],
            "url": f"{SITE}/data/tokens/{addr}.json",
        })

    dump(os.path.join(DATA, "index.json"), {
        "snapshot": generated,
        "totals": t,
        "days_recorded": len(days),
        "tokens": sorted(listed, key=lambda r: r["symbol"].lower()),
    }, pretty=True)

    print(f"Published {day}: {t['tokens']} scored tokens, {t['organic']} of {t['eoa']} wallet holders look organic. "
          f"{len(days)} day(s) of history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
