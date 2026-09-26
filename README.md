# polybot — hands-off Polymarket bot for a $25 bankroll

Runs every 30 minutes on GitHub Actions (free tier), scans every active Polymarket market, and
places small fill-or-kill orders when it finds an edge. It starts in **paper mode** and switches
itself to **live mode** the moment the two secrets below exist. State and a human-readable report
are committed back to the repo after every run (`state/ledger.json`, `state/report.md`).

## Sports mode (default): NFL and college football moneylines

The bot only trades games ESPN knows about, priced against ESPN's numbers:

| game state | model probability | source | margin required after fees |
|---|---|---|---|
| before kickoff | no-vig ESPN BET moneyline | ESPN scoreboard `odds` | 4c |
| in progress | ESPN live win probability | ESPN scoreboard `situation` / game summary | 5c, or 3c when the model is ≥90% |
| final, market not yet resolved | 1 or 0 from the final score | ESPN scoreboard | 1c |

For every Polymarket moneyline market matched to a game (both outcome names must be the game's teams, timing must agree),
edge = model − ask − taker fee. Trades need edge above the margin and are sized with half-Kelly on the net odds at the ask,
capped by the risk profile (50% of equity per position on `aggressive`). Jev sees the game state and prices and can veto a
trade when it leans the other way. Held positions are sold when the market bids more than the model says they are worth
(by 5c after fees), so a lead that evaporates is cut rather than ridden to zero. Pair arbitrage still runs on the same markets.

Data comes from ESPN's public scoreboard API, which is free and needs no key. Spreads and totals are not traded yet
(ESPN publishes no probability for them). Set the repo variable `SPORTS_ONLY=false` to fall back to the general strategies below.

## What it trades in general mode

| strategy | idea | risk |
|---|---|---|
| `pair_arb` | Buy YES **and** NO of the same market when the two asks add up to less than $1.00. The pair always pays exactly $1.00 at resolution. | Risk-free once both legs fill. A half-filled pair is sold back immediately. Capital is locked until the market resolves, so only markets ending within 21 days qualify. |
| `harvest` | Buy the heavy favourite (94c–98.5c) of a liquid market that resolves within 72 hours. Favourites at these prices historically win slightly more often than the price implies, and the capital turns over in days. Taker fees (about 0.4% on sports and crypto markets at these prices) are deducted before a trade qualifies. | Directional. Capped at 20% of equity per position, 8 positions, stop-loss if the price drops 25c, and only on books with tight spreads and real volume. |
| `negrisk_arb` (off by default) | Buy every YES in a multi-outcome event when they add to less than $1.00. | Same as pair_arb, plus the risk that Polymarket adds an outcome to the event later. Turn on with repo variable `ENABLE_NEGRISK_ARB=true`. |

## Jev, the AI decider

[Jev](https://openrouter.ai/typesafe/jev-1.13) is TypeSafe's "System One" decision model (released 15 Sep 2026). It does not
write text: it takes state plus typed questions and returns probabilities in ~450 ms for $0.042 per million input tokens.
The bot calls it through OpenRouter's Decisions API (`POST /api/alpha/decisions`) and uses it in three ways:

1. **Gate on favourites.** Every favourite the harvest strategy wants to buy is shown to Jev with its rules, timing and
   current prices. If Jev leans toward the *other* side, or judges the rules ambiguous, the trade is skipped.
   Favourites Jev is surest about are bought first. (In practice Jev hedges: it answers 70–90% on favourites the market
   prices at 96%, so its number is used as a ranking and a disagreement check, not as a probability.)
2. **Blind edge finder.** Liquid markets priced 50c–90c are shown to Jev *without* the price. When Jev answers ≥85%
   (or ≤15%) for a side that the market prices at least 10c cheaper, the bot buys that side (`ai_edge`, capped at 35% of
   equity in the aggressive profile). Jev's number is averaged with the market price before sizing, because independent
   tests found its mid-range probabilities unreliable and only its strong answers informative
   ([Jev-Calibration](https://github.com/AnthusAI/Jev-Calibration), [jev-test](https://github.com/souvikr/jev-test)).
   Judged blind, Jev answers close to 50% on most markets, so these trades are rare by design.
3. **Self-grading.** Every blind call is logged and graded against the real resolution. The report shows Jev's accuracy
   by stated probability, and a circuit breaker pauses edge trades if its extreme answers fall under 88% right across 30+
   resolved markets.

Calls are cached for an hour, capped at 60 markets per run, and cost well under $1/month. Without the
`OPENROUTER_API_KEY` secret the bot runs exactly as before, with Jev off.

## Risk profiles

Set with the repo variable `RISK_PROFILE`. **`aggressive` is the default.**

| | aggressive (default) | conservative |
|---|---|---|
| favourites bought from | 80c | 94c |
| max per position | 50% of equity | 20% of equity |
| max deployed | 100%, no cash reserve | 85%, $2 reserve |
| spend per cycle | unlimited | $12 |
| stop-loss | none | sell if a position drops 25c |
| daily loss halt | 60% of equity | 10% of equity |
| hard stop | equity under 20% of start | equity under 50% of start |
| typical week on $25 | −$25 to +$8 | −$2 to +$1.50 |

Winners compound automatically: every cap is a fraction of current equity, so position sizes grow with the bankroll.
Both profiles share these guards (all overridable by env vars / repo variables, see `polybot/config.py`):

- at most 8 open positions and 6 orders per cycle
- **kill switch:** commit an empty file at `state/STOP` and the bot stops trading until it is removed
- only fill-or-kill orders are used, so nothing ever rests on the book between runs; any stray open order is cancelled

## One-time setup (about 10 minutes, no paid services)

1. **Fund Polymarket with $25.** Sign up at polymarket.com with an email (this creates a gasless proxy wallet), deposit $25.
2. **Export your private key**: Polymarket → *Settings* → *Developer* (or https://reveal.magic.link/polymarket for email accounts).
3. **Add it as a GitHub Actions secret** (repo → *Settings* → *Secrets and variables* → *Actions*):
   - `POLYMARKET_PRIVATE_KEY` (required)
   - `OPENROUTER_API_KEY` or `OPEN_ROUTER_SECRET` (turns on the Jev decider; optional but recommended)
   - `POLYMARKET_FUNDER` (optional). Your Polymarket wallet address is derived from the key automatically, using the
     same formula as Polymarket's own client, and the bot picks whichever derived wallet holds your USDC. Set this
     only if the report shows a $0 balance after you have deposited.
4. **Merge this branch into `main`.** GitHub only runs scheduled workflows from the default branch. Until then you can start a cycle by hand from the *Actions* tab (*polybot* → *Run workflow*).

That is everything. From then on:

- every 30 minutes a run scans, trades, and commits `state/report.md` with equity, open positions and the last trades
- the `tests` workflow keeps the code honest on every push
- to pause: add `state/STOP`; to change limits: set repo variables (`BANKROLL_USD`, `RISK_PROFILE`, `OPENROUTER_MODEL`, `AI_EDGE_MIN_P`, …)

Without the secrets the bot paper-trades with a simulated $25 so you can watch it before funding. The first paper runs are already in `state/report.md`.

## Things to know

- **Winning positions must be redeemed.** After a market resolves, the winnings sit as redeemable shares. Polymarket's website shows a *Claim* button and, for email-login accounts, redeems automatically over time; the bot lists redeemable positions in the report notes. Until they are claimed that cash is not reinvested.
- **Expectations.** A favourite can never pay more than $1 per share, so the upside per trade is 2% to 25% depending on entry price. The aggressive profile bets half the bankroll per position: a run of winners compounds quickly, and a single loser costs half the bankroll. Pair arbitrage is rare and small because faster bots compete for it.
- **Actions minutes.** Private repos get 2,000 free minutes/month; 48 runs/day at about a minute each fits. Making the repo public removes the limit.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q            # offline tests with fake exchange data
python -m polybot.run          # one paper cycle against the real APIs (needs network)
```

Layout: `gamma.py` (market discovery) → `clob.py` (order books, fees, orders) → `espn.py` (live football data) →
`sports.py` / `strategies.py` (pure opportunity finders) → `decider.py` (Jev) → `risk.py` (approval) → `executor.py` (fills, paper or live) →
`ledger.py` (state) ; `run.py` wires one cycle together.
