# Elysium Headcount

Holder count is the easiest number to fake on a launchpad. Elysium Headcount takes every Elysium testnet token with 100+ holders and separates holders that look like people from wallets that move in lockstep.

**Live:** [elysium-headcount.vercel.app](https://elysium-headcount.vercel.app)

## What the first snapshot found

Snapshot of 15 October 2026, 70 scored tokens with 100+ holders:

- 63,739 holders reported. 5,252 look organic, about 8% of wallet holders.
- In 60 of 70 tokens, fewer than 10% of holders look organic.
- 27 tokens are held by one set of about 1,240 wallets. Each of them shows ~1,250 holders, and between 6 and 35 of those look organic.
- A second pattern comes from task-farm wallets: wallets with hundreds of transactions that buy many tokens as one step of a scripted task list.
- The tokens that do look organic (KALOS, stKALOS, HYPERLISA) are small, and KALOS still has one wallet holding about half its supply. Holder quality and concentration are separate questions, so the page shows both.

## Why it matters for Ascend

Ascend plans to qualify tokens for Ascended status, buybacks and a path to HyperCore on criteria that include unique holders, volume and checks for concentrated or coordinated accumulation. On today's testnet data, raw holder counts would rank scripted tokens above real ones. Headcount is a public, reproducible way to see the difference before any capital is routed.

## How it works

`scanner/holder_quality.py` reads the public Elysium explorer API, so it needs no keys and sends no transactions. For each token it fetches the full holder list and up to the 2,000 most recent transfers, then computes:

| Signal | What it measures |
| --- | --- |
| Balance clusters | Wallets holding almost the same amount, in groups of at least 15 wallets and 3% of holders |
| Repeat wallets | Wallets that sit in a balance cluster in three or more tokens |
| Ladder buys | Runs of 5+ consecutive buys that differ by less than 0.2%, the trace of a fixed-size buy script |
| Burst | Share of buys inside the busiest 6-hour window |
| Buy only | Share of wallets that received the token from a pool or router and never sent it on |
| Task-farm sample | Share of 15 sampled clustered wallets with 300+ transactions |
| Top wallet | Share of supply held by the largest non-contract wallet |
| Wallet family | Tokens whose holder sets overlap with a Jaccard index of 0.5 or more |

**Look organic** = wallet holders that are neither in a balance cluster nor a repeat wallet. Contracts such as pools are left out of both sides.

## Run it yourself

Python 3.9+ standard library only.

```sh
cd scanner
python3 holder_quality.py --min-holders 100
```

The first run takes 30 to 40 minutes because the explorer API is rate limited. Responses are cached in `scanner/cache/`, so later runs take about a minute. Results are written to `scanner/results/holder_quality.json` and `.csv`. Copy the JSON next to `index.html` to update the site.

To serve the site locally:

```sh
python3 -m http.server 4173
```

## Limits

- Flags describe on-chain patterns. They do not identify people or prove intent, and a careful operator can run wallets that pass every check.
- Testnet price, volume and market cap fields are sparse and can be manipulated.
- Transfer-based signals use the most recent 2,000 transfers per token. Holder-based signals use the full list.

## Roadmap

- Scheduled snapshots, so every token gets a holder-quality history instead of a single reading.
- A public JSON endpoint per token, so launch pages, explorers and Ascend's own qualification process can read the same numbers.
- Wallet lookup: paste an address and see which clusters and families it belongs to.
- Alerts when a token's organic share moves sharply or a new wallet family appears.
- Mainnet support once Ascend launches on Elysium mainnet.

## Disclaimer

Independent community project. Not affiliated with Ascend, Kinetiq or Elysium. Not financial advice.
