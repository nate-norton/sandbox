# polybot report

- mode: **live**  |  profile: **aggressive**  |  runs: 63  |  last run: 2026-09-26T17:23:29Z  |  wallet `polymarket.us key 865b51eb…`
- equity: **$56.28** (started $59.49, realized +0.00)
- cash: $45.00  |  deployed: $14.99  |  open positions: 3
- P&L today: +0.00

## Open positions

| market | side | size | entry | mark | ends | kind |
|---|---|---|---|---|---|---|
| Ole Miss vs. Florida | Rebels | 12.0 | 0.417 | 0.385 |  | external |
| Texas vs. Tennessee | Volunteers | 14.0 | 0.357 | 0.150 |  | external |
| Texas A&M vs. LSU | Aggies | 19.0 | 0.263 | 0.240 |  | external |

## Recent trades

- 2026-09-26T04:31:35Z BUY 5.0 Ohio State @ 0.962 (harvest) Illinois vs. Ohio State
- 2026-09-26T04:31:35Z BUY 5.0 No @ 0.980 (harvest) Will San Marino win on 2026-09-26?
- 2026-09-26T07:17:09Z BUY 12.0 No @ 0.985 (harvest) Exact Score: Júbilo Iwata 0 - 2 Vanraure Hachinohe FC?

## Jev (AI decider)

- model `~typesafe/jev-latest`: 17 favourites gated, 0 markets judged blind, 0 API calls, 17 cache hits, 0 errors
- blind-call accuracy by stated probability (resolved markets only): <=0.05: 0 calls, 0.05-0.5: 0 calls, 0.5-0.95: 0 calls, >=0.95: 0 calls

## Observation log

- 2821 quote/model snapshots and 0 game outcomes across 1 files in `state/obs/`

## Last scan

ai.edge_assessed=0, ai.gate_assessed=17, books=38, candidates=18, markets=136, obs.rows=36, sports.games=145, sports.matched=136, sports.moneyline_markets=136, sports.no_book=124, sports.no_edge=18, sports.no_model=34, sports.price_out_of_band=6, sports.too_far_ahead=40, sports.unstable=4

## Notes

- 2026-09-26T08:02:07Z POLYMARKET_PRIVATE_KEY rejected: expected 64 hex characters, got 88 characters
- 2026-09-26T08:03:24Z POLYMARKET_PRIVATE_KEY rejected: got 88 characters: this looks like an API secret from Settings → Developer (base64), not the wallet private key. The wallet key is 64 hex characters; export it at https://reveal.magic.link/polymarket
- 2026-09-26T08:13:38Z mode changed paper -> live; simulated positions cleared
- 2026-09-26T08:13:38Z live wallet 0xAB4C0a70a928d1cB2f35c6709C8Dd689BA698de2 shows $0 USDC. If you have deposited, compare this with the address on your Polymarket profile and set POLYMARKET_FUNDER to that one.
- 2026-09-26T08:13:38Z halted: equity 0.00 below floor 5.00 (20% of start); stopping to preserve capital
- 2026-09-26T08:17:12Z halted: no funds in the wallet yet
- 2026-09-26T08:30:44Z mode changed live -> paper; simulated positions cleared
- 2026-09-26T08:37:18Z mode changed paper -> live; simulated positions cleared
- 2026-09-26T08:38:23Z mode changed live -> paper; simulated positions cleared
- 2026-09-26T08:56:32Z mode changed paper -> live; simulated positions cleared

## Recent decisions

- 2026-09-26T17:05:54Z NOT FILLED Toledo @ 0.710 x26 (model 0.80, live_wp): unmatched
