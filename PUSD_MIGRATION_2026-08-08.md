# Polymarket has migrated settlement to PUSD — discovery and evidence (2026-08-08)

Found the same afternoon the wallet key was installed, because the user pushed back on
"the exchange needs USDC.e" — they were right. Everything below was verified directly
on-chain or against Polymarket's own APIs on 2026-08-08; nothing is from prior knowledge
(official client libraries and this repo's earlier audits all predate the migration).

## The finding, in plain terms

Polymarket's money is now **PUSD ("Polymarket USD")** — an ERC-20 on Polygon at
`0xC011a7E12A19F7b1F670d46f03B03F3342e82DFB` (6 decimals, ~$1.00, listed on CoinGecko as
`polymarket-usd`, active Uniswap V3/V4 pools against both USDC flavors). Trades no longer
settle through the exchange contracts this repo's Tier C work was built against — they
settle through **two new exchange contracts whose collateral is PUSD**.

| | old (retired) | new (live) |
|---|---|---|
| regular (binary) exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` | `0xe111180000d2663c0091e4f400237545b87b996b` |
| negRisk exchange (weather boards) | `0xC5d563A36AE78145C45a50134d48A1215220f80a` | `0xe2222d279d744050d28e00520010520000310f59` |
| collateral | USDC.e `0x2791…4174` | PUSD `0xC011…2DFB` |
| negRisk adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` | same — still appears in new negRisk settlements |
| conditional tokens | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | same — still the position ledger |

New-exchange A/B roles are inferred from settlement traces: negRisk trades (soccer
multi-outcome) hit `0xe2222…` together with the old adapter; binary trades (crypto
up/down) hit `0xe111…`. **Not yet confirmed against official docs — do that before
approving or signing anything** (open item 1 below).

## Evidence trail (all reproducible)

1. **Old exchanges are idle**: `eth_getLogs` for `OrderFilled` on both old exchanges over
   ~50 min of Polygon blocks → **0 events**, while PUSD showed ~92k `Transfer` events in
   20 min. (RPC: `polygon-bor-rpc.publicnode.com`; drpc.org also works from this box —
   polygon-rpc.com, llamarpc, 1rpc, ankr, blastapi all refuse it.)
2. **The busy contracts self-identify**: the two highest-volume PUSD movers are contracts
   answering `getCollateral()` → PUSD (selector from `keccak("getCollateral()")`; same
   call on the old exchanges returns USDC.e — my first check asked the old contracts and
   got a stale answer, which is how the wrong-way swap happened).
3. **Real trades land there**: took live trades from
   `https://data-api.polymarket.com/trades?limit=6&takerOnly=true`, fetched each
   `transactionHash` receipt → every one executed against `0xe111…` or `0xe2222…`.
4. **Traders hold PUSD**: decoded one receipt's token transfers — makers paid PUSD in,
   taker was paid PUSD out, fee (to `0x115f48dc2a731aa16251c6d6e1befc42f92accc9`) taken
   in PUSD. No USDC.e touched the trader.
5. **Official clients are behind**: newest `py-clob-client` is still our pinned 0.34.6
   (2026-02-19); newest JS `@polymarket/clob-client` 5.8.1 (2026-03-23) still hardcodes
   the OLD addresses in `dist/config.js`. So the C7 signing path must override the
   exchange addresses itself — no library upgrade will do it yet.
6. **PUSD is not a simple on-contract wrapper**: PUSD supply ~464M but the PUSD contract
   itself holds only ~$3k of USDC.e — backing lives elsewhere. Conversion is via DEX
   (or Polymarket's own deposit flow), effectively 1:1 with small fees.

## What did NOT change (verified live, 2026-08-08)

**The weather fee schedule is intact** — checked via gamma API on a live board
(`highest-temperature-in-london-on-august-9-2026`):
`feeType: weather_fees`, `feeSchedule: {rate: 0.05, exponent: 1, takerOnly: true,
rebateRate: 0.25}`, `maker_base_fee = taker_base_fee = 1000` on the CLOB market object.
The audited fee model (`fee = shares · 0.05 · p · (1−p)`, takers only) and therefore all
Tier A/B P&L numbers **stand**. (Caution: crypto up/down markets showed a 7% rate — the
fee factor is market-class-specific; never assume 5% outside weather.)

The scanner and WS feed read prices from the same CLOB/WS APIs as before and are
unaffected; both services kept running throughout.

## Wallet state (2026-08-08 end of day)

- `.env` now carries the live key set: signature type 0 (direct EOA), funder/signer
  `0xFBE59D4217e3456F51A02183117332EA80ff8865`. Key verified to control that address;
  never committed, never logged.
- On-chain: **253.85 PUSD** (bankroll) + **~183 POL** (gas, years' worth). The PUSD →
  USDC.e → PUSD round trip cost ≈ $4.70 (my wrong call; user's pushback caught it).
- `backend/exec/wallet_status.py` re-checks all of this in one command.

## Open items, in order (all mine unless marked)

1. Confirm the two new exchange addresses + adapter against an official source
   (docs.polymarket.com / Polymarket announcements) before any approval or signed order.
2. Point C7 signing at the new addresses — py-clob-client builds the EIP-712 order with
   `verifyingContract` from its hardcoded config; override it (do NOT approve/sign against
   old addresses — orders would be invalid or rejected). Build + adversarial audit per
   the working agreement, regression tests after.
3. One-time approvals, **in PUSD**, to the NEW contracts (and `setApprovalForAll` on
   conditional tokens as required for the short side). A few cents of POL each.
4. The tiny live dry-run order (C7's final proof), then the authenticated latency legs
   (C8 numbers were unauthenticated), then D-tier per the existing plan.
5. Re-verify the short-side `convertPositions` mechanics (B5/B6 assumed the old adapter;
   it *appears* unchanged, but the D-tier short-side start depends on it).
