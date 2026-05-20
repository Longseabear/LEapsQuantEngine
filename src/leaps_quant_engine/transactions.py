from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class TransactionCostSummary:
    """Normalized transaction cost summary for live and simulated fills."""

    fee: float = 0.0
    commission: float = 0.0
    tax: float = 0.0
    regulatory_fee: float = 0.0
    slippage_cost: float = 0.0
    currency: str = ""
    source: str = ""

    @property
    def total_cost(self) -> float:
        return self.fee + self.commission + self.tax + self.regulatory_fee + self.slippage_cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "fee": self.fee,
            "commission": self.commission,
            "tax": self.tax,
            "regulatory_fee": self.regulatory_fee,
            "slippage_cost": self.slippage_cost,
            "total_cost": self.total_cost,
            "currency": self.currency,
            "source": self.source,
        }

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any] | None, *, fallback_fee: float = 0.0) -> "TransactionCostSummary":
        payload = dict((metadata or {}).get("transaction_costs") or {})
        if payload:
            return cls(
                fee=_float(payload.get("fee")),
                commission=_float(payload.get("commission")),
                tax=_float(payload.get("tax")),
                regulatory_fee=_float(payload.get("regulatory_fee")),
                slippage_cost=_float(payload.get("slippage_cost")),
                currency=str(payload.get("currency") or ""),
                source=str(payload.get("source") or ""),
            )
        return cls(fee=float(fallback_fee or 0.0))


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(str(value).replace(",", "").strip() or 0.0)
