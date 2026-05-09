# Snapshot Engine

Snapshots are immutable views of indicator and market-data quality at a point in time.

```text
market data collection
  -> IndicatorEngine update
  -> IndicatorSnapshot
  -> SnapshotContext / risk quality gates
```

## Main Files

- `indicator.py`: `IndicatorSnapshot`, `IndicatorValue`, and `IndicatorSnapshotStore`.
- `freshness.py`: `SnapshotFreshnessPolicy`, `SnapshotQualityReport`, and quality statuses.

## Quality Status

- `fresh`: suitable for new entries and risk checks.
- `degraded`: usable for cautious workflows, but not ideal for new entries.
- `stale`: old snapshot; exits/risk maintenance may still inspect it.
- `invalid`: should not be used for decisions.

## Usage

Alpha sees snapshot quality through `SnapshotContext`.

Risk receives `IndicatorSnapshot.quality_report` through `RiskManagementContext` and may block entries when data is not fresh.

Snapshots should be treated as immutable evidence for a framework cycle.

