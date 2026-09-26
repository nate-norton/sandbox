# polybot report

- mode: **live**  |  profile: **aggressive**  |  runs: 82  |  last run: 2026-09-26T18:38:18Z  |  wallet `polymarket.us key 865b51eb…`
- equity: **$41.23** (started $59.49, realized +0.00)
- cash: $3.37  |  deployed: $56.63  |  open positions: 5
- P&L today: +0.00

## Open positions

| market | side | size | entry | mark | ends | kind |
|---|---|---|---|---|---|---|
| Ole Miss vs. Florida | Rebels | 12.0 | 0.417 | 0.385 |  | external |
| Northern Illinois vs. Georgia State | Panthers | 33.0 | 0.782 | 0.675 |  | sports_edge |
| Texas vs. Tennessee | Volunteers | 14.0 | 0.357 | 0.335 |  | external |
| Texas A&M vs. LSU | Aggies | 19.0 | 0.263 | 0.240 |  | external |
| UCLA vs. Maryland | Terrapins | 38.0 | 0.416 | 0.045 |  | sports_edge |

## Recent trades

- 2026-09-26T04:31:35Z BUY 5.0 Ohio State @ 0.962 (harvest) Illinois vs. Ohio State
- 2026-09-26T04:31:35Z BUY 5.0 No @ 0.980 (harvest) Will San Marino win on 2026-09-26?
- 2026-09-26T07:17:09Z BUY 12.0 No @ 0.985 (harvest) Exact Score: Júbilo Iwata 0 - 2 Vanraure Hachinohe FC?
- 2026-09-26T17:35:26Z BUY 38.0 Maryland @ 0.405 (sports_edge) UCLA vs. Maryland
- 2026-09-26T18:11:58Z BUY 33.0 Georgia State @ 0.770 (sports_edge) Northern Illinois vs. Georgia State

## Jev (AI decider)

- model `~typesafe/jev-latest`: 29 favourites gated, 0 markets judged blind, 15 API calls, 29 cache hits, 0 errors
- blind-call accuracy by stated probability (resolved markets only): <=0.05: 0 calls, 0.05-0.5: 0 calls, 0.5-0.95: 0 calls, >=0.95: 0 calls

## Observation log

- 3929 quote/model snapshots and 0 game outcomes across 1 files in `state/obs/`

## Last scan

ai.edge_assessed=0, ai.gate_assessed=29, books=63, candidates=33, markets=136, obs.rows=59, sports.below_min_order=1, sports.games=145, sports.matched=136, sports.moneyline_markets=136, sports.no_book=104, sports.no_edge=30, sports.no_model=36, sports.price_out_of_band=11, sports.stale_book=4, sports.too_far_ahead=39, sports.unstable=2

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
