# kr-lowvol-defensive

`kr-lowvol-defensive` is a KRW-only Korean equity sleeve built from a
LEAN-style defensive factor idea. It is designed to be different from the LEaps
growth/momentum/PPO sleeve: the model prefers liquid, calmer stocks and reduces
gross exposure when the selected basket becomes unstable, crowded, or
lottery-like.

## Model Shape

```text
KRX liquid stock universe
  -> low-volatility / anti-lottery active selection
  -> defensive factor alpha insights
  -> inverse-volatility portfolio construction with crowding haircuts
  -> basic long-only risk gates
  -> standard limit order intents
```

## Strategy Rules

- Avoid ETFs, preferred shares, illiquid names, and very low-priced stocks.
- Rank stocks by normalized volatility, smooth medium-term trend, liquidity,
  valuation support, quality support, dividend support, and drawdown
  discipline.
- Penalize lottery-like structures: large one-day returns, large gaps, wide
  intraday ranges, high z-scores, and elevated realized volatility.
- Penalize real crowding inputs when available: retail net-buy concentration,
  retail-flow z-score, retail participation, and retail absorption against
  foreign/institutional selling.
- Fall back to crowding proxies when those inputs are absent: volume-ratio
  spikes, volume momentum, and turnover shocks paired with price spikes.
- Block falling knives: high drawdown, negative momentum, large gaps, or
  excessive short-term volatility.
- Hold a broad defensive basket seeded from the top 10-15 names, allowing
  existing low-volatility holdings to persist when churn is not justified.
- Penalize unsupported sideways return profiles so a very calm stock does not
  stay attractive purely because it is quiet.
- Weight by inverse volatility with per-position caps and heat penalties.
- If a held symbol drops out of the selected basket, keep it only while the
  signal is recent or the position is already working: a stale 14-day flat or
  losing hold becomes a zero target, and a 28-day stale target is removed even
  if it is not flat.
- Skip fractional-lot targets whose desired value is not at least 1.1 shares,
  preventing expensive names from flipping between 0 and 1 share on intraday
  price noise.
- Keep some cash when breadth is weak, average volatility is high, or the
  selected basket is crowded, but do not let ordinary defensive regimes collapse
  into mostly-cash exposure.
- Live execution allows buys and trims. Rebalance sensitivity is intentionally
  low: portfolio targets refresh weekly, reused targets suppress price-only
  churn, but meaningful cash deployment against the existing basket can still
  pass through the KRW 500k notional filter.

## Runtime

Paper runtime config:

```text
configs/runtime/kr_lowvol_defensive_sleeve.json
```

Universe:

```text
configs/universes/kr_lowvol_defensive_core.json
```

The sleeve is not added to live multi-sleeve runtime by default. The runtime
config is paper research wiring only unless the operator explicitly opts in.

Alpha cadence is `daily_at 08:50 Asia/Seoul`, portfolio construction refreshes
at `week_start_at 08:55 Asia/Seoul`, and buy intents are limited to
`09:05-14:50 Asia/Seoul`.
