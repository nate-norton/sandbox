# polybot report

- mode: **live**  |  profile: **aggressive**  |  runs: 98  |  last run: 2026-09-27T00:32:35Z  |  wallet `polymarket.us key 865b51eb…`
- equity: **$48.83** (started $59.49, realized +0.00)
- cash: $23.34  |  deployed: $24.23  |  open positions: 3
- P&L today: +0.00

## Open positions

| market | side | size | entry | mark | ends | kind |
|---|---|---|---|---|---|---|
| Oklahoma State vs. West Virginia | Cowboys | 19.0 | 0.508 | 0.480 |  | external |
| Oregon vs. USC | Trojans | 27.0 | 0.354 | 0.455 |  | external |
| Texas A&M vs. LSU | Aggies | 19.0 | 0.263 | 0.215 |  | external |

## Recent trades

- 2026-09-26T04:31:35Z BUY 5.0 Ohio State @ 0.962 (harvest) Illinois vs. Ohio State
- 2026-09-26T04:31:35Z BUY 5.0 No @ 0.980 (harvest) Will San Marino win on 2026-09-26?
- 2026-09-26T07:17:09Z BUY 12.0 No @ 0.985 (harvest) Exact Score: Júbilo Iwata 0 - 2 Vanraure Hachinohe FC?
- 2026-09-26T17:35:26Z BUY 38.0 Maryland @ 0.405 (sports_edge) UCLA vs. Maryland
- 2026-09-26T18:11:58Z BUY 33.0 Georgia State @ 0.770 (sports_edge) Northern Illinois vs. Georgia State

## Jev (AI decider)

- model `~typesafe/jev-latest`: 29 favourites gated, 0 markets judged blind, 2 API calls, 27 cache hits, 0 errors
- blind-call accuracy by stated probability (resolved markets only): <=0.05: 0 calls, 0.05-0.5: 0 calls, 0.5-0.95: 0 calls, >=0.95: 0 calls

## Observation log

- 4817 quote/model snapshots and 41 game outcomes across 3 files in `state/obs/`

## Last scan

ai.edge_assessed=0, ai.gate_assessed=29, books=62, candidates=32, markets=118, obs.outcomes=1, obs.rows=54, sports.games=154, sports.matched=118, sports.moneyline_markets=118, sports.no_book=76, sports.no_edge=32, sports.no_model=80, sports.price_out_of_band=4, sports.stale_book=2, sports.too_far_ahead=19

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
- 2026-09-26T17:33:45Z BOUGHT Maryland @ 0.405 x38 (model 0.57, live_wp) for $15.39
- 2026-09-26T17:37:26Z NOT FILLED Baylor @ 0.785 x33 (model 0.88, live_wp): new: ORD_REJECT_REASON_EXCHANGE_OPTION; expired: ORD_REJECT_REASON_EXCHANGE_OPTION
- 2026-09-26T17:53:18Z NOT FILLED Toledo @ 0.940 x28 (model 0.98, live_wp): new: ORD_REJECT_REASON_EXCHANGE_OPTION; expired: ORD_REJECT_REASON_EXCHANGE_OPTION
- 2026-09-26T18:10:07Z NOT FILLED Boston College @ 0.120 x82 (model 0.27, live_wp): new: ORD_REJECT_REASON_EXCHANGE_OPTION; expired: ORD_REJECT_REASON_EXCHANGE_OPTION
- 2026-09-26T18:10:07Z BOUGHT Georgia State @ 0.775 x33 (model 0.87, live_wp) for $25.41
- 2026-09-26T23:49:59Z skip South Florida @ 0.930 x27 (model 0.97, live_wp): ask moved 0.930 -> 0.970 before the order
- 2026-09-27T00:07:47Z NOT FILLED Kennesaw State @ 0.180 x29 (model 0.27, live_wp): new: ORD_REJECT_REASON_EXCHANGE_OPTION; expired: ORD_REJECT_REASON_EXCHANGE_OPTION
