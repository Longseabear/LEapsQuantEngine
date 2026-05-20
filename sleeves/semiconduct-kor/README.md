# semiconduct-kor

KRW-only Korean semiconductor sleeve workspace.

Runtime config:

```text
configs/runtime/semiconduct_kor_sleeve.json
```

Initial executable flow:

```text
semiconductor KRX universe
  -> Samsung core selection
  -> Samsung strike-risk-off buy-only re-entry alpha
  -> Samsung buy-only portfolio construction
  -> basic long-only risk
  -> standard limit execution intents
```

Workspace layout:

```text
sleeves/semiconduct-kor/
  alphas/
    samsung_strike_risk_reentry.py
    samsung_steward.py
    semiconductor_momentum.py
    volatility_trailing_stop.py
  selections/
    samsung_core.py
    semiconductor_momentum.py
    operational_symbols.py
  portfolios/
    samsung_buy_only.py
    equal_weight.py
    samsung_steward.py
  risks/
    basic.py
  executions/
    immediate.py
```

The default runtime is `paper` and routes through a paper domestic broker account.
The active profile is buy-only. It assumes the operator may hold KRW cash after
a manual Samsung Electronics (`KRX:005930`) sale and wants staged re-entry only
after strike risk is no longer active. The configured alpha never emits `FLAT`
or `DOWN`, and the configured portfolio model never emits a target below the
current Samsung weight. If strike risk turns back on, the sleeve freezes new
buys instead of selling.

The strike re-entry alpha consumes strike risk state from snapshot metadata or
model state. Missing strike state defaults to `on`, which blocks new buys. The
accepted states are `on`, `easing`, `off_candidate`, and `off_confirmed`.
`off_candidate` can only trigger a small probe after dynamic bottom confirmation;
`off_confirmed` can stage reclaim/rebuild/core adds. Price gates use rolling
lows, moving averages, returns, close-location value, momentum, and volatility,
not fixed price constants.

The older `samsung_steward.py` and `volatility_trailing_stop.py` models remain
in the workspace as research/reference modules, but they are not wired into the
default runtime because they can emit sell/trim signals.

v2 adds a conservative `risk_capitulation_accumulate` mode for the Samsung core:
when risk-off is severe but not a hard exit, the model first defends down toward
the 35% core target, then allows small 5 percentage-point re-adds only if live
price trades below the capitulation trigger and above the stop guard.

v3 adds a recovery DCA re-entry layer for post-stress conditions. After a manual
or model-driven exit creates KRW cash, the alpha does not jump back to full core
exposure just because the daily snapshot is no longer risk-off. It waits for
stabilization, then emits staged `accumulate_reentry_*` insights:

- probe: first stabilization after stress, capped at 25%
- reclaim: SMA20/short momentum repair, capped at 45%
- rebuild: SMA60/medium momentum repair, capped at 65%
- core rebuild remains gradual, with cooldown holding the last re-entry target
  for three days before another add can occur

Useful checks:

```powershell
py -3 -m leaps_quant_engine.cli runtime-config-validate configs/runtime/semiconduct_kor_sleeve.json
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/semiconduct_kor_sleeve.json --sleeve-id semiconduct-kor --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/semiconduct_kor_sleeve.json --sleeve-id semiconduct-kor --start 2026-05-08 --end 2026-05-08 --source finance-datareader --summary-only
```
