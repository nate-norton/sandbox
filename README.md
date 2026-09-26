# polybot — hands-off Polymarket bot for a $25 bankroll

Runs every 30 minutes on GitHub Actions (free tier), scans every active Polymarket market, and
places small fill-or-kill orders when it finds an edge. It starts in **paper mode** and switches
itself to **live mode** the moment the two secrets below exist. State and a human-readable report
are committed back to the repo after every run (`state/ledger.json`, `state/report.md`).

## What it trades

| strategy | idea | risk |
|---|---|---|
| `pair_arb` | Buy YES **and** NO of the same market when the two asks add up to less than $1.00. The pair always pays exactly $1.00 at resolution. | Risk-free once both legs fill. A half-filled pair is sold back immediately. Capital is locked until the market resolves, so only markets ending within 21 days qualify. |
| `harvest` | Buy the heavy favourite (94c–98.5c) of a liquid market that resolves within 72 hours. Favourites at these prices historically win slightly more often than the price implies, and the capital turns over in days. Taker fees (about 0.4% on sports and crypto markets at these prices) are deducted before a trade qualifies. | Directional. Capped at 20% of equity per position, 8 positions, stop-loss if the price drops 25c, and only on books with tight spreads and real volume. |
| `negrisk_arb` (off by default) | Buy every YES in a multi-outcome event when they add to less than $1.00. | Same as pair_arb, plus the risk that Polymarket adds an outcome to the event later. Turn on with repo variable `ENABLE_NEGRISK_ARB=true`. |

## Bankroll protection (the "balance spend" part)

All in `polybot/config.py`, overridable by env vars / repo variables:

- keeps `CASH_RESERVE_USD` ($2) untouched, never deploys more than 85% of equity
- spends at most `MAX_SPEND_PER_RUN` ($12) per cycle and places at most 6 orders per cycle
- halts for the day after losing 10% of equity, and halts entirely if equity falls under half the starting bankroll
- **kill switch:** commit an empty file at `state/STOP` and the bot stops trading until it is removed
- only fill-or-kill orders are used, so nothing ever rests on the book between runs; any stray open order is cancelled

## One-time setup (about 10 minutes, no paid services)

1. **Fund Polymarket with $25.** Sign up at polymarket.com with an email (this creates a gasless proxy wallet), deposit $25.
2. **Get the two values the bot needs.**
   - Private key: Polymarket → profile menu → *Settings* → *Export private key*.
   - Funder address: your Polymarket wallet address shown on your profile / deposit page (the `0x…` proxy address, **not** the exporting wallet's address).
3. **Add them as GitHub Actions secrets** (repo → *Settings* → *Secrets and variables* → *Actions*):
   - `POLYMARKET_PRIVATE_KEY`
   - `POLYMARKET_FUNDER`
   - If you signed up with a browser wallet instead of email, also add a repository **variable** `POLYMARKET_SIGNATURE_TYPE=2` (email/Magic login is `1`, the default).
4. **Merge this branch into `main`.** GitHub only runs scheduled workflows from the default branch. Until then you can start a cycle by hand from the *Actions* tab (*polybot* → *Run workflow*).

That is everything. From then on:

- every 30 minutes a run scans, trades, and commits `state/report.md` with equity, open positions and the last trades
- the `tests` workflow keeps the code honest on every push
- to pause: add `state/STOP`; to change limits: set repo variables (`BANKROLL_USD`, `HARVEST_ENABLED`, …)

Without the secrets the bot paper-trades with a simulated $25 so you can watch it before funding. The first paper runs are already in `state/report.md`.

## Things to know

- **Winning positions must be redeemed.** After a market resolves, the winnings sit as redeemable shares. Polymarket's website shows a *Claim* button and, for email-login accounts, redeems automatically over time; the bot lists redeemable positions in the report notes. Until they are claimed that cash is not reinvested.
- **Expectations.** With $25 and Polymarket's $5-minimum orders, a good run makes cents, not dollars: a 3% favourite that resolves in two days is about 15c on a $4.80 position. Pair arbitrage is rarer and smaller still because faster bots compete for it. The point of the design is to compound slowly without blowing up, not to get rich.
- **Actions minutes.** Private repos get 2,000 free minutes/month; 48 runs/day at about a minute each fits. Making the repo public removes the limit.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q            # offline tests with fake exchange data
python -m polybot.run          # one paper cycle against the real APIs (needs network)
```

Layout: `gamma.py` (market discovery) → `clob.py` (order books, fees, orders) → `strategies.py`
(pure opportunity finders) → `risk.py` (approval) → `executor.py` (fills, paper or live) →
`ledger.py` (state) ; `run.py` wires one cycle together.
