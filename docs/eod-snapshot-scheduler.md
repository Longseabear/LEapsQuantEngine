# EOD Portfolio Snapshot Scheduler

The EOD snapshot tools keep an operator-readable, append-only portfolio audit
trail for roughly one month. They are report-only: they do not submit, cancel,
or reconcile orders.

## One-Shot Snapshot

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\leaps_eod_snapshot.ps1 `
  -Label manual `
  -RetentionDays 31
```

Default targets:

- `LEaps` from `configs/runtime/live_multi_sleeve.json`
- `kr-lowvol-defensive` from `configs/runtime/live_multi_sleeve.json`
- `us_etf_rotation` from `configs/runtime/live_multi_sleeve.json`

The EOD snapshot is still stored per sleeve. The shared config keeps it aligned
with the live multi-sleeve runner, while each target uses its own sleeve id,
market label, account store, and order-runtime route.

Artifacts are written under:

```text
data/eod-snapshots/<yyyy-MM-dd>/<label>/<target>/
```

Each run stores:

- portfolio report runtime JSON, candidate order JSON, and operator message text
- `order_runtime_status.json`
- `runtime_health.json`
- `runtime_preflight.json`
- copies of the current virtual-account and order-runtime stores
- a top-level manifest with command exit codes and retention cleanup details

## Sleeve Daily Performance

EOD snapshots can be read back as a sleeve-scoped equity curve:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-daily-performance `
  --snapshot-root data/eod-snapshots `
  --sleeve-id LEaps `
  --include-holdings
```

The command groups snapshots by `sleeve_id + currency + date`, keeps the latest
snapshot per date, and reports:

- equity, cash, gross exposure, exposure percentage
- held symbols, and optionally full holding rows
- previous equity
- net cash flow from the virtual-account `cash_transfers` ledger
- cash-flow-adjusted daily PnL and daily return

This mirrors LEAN's result/statistics layer, but namespaced by sleeve. Do not
use raw equity changes as strategy return when cash was moved into or out of a
sleeve; the command subtracts same-period cash transfers before calculating the
daily return.

Useful variants:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-daily-performance --sleeve-id us_etf_rotation --currency USD
py -3 -m leaps_quant_engine.cli sleeve-daily-performance --from-date 2026-05-12 --to-date 2026-05-14
py -3 -m leaps_quant_engine.cli sleeve-daily-performance --summary-only
```

## Scheduler

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\leaps_eod_snapshot_scheduler.ps1
```

Default schedules use KST:

- `18:05` `krx-after-hours` for `LEaps`
- `18:05` `krx-after-hours` for `kr-lowvol-defensive`
- `06:10` `us-after-hours` for `us_etf_rotation`

The scheduler writes one marker per date and label under
`data/runtime/eod-snapshots/`, so a restarted scheduler will not duplicate the
same daily capture.

When multiple sleeves share the same date and label, such as domestic
`LEaps` and `kr-lowvol-defensive`, the scheduler groups those targets into one
snapshot run. This keeps one `krx-after-hours` marker while still writing a
separate per-sleeve `portfolio-report/*_runtime_*.json` artifact for each
target.

Check scheduler output status without submitting orders:

```powershell
py -3 -m leaps_quant_engine.cli eod-snapshot-status --summary-only
```

The status command reads marker files and manifests, then reports each label as
`scheduled`, `ok_today`, `failed_today`, or `missing_today`.

## Start Hidden

```powershell
Start-Process -FilePath powershell.exe `
  -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','tools\leaps_eod_snapshot_scheduler.ps1') `
  -WindowStyle Hidden -PassThru
```

## Retention

The one-shot tool deletes dated snapshot directories older than
`-RetentionDays` under `data/eod-snapshots`. The default is `31`.
