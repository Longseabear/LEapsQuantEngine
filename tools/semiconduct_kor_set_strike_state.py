from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from leaps_quant_engine.runtime_state import ModelStateKey, SQLiteRuntimeStateStore, StatePatch


DEFAULT_RUNTIME_STATE = Path("data/runtime/runtime-state/semiconduct_kor_shadow.sqlite")
DEFAULT_MODEL_ID = "semiconduct-kor-samsung-strike-monitor"
DEFAULT_NAMESPACE = "strike_risk"
DEFAULT_SYMBOL_KEY = "KRX:005930"
VALID_STATUSES = {"on", "easing", "off_candidate", "off_confirmed"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Set semiconduct-kor Samsung strike-risk state.")
    parser.add_argument("--runtime-state", type=Path, default=DEFAULT_RUNTIME_STATE)
    parser.add_argument("--status", choices=sorted(VALID_STATUSES), required=True)
    parser.add_argument("--confidence", type=float, default=0.0)
    parser.add_argument("--source-count", type=int, default=0)
    parser.add_argument("--reason", default="")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--symbol-key", default=DEFAULT_SYMBOL_KEY)
    args = parser.parse_args()

    as_of = _parse_datetime(args.as_of) if args.as_of else datetime.now().astimezone()
    value = {
        "strike_risk_status": args.status,
        "confidence": max(0.0, min(float(args.confidence), 1.0)),
        "source_count": max(int(args.source_count), 0),
        "as_of": as_of.isoformat(),
        "reason": args.reason,
    }
    key = ModelStateKey(
        sleeve_id="semiconduct-kor",
        model_id=args.model_id,
        namespace=args.namespace,
        symbol_key=args.symbol_key,
    )
    store = SQLiteRuntimeStateStore(args.runtime_state)
    events = store.apply_patches((StatePatch(key=key, value=value, reason="operator_strike_risk_state_set"),), applied_at=as_of)
    print(
        json.dumps(
            {
                "status": "ok",
                "runtime_state": str(args.runtime_state),
                "key": key.to_dict(),
                "value": value,
                "event_count": len(events),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


if __name__ == "__main__":
    raise SystemExit(main())
