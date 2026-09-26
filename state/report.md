# polybot report

- mode: **live**  |  profile: **aggressive**  |  runs: 11  |  last run: 2026-09-26T08:13:38Z  |  wallet `0xAB4C0a70a928d1cB2f35c6709C8Dd689BA698de2`
- equity: **$0.00** (started $25.00, realized +0.00)
- cash: $0.00  |  deployed: $0.00  |  open positions: 0
- P&L today: +0.00  |  **HALTED: equity 0.00 below floor 5.00 (20% of start); stopping to preserve capital**

## Recent trades

- 2026-09-26T04:31:35Z BUY 5.0 Ohio State @ 0.962 (harvest) Illinois vs. Ohio State
- 2026-09-26T04:31:35Z BUY 5.0 No @ 0.980 (harvest) Will San Marino win on 2026-09-26?
- 2026-09-26T07:17:09Z BUY 12.0 No @ 0.985 (harvest) Exact Score: Júbilo Iwata 0 - 2 Vanraure Hachinohe FC?

## Jev (AI decider)

- model `~typesafe/jev-latest`: 30 favourites gated, 30 markets judged blind, 0 API calls, 60 cache hits, 0 errors
- blind-call accuracy by stated probability (resolved markets only): <=0.05: 0 calls, 0.05-0.5: 0 calls, 0.5-0.95: 0 calls, >=0.95: 0 calls

## Last scan

ai.edge_assessed=30, ai.gate_assessed=30, ai_edge.gap_too_small=1, ai_edge.not_extreme=29, arb.best_pair_x1000=1000, arb.no_book=44, arb.no_edge=331, books=751, candidates=375, harvest.below_min_order=30, harvest.illiquid=8, harvest.in_window=226, harvest.no_book=48, harvest.price_out_of_range=357, harvest.wide_spread=1, harvest.window=147, markets=600

## Notes

- 2026-09-26T08:02:07Z POLYMARKET_PRIVATE_KEY rejected: expected 64 hex characters, got 88 characters
- 2026-09-26T08:03:24Z POLYMARKET_PRIVATE_KEY rejected: got 88 characters: this looks like an API secret from Settings → Developer (base64), not the wallet private key. The wallet key is 64 hex characters; export it at https://reveal.magic.link/polymarket
- 2026-09-26T08:13:38Z mode changed paper -> live; simulated positions cleared
- 2026-09-26T08:13:38Z live wallet 0xAB4C0a70a928d1cB2f35c6709C8Dd689BA698de2 shows $0 USDC. If you have deposited, compare this with the address on your Polymarket profile and set POLYMARKET_FUNDER to that one.
- 2026-09-26T08:13:38Z halted: equity 0.00 below floor 5.00 (20% of start); stopping to preserve capital
