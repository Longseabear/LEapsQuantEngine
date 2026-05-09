# LEaps Sleeve Workspace

This workspace owns sleeve-specific strategy code and settings for the `LEaps` sleeve.

Initial layout:

```text
sleeves/LEaps/
  alphas/
    momentum.py
    volatility_trailing_stop.py
    etf_rotation.py
  portfolios/
    equal_weight.py
```

Runtime configs can set:

```json
"workspace_path": "sleeves/LEaps"
```

With that setting, relative strategy module references such as `alphas/momentum.py` and `portfolios/equal_weight.py` resolve inside this workspace.

Manage active alpha modules through the runtime config:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-alpha-list configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-alpha-enable configs/runtime/leaps_workspace_smoke.json alphas/momentum.py --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-alpha-disable configs/runtime/leaps_workspace_smoke.json alphas/momentum.py --sleeve-id LEaps
```

Manage the active portfolio construction model the same way:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-portfolio-list configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-portfolio-set configs/runtime/leaps_workspace_smoke.json equal_weight --sleeve-id LEaps
```

After changing active alpha modules or the portfolio model, send the emitted `reload_sleeve` command to apply the new config at a runtime boundary.
