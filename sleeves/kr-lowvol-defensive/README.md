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
- Penalize crowding proxies: volume-ratio spikes, volume momentum, and
  turnover shocks paired with price spikes.
- Block falling knives: high drawdown, negative momentum, large gaps, or
  excessive short-term volatility.
- Hold up to 10-12 names.
- Weight by inverse volatility with per-position caps and heat penalties.
- Keep cash when breadth is weak, average volatility is high, or the selected
  basket is crowded.

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
