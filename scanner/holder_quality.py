#!/usr/bin/env python3
"""
Elysium Holder Quality
Raw holder count vs. estimated organic holders for every ERC-20 on the Elysium testnet.

Signals (all from the public Blockscout API, read only):
  clone      wallet sits in a balance cluster: many wallets holding almost the same amount
  cross      wallet is a clone-cluster member in 3+ analyzed tokens
  burst      share of pool buys that land in the busiest 6-hour window
  ladder     share of buys inside runs of near-identical, slowly shrinking amounts (scripted buys)
  buyonly    share of buyers that never sold or moved the token
  farm       share of a sampled set of clone wallets with very high tx counts

Usage:
  python3 holder_quality.py                 # all tokens with >= 20 holders
  python3 holder_quality.py --min-holders 100 --max-tokens 30
  python3 holder_quality.py --token 0x77677Ecd3D1712a5D2B0789Ee7EC67A56c1f8888

Output: results/holder_quality.csv, results/holder_quality.json, summary table in the terminal.
Raw API responses are cached in ./cache so reruns are fast. Use --fresh to ignore the cache.
No dependencies beyond the Python 3.9+ standard library.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

API = "https://testnet-api.elysium.kinetiq.xyz/api/v2"
CACHE_DIR = "cache"
OUT_DIR = "results"
USE_CACHE = True
MIN_INTERVAL = 0.35          # seconds between API calls across all threads (rate-limit friendly)
_lock = threading.Lock()
_last_call = [0.0]

# Tunables
BUCKET_WIDTH = 0.03          # balances within ~3% of each other share a bucket
CLONE_MIN_ABS = 15           # a cluster needs at least this many wallets...
CLONE_MIN_REL = 0.03         # ...and at least this share of the token's EOAs
CROSS_MIN_TOKENS = 3         # clone member in this many tokens -> cross flag
LADDER_STEP = 0.002          # consecutive buys differing by < 0.2% count as a ladder step
LADDER_MIN_RUN = 5           # run length that counts as a ladder
FARM_TX_THRESHOLD = 300      # sampled wallet with >= this many txs counts as farm-like
MIN_EOA_FOR_SCORE = 20      # fewer EOA holders than this -> no quality score
FAMILY_OVERLAP = 0.5         # two tokens are one family if shared / union of their EOA holders >= 0.5 (Jaccard)
FAMILY_MIN_SHARED = 50
COMPLETE_RATIO = 0.9         # fetched holders must reach 90% of the reported count
SKIP_SYMBOLS = {"WHYPE", "USDC"}


# ---------------------------------------------------------------- HTTP + cache

def _cache_path(url):
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")


def get_json(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode({k: ("null" if v is None else str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()})
    cp = _cache_path(url)
    if USE_CACHE and os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            return json.load(f)
    last_err = None
    for attempt in range(8):
        with _lock:
            wait = _last_call[0] + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "holder-quality/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                return None
            if e.code == 422:
                break
            retry_after = e.headers.get("retry-after") if e.headers else None
            if e.code == 429:
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60, 5 * 2 ** attempt)
            else:
                delay = 2 * (attempt + 1)
            time.sleep(delay)
        except Exception as e:  # timeouts, resets
            last_err = e
            time.sleep(2 * (attempt + 1))
    print(f"  ! giving up on {url}: {last_err}", file=sys.stderr)
    return None


def paginate(path, max_pages):
    items, params = [], None
    for _ in range(max_pages):
        d = get_json(path, params)
        if not d:
            break
        items.extend(d.get("items", []))
        nxt = d.get("next_page_params")
        if not nxt:
            break
        params = nxt
    return items


# ---------------------------------------------------------------- helpers

def to_units(raw, decimals):
    try:
        return int(raw) / (10 ** decimals)
    except (TypeError, ValueError):
        return 0.0


def bucket_key(v):
    if v <= 0:
        return None
    return int(math.floor(math.log(v) / math.log(1 + BUCKET_WIDTH)))


def addr(x):
    return (x or {}).get("hash", "").lower() if isinstance(x, dict) else ""


def ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------- collection

def list_tokens(min_holders, max_tokens):
    out = []
    for t in paginate("/tokens", 40):
        if t.get("type") != "ERC-20":
            continue
        if (t.get("symbol") or "").upper() in SKIP_SYMBOLS:
            continue
        h = int(t.get("holders_count") or t.get("holders") or 0)
        if h < min_holders:
            continue
        out.append(t)
    out.sort(key=lambda t: -int(t.get("holders_count") or t.get("holders") or 0))
    return out[:max_tokens] if max_tokens else out


def collect_token(t, holder_pages, transfer_pages):
    a = t.get("address_hash") or t.get("address")
    dec = int(t.get("decimals") or 18)
    holders = []
    for h in paginate(f"/tokens/{a}/holders", holder_pages):
        holders.append({
            "a": addr(h.get("address")),
            "c": bool((h.get("address") or {}).get("is_contract")),
            "v": to_units(h.get("value"), dec),
        })
    transfers = []
    for x in paginate(f"/tokens/{a}/transfers", transfer_pages):
        total = x.get("total") or {}
        transfers.append({
            "f": addr(x.get("from")),
            "t": addr(x.get("to")),
            "fc": bool((x.get("from") or {}).get("is_contract")),
            "tc": bool((x.get("to") or {}).get("is_contract")),
            "v": to_units(total.get("value"), int(total.get("decimals") or dec)),
            "ts": ts(x.get("timestamp") or ""),
        })
    return {"token": t, "address": a, "holders": holders, "transfers": transfers}


# ---------------------------------------------------------------- analysis

def clone_members(holders):
    eoa = [h for h in holders if not h["c"] and h["v"] > 0]
    buckets = defaultdict(list)
    for h in eoa:
        k = bucket_key(h["v"])
        buckets[k].append(h["a"])
    # merge each bucket with its neighbour so a cluster split across a boundary still counts
    keys = sorted(buckets)
    members, largest = set(), 0
    threshold = max(CLONE_MIN_ABS, CLONE_MIN_REL * len(eoa))
    for k in keys:
        group = buckets[k] + buckets.get(k + 1, [])
        if len(group) >= threshold:
            members.update(group)
        largest = max(largest, len(group))
    return eoa, members, largest


def pool_address(holders):
    contracts = [h for h in holders if h["c"]]
    return max(contracts, key=lambda h: h["v"])["a"] if contracts else None


def transfer_signals(transfers, pool):
    if not transfers:
        return {"buys": 0, "burst": 0.0, "ladder": 0.0, "buyonly": 0.0}
    buys = sorted([x for x in transfers if pool and x["f"] == pool], key=lambda x: x["ts"])
    # also count router-mediated buys: token leaves a contract to an EOA
    if not buys:
        buys = sorted([x for x in transfers if x["fc"] and x["t"]], key=lambda x: x["ts"])
    n = len(buys)
    if n == 0:
        return {"buys": 0, "burst": 0.0, "ladder": 0.0, "buyonly": 0.0}

    # burst: busiest 6h window
    times = [b["ts"] for b in buys]
    best, j = 0, 0
    for i in range(n):
        while times[i] - times[j] > 6 * 3600:
            j += 1
        best = max(best, i - j + 1)
    burst = best / n

    # ladder: runs of near-identical amounts in time order
    in_ladder, run = 0, 1
    for i in range(1, n + 1):
        same = False
        if i < n:
            a, b = buys[i - 1]["v"], buys[i]["v"]
            same = a > 0 and abs(a - b) / a < LADDER_STEP
        if same:
            run += 1
        else:
            if run >= LADDER_MIN_RUN:
                in_ladder += run
            run = 1
    ladder = in_ladder / n

    buyers = {x["t"] for x in transfers if x["fc"] and x["t"] and not x.get("tc")}
    senders = {x["f"] for x in transfers if not x["fc"]}
    buyonly = len(buyers - senders) / len(buyers) if buyers else 0.0
    return {"buys": n, "burst": burst, "ladder": ladder, "buyonly": buyonly}


def farm_ratio(wallets, sample_size, cache):
    sample = sorted(wallets)[:: max(1, len(wallets) // sample_size or 1)][:sample_size]
    if not sample:
        return None, 0
    hits = 0
    for w in sample:
        if w not in cache:
            c = get_json(f"/addresses/{w}/counters") or {}
            cache[w] = int(c.get("transactions_count") or 0)
        if cache[w] >= FARM_TX_THRESHOLD:
            hits += 1
    return hits / len(sample), len(sample)


def analyze(data, sample_size):
    rows, clone_sets, txcache = [], {}, {}
    for d in data:
        eoa, members, largest = clone_members(d["holders"])
        clone_sets[d["address"]] = members
        d["_eoa"], d["_largest"] = eoa, largest

    cross_count = Counter()
    for s in clone_sets.values():
        cross_count.update(s)

    # families: tokens held by largely the same wallet set
    eoa_sets = {d["address"]: {h["a"] for h in d["_eoa"]} for d in data}
    parent = {a: a for a in eoa_sets}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    keys = list(eoa_sets)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            A, B = eoa_sets[keys[i]], eoa_sets[keys[j]]
            if min(len(A), len(B)) < FAMILY_MIN_SHARED:
                continue
            shared = len(A & B)
            if shared >= FAMILY_MIN_SHARED and shared / len(A | B) >= FAMILY_OVERLAP:
                parent[find(keys[i])] = find(keys[j])
    groups = defaultdict(list)
    for a in keys:
        groups[find(a)].append(a)
    family_of, fam_no = {}, 0
    for root, members_ in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(members_) > 1:
            fam_no += 1
            for a in members_:
                family_of[a] = (f"F{fam_no}", len(members_))

    for d in data:
        t, eoa = d["token"], d["_eoa"]
        members = clone_sets[d["address"]]
        eoa_set = {h["a"] for h in eoa}
        cross = {w for w in eoa_set if cross_count[w] >= CROSS_MIN_TOKENS}
        flagged = members | cross
        pool = pool_address(d["holders"])
        sig = transfer_signals(d["transfers"], pool)
        fr, fn = farm_ratio(members, sample_size, txcache) if sample_size else (None, 0)

        n_eoa = len(eoa_set)
        organic = n_eoa - len(flagged & eoa_set)
        total_supply = sum(h["v"] for h in d["holders"]) or 1
        top_eoa = max((h["v"] for h in eoa), default=0)
        pool_share = next((h["v"] for h in d["holders"] if h["a"] == pool), 0) / total_supply

        raw_holders = int(t.get("holders_count") or t.get("holders") or 0)
        fetched = len(d["holders"])
        if fetched == 0:
            status = "failed"
        elif raw_holders and fetched < COMPLETE_RATIO * raw_holders:
            status = "partial"
        elif n_eoa < MIN_EOA_FOR_SCORE:
            status = "too_few_eoa"
        else:
            status = "ok"
        quality = round(100 * organic / n_eoa, 1) if status == "ok" else None
        fam, fam_size = family_of.get(d["address"], ("", 1))
        rows.append({
            "status": status,
            "family": fam,
            "family_size": fam_size,
            "symbol": t.get("symbol"),
            "name": t.get("name"),
            "address": d["address"],
            "raw_holders": raw_holders,
            "eoa_holders": n_eoa,
            "clone_wallets": len(members),
            "largest_cluster": d["_largest"],
            "cross_token_wallets": len(cross),
            "est_organic": organic,
            "quality_pct": quality,
            "pool_share_pct": round(100 * pool_share, 1),
            "top_eoa_share_pct": round(100 * top_eoa / total_supply, 1),
            "pool_buys_seen": sig["buys"],
            "burst_6h_pct": round(100 * sig["burst"], 1),
            "ladder_pct": round(100 * sig["ladder"], 1),
            "buy_only_pct": round(100 * sig["buyonly"], 1),
            "farm_sample_pct": None if fr is None else round(100 * fr, 1),
            "farm_sample_n": fn,
            "volume_24h_usd": round(float(t.get("volume_24h") or 0), 2),
            "market_cap_usd": round(float(t.get("circulating_market_cap") or 0), 2),
            "holders_fetched": len(d["holders"]),
            "transfers_fetched": len(d["transfers"]),
        })
    rows.sort(key=lambda r: (r["status"] != "ok", r["family"] == "", r["family"], -r["raw_holders"]))
    return rows


# ---------------------------------------------------------------- output

def write_outputs(rows, meta):
    os.makedirs(OUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUT_DIR, "holder_quality.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    json_path = os.path.join(OUT_DIR, "holder_quality.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "tokens": rows}, f, indent=2)
    return csv_path, json_path


def print_table(rows):
    cols = [("symbol", 10), ("status", 7), ("family", 6), ("raw_holders", 7), ("clone_wallets", 7), ("cross_token_wallets", 7),
            ("est_organic", 7), ("quality_pct", 7), ("ladder_pct", 7), ("burst_6h_pct", 7),
            ("farm_sample_pct", 7), ("volume_24h_usd", 10)]
    head = ["Token", "Status", "Family", "Raw", "Clone", "Cross", "Organic", "Qual%", "Ladder%", "Burst%", "Farm%", "Vol24h$"]
    print("  ".join(h.rjust(w) if i else h.ljust(w) for i, (h, (_, w)) in enumerate(zip(head, cols))))
    for r in rows:
        cells = []
        for i, (k, w) in enumerate(cols):
            v = r[k]
            s = "-" if v is None else str(v)
            cells.append(s.ljust(w) if i == 0 else s.rjust(w))
        print("  ".join(cells))


# ---------------------------------------------------------------- main

def main():
    global USE_CACHE
    p = argparse.ArgumentParser(description="Elysium holder quality scanner")
    p.add_argument("--min-holders", type=int, default=20)
    p.add_argument("--max-tokens", type=int, default=0, help="0 = no limit")
    p.add_argument("--token", action="append", help="analyze only these token addresses (repeatable)")
    p.add_argument("--holder-pages", type=int, default=500, help="20 holders per page")
    p.add_argument("--transfer-pages", type=int, default=100, help="20 transfers per page")
    p.add_argument("--sample", type=int, default=15, help="clone wallets to check tx counts per token (0 = skip)")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--fresh", action="store_true", help="ignore cached API responses")
    args = p.parse_args()
    USE_CACHE = not args.fresh

    started = time.time()
    if args.token:
        tokens = [get_json(f"/tokens/{a}") for a in args.token]
        tokens = [t for t in tokens if t]
    else:
        print("Fetching token list...")
        tokens = list_tokens(args.min_holders, args.max_tokens)
    if not tokens:
        print("No tokens matched.")
        return 1
    print(f"Analyzing {len(tokens)} tokens with {args.workers} workers...")

    data = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(collect_token, t, args.holder_pages, args.transfer_pages): t for t in tokens}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            try:
                d = f.result()
                data.append(d)
                print(f"  [{i}/{len(tokens)}] {t.get('symbol')}: {len(d['holders'])} holders, {len(d['transfers'])} transfers")
            except Exception as e:
                print(f"  [{i}/{len(tokens)}] {t.get('symbol')}: failed ({e})", file=sys.stderr)

    print("Scoring...")
    rows = analyze(data, args.sample)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api": API,
        "params": vars(args),
        "thresholds": {
            "bucket_width": BUCKET_WIDTH, "clone_min_abs": CLONE_MIN_ABS, "clone_min_rel": CLONE_MIN_REL,
            "cross_min_tokens": CROSS_MIN_TOKENS, "ladder_step": LADDER_STEP, "ladder_min_run": LADDER_MIN_RUN,
            "farm_tx_threshold": FARM_TX_THRESHOLD, "family_overlap": FAMILY_OVERLAP, "family_min_shared": FAMILY_MIN_SHARED,
        },
        "note": "Estimates from public testnet data. Flags describe on-chain patterns, not identity or intent.",
    }
    csv_path, json_path = write_outputs(rows, meta)
    print()
    print_table(rows)
    fams = Counter(r["family"] for r in rows if r["family"])
    for f, n in sorted(fams.items(), key=lambda kv: -kv[1]):
        syms = [r["symbol"] for r in rows if r["family"] == f]
        print(f"  {f}: {n} tokens share most of their holders -> {', '.join(map(str, syms[:12]))}{' ...' if len(syms) > 12 else ''}")
    bad = sum(1 for r in rows if r["status"] in ("failed", "partial"))
    if bad:
        print(f"  {bad} tokens incomplete (rate limit). Run the same command again: cached pages are reused, missing ones are fetched.")
    print(f"\nSaved {csv_path} and {json_path} in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
