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

- `LEaps` from `configs/runtime/leaps_workspace_smoke.json`
- `us_etf_rotation` from `configs/runtime/us_etf_rotation_sleeve.json`

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

## Scheduler

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\leaps_eod_snapshot_scheduler.ps1
```

Default schedules use KST:

- `18:05` `krx-after-hours` for `LEaps`
- `06:10` `us-after-hours` for `us_etf_rotation`

The scheduler writes one marker per date and label under
`data/runtime/eod-snapshots/`, so a restarted scheduler will not duplicate the
same daily capture.

## Start Hidden

```powershell
Start-Process -FilePath powershell.exe `
  -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','tools\leaps_eod_snapshot_scheduler.ps1') `
  -WindowStyle Hidden -PassThru
```

## Retention

The one-shot tool deletes dated snapshot directories older than
`-RetentionDays` under `data/eod-snapshots`. The default is `31`.
