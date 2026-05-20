# us_etf_rotation

USD-only sleeve workspace for US ETF rotation.

Runtime config:

```text
configs/runtime/us_etf_rotation_sleeve.json
```

Operator notes:

```text
sleeves/us_etf_rotation/OPERATING_NOTES.md
```

The sleeve uses:

```text
sleeves/us_etf_rotation/
  alphas/
    etf_rotation.py
    volatility_trailing_stop.py
  selections/
    etf_rotation.py
    operational_symbols.py
  portfolios/
    equal_weight.py
    rl_ppo_constructor.py
  risks/
    basic.py
  executions/
    immediate.py
```

The executable flow is:

```text
US ETF universe
  -> ETF rotation selection
  -> ETF rotation alpha
  -> RL PPO portfolio construction
  -> basic long-only risk
  -> immediate/standard execution intents
```

This sleeve owns US ETF capital and routes through the overseas USD broker
account boundary. It does not trade US single stocks or Korean symbols.

The active portfolio model borrows the engine's PPO allocator wrapper used by
`LEaps`, but keeps ownership inside this sleeve workspace. The runtime
intentionally falls back to deterministic score-weighted ETF targets through
the same RL constructor surface until an ETF-specific PPO policy beats that
baseline. The first 8,000-step ETF-only PPO policy trained on 2021-05-10 ->
2024-12-31 was too defensive, so it is kept as a research artifact rather than
enabled in runtime config.
