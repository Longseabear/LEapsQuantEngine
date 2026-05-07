# DEVELOPMENT

## Principles

- Core engine code should stay deterministic and easy to replay.
- Strategy APIs should remain small, LEAN-like, and friendly to iterative research.
- Sleeves are not just folders; they are capital, policy, and responsibility boundaries.
- Execution starts with order intents. Broker-specific submission belongs behind adapters.

## Current Milestone

Create the v0 skeleton:

- package layout
- core models
- algorithm interface
- sleeve-aware engine loop
- execution model
- sample config
- smoke tests

## Commands

```powershell
py -3 -m pytest -q
```

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli run-once sample_swing_kor_pipeline.json
```

KIS adapter smoke check, when the local broker-engine bridge is running:

```powershell
$env:PYTHONPATH='src'
py -3 -c "from leaps_quant_engine.adapters.kis import KISBrokerEngineMarketDataProvider; p=KISBrokerEngineMarketDataProvider.from_env(); print(p.health_check())"
```

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli kis-health
```
