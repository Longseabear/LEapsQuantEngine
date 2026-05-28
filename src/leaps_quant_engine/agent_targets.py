from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from leaps_quant_engine.models import Symbol


@dataclass(frozen=True, slots=True)
class AgentTargetItem:
    symbol: Symbol
    target_percent: float
    name: str = ""
    reason: str = ""
    confidence: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


@dataclass(frozen=True, slots=True)
class AgentTargetArtifact:
    path: Path
    raw: Mapping[str, Any]
    sleeve_id: str = ""
    target_id: str = ""
    generated_at: datetime | None = None
    expires_at: datetime | None = None
    flatten: bool = False
    max_gross_exposure: float | None = None
    targets: tuple[AgentTargetItem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))

    @property
    def gross_exposure(self) -> float:
        return sum(abs(target.target_percent) for target in self.targets)


@dataclass(frozen=True, slots=True)
class AgentTargetLoadResult:
    status: str
    path: Path
    artifact: AgentTargetArtifact | None = None
    reason: str = ""

    @property
    def is_usable(self) -> bool:
        return self.artifact is not None and self.status == "loaded"


def load_agent_target_artifact(
    path: str | Path,
    *,
    as_of: datetime | None = None,
    sleeve_id: str | None = None,
    require_sleeve_id: bool = True,
    max_age_hours: float = 36.0,
    default_market: str = "KRX",
    allowed_markets: tuple[str, ...] | list[str] | set[str] | None = ("KRX",),
    allow_short: bool = False,
    flatten_flag: str = "flatten",
) -> AgentTargetLoadResult:
    resolved = resolve_agent_target_path(path, as_of=as_of)
    if not resolved.exists():
        return AgentTargetLoadResult(status="missing_target_artifact", path=resolved)
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return AgentTargetLoadResult(status="invalid_target_artifact", path=resolved, reason=str(exc))
    if not isinstance(payload, Mapping):
        return AgentTargetLoadResult(status="invalid_target_artifact", path=resolved, reason="root_not_object")

    artifact_sleeve_id = str(payload.get("sleeve_id") or "").strip()
    expected_sleeve_id = str(sleeve_id or "").strip()
    if require_sleeve_id and not artifact_sleeve_id:
        return AgentTargetLoadResult(status="missing_sleeve_id", path=resolved)
    if expected_sleeve_id and artifact_sleeve_id and artifact_sleeve_id != expected_sleeve_id:
        return AgentTargetLoadResult(status="sleeve_id_mismatch", path=resolved)

    generated_at = parse_datetime(payload.get("generated_at") or payload.get("as_of"))
    expires_at = parse_datetime(payload.get("expires_at"))
    now = as_of or datetime.now(timezone.utc)
    if expires_at is not None and _utc_naive(now) > _utc_naive(expires_at):
        return AgentTargetLoadResult(status="target_artifact_expired", path=resolved)
    if generated_at is not None and float(max_age_hours) > 0:
        age = _utc_naive(now) - _utc_naive(generated_at)
        if age > timedelta(hours=float(max_age_hours)):
            return AgentTargetLoadResult(status="target_artifact_stale", path=resolved)

    markets = None
    if allowed_markets is not None:
        markets = {str(market).strip().upper() for market in allowed_markets if str(market).strip()}
    targets = _parse_targets(
        payload,
        default_market=default_market,
        allowed_markets=markets,
        allow_short=allow_short,
    )
    artifact = AgentTargetArtifact(
        path=resolved,
        raw=payload,
        sleeve_id=artifact_sleeve_id,
        target_id=str(payload.get("target_id") or payload.get("id") or "").strip(),
        generated_at=generated_at,
        expires_at=expires_at,
        flatten=bool(payload.get(flatten_flag, False)),
        max_gross_exposure=_optional_float(payload.get("max_gross_exposure")),
        targets=tuple(targets),
    )
    return AgentTargetLoadResult(status="loaded", path=resolved, artifact=artifact)


def resolve_agent_target_path(path: str | Path, *, as_of: datetime | None = None) -> Path:
    """Resolve a live or point-in-time agent target path.

    Plain paths keep the live behavior unchanged. Backtests can pass a template
    such as ``data/research/leaps/targets/{date}.json`` so each replay date
    reads the artifact that would have existed at that point in time.
    """

    text = str(path)
    if as_of is not None:
        day = as_of.date()
        values = {
            "date": day.isoformat(),
            "yyyymmdd": day.strftime("%Y%m%d"),
            "yyyy": day.strftime("%Y"),
            "mm": day.strftime("%m"),
            "dd": day.strftime("%d"),
        }
        try:
            text = text.format(**values)
        except (KeyError, ValueError):
            text = str(path)
    resolved = Path(text)
    if resolved.is_dir() and as_of is not None:
        day = as_of.date()
        for name in (f"{day.isoformat()}.json", f"{day.strftime('%Y%m%d')}.json"):
            candidate = resolved / name
            if candidate.exists():
                return candidate
    return resolved


def parse_symbol(value: Any, default_market: str = "KRX") -> Symbol | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if ":" in text:
        market, ticker = text.split(":", 1)
        market = market.strip()
        ticker = ticker.strip()
    else:
        market = str(default_market or "KRX").strip().upper()
        ticker = text
    if not market or not ticker:
        return None
    return Symbol(ticker=ticker, market=market)


def target_percent(item: Mapping[str, Any]) -> float | None:
    for name in ("target_percent", "weight", "target_weight"):
        if name not in item:
            continue
        value = item.get(name)
        try:
            percent = float(value)
        except (TypeError, ValueError):
            return None
        if abs(percent) > 1.0 and abs(percent) <= 100.0:
            percent /= 100.0
        if abs(percent) > 1.0:
            return None
        return percent
    return None


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(f"{text}T00:00:00")
        except ValueError:
            return None


def compact_tag(value: str) -> str:
    return "_".join(str(value).strip().lower().split())[:80]


def clamp_pct(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _parse_targets(
    payload: Mapping[str, Any],
    *,
    default_market: str,
    allowed_markets: set[str] | None,
    allow_short: bool,
) -> list[AgentTargetItem]:
    result: list[AgentTargetItem] = []
    seen: set[str] = set()
    for item in payload.get("targets") or ():
        if not isinstance(item, Mapping):
            continue
        symbol = parse_symbol(item.get("symbol") or item.get("symbol_key") or item.get("ticker"), default_market)
        if symbol is None or symbol.key in seen:
            continue
        if allowed_markets is not None and symbol.market.upper() not in allowed_markets:
            continue
        percent = target_percent(item)
        if percent is None:
            continue
        if percent < 0 and not allow_short:
            continue
        seen.add(symbol.key)
        result.append(
            AgentTargetItem(
                symbol=symbol,
                target_percent=percent,
                name=str(item.get("name") or "").strip(),
                reason=str(item.get("reason") or item.get("thesis") or "").strip(),
                confidence=_optional_float(item.get("confidence")),
                raw=item,
            )
        )
    return result


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
