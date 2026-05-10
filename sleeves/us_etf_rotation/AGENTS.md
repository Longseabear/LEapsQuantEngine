# AGENTS.md

## Scope

`us_etf_rotation` is a USD-only sleeve workspace for US ETF rotation.

## Rules

- Trade universe inputs must be US ETFs only.
- Alpha models emit insights only; they must not create orders or touch broker APIs.
- Portfolio, risk, and execution stay behind the engine framework interfaces.
- Keep state sleeve-local. Do not share mutable portfolio state with `LEaps` or other sleeves.
- KIS and broker-engine access must remain outside sleeve model code.

## Verification

Run:

```powershell
py -3 -m pytest -q
```
