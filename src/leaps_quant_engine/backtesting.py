from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import csv
import math

from leaps_quant_engine.engine import Engine
from leaps_quant_engine.market_data import MarketDataError, MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, OrderIntent, OrderSide, Symbol


@dataclass(slots=True)
class VirtualMarketDataProvider(MarketDataProvider):
    """In-memory market data provider for deterministic backtests."""

    history: dict[str, list[Bar]] = field(default_factory=dict)

    @classmethod
    def from_bars(cls, bars: list[Bar]) -> "VirtualMarketDataProvider":
        provider = cls()
        for bar in bars:
            provider.add_bar(bar)
        return provider

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        *,
        symbol: Symbol,
        time_column: str = "time",
    ) -> "VirtualMarketDataProvider":
        bars: list[Bar] = []
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                bars.append(
                    Bar(
                        symbol=symbol,
                        time=_parse_datetime(row[time_column]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row.get("volume") or 0)),
                    )
                )
        return cls.from_bars(bars)

    def add_bar(self, bar: Bar) -> None:
        bars = self.history.setdefault(bar.symbol.key, [])
        bars.append(bar)
        bars.sort(key=lambda item: item.time)

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        bars = self.history.get(symbol.key) or []
        if not bars:
            raise MarketDataError(f"No virtual bars for {symbol.key}")
        return bars[-1]

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        bars = self.history.get(symbol.key) or []
        return [
            bar
            for bar in bars
            if (start is None or bar.time >= start) and (end is None or bar.time <= end)
        ]


@dataclass(frozen=True, slots=True)
class BacktestSnapshot:
    time: datetime
    equity: float
    cash: float
    gross_exposure: float

    @property
    def exposure(self) -> float:
        return self.gross_exposure / self.equity if self.equity > 0 else 0.0


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    sleeve_id: str
    symbol: Symbol
    entry_time: datetime
    exit_time: datetime
    quantity: int
    average_entry_price: float
    exit_price: float
    pnl: float
    holding_days: float


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    initial_equity: float
    final_equity: float
    total_return: float
    cagr: float
    sharpe: float
    mdd: float
    turnover: float
    avg_holding_days: float
    avg_exposure: float
    win_rate: float
    trade_count: int
    order_count: int

    def to_report(self) -> dict[str, float | int]:
        return {
            "initial_equity": self.initial_equity,
            "final_equity": self.final_equity,
            "total_return": self.total_return,
            "cagr": self.cagr,
            "sharpe": self.sharpe,
            "mdd": self.mdd,
            "turnover": self.turnover,
            "avg_holding_days": self.avg_holding_days,
            "avg_exposure": self.avg_exposure,
            "win_rate": self.win_rate,
            "trade_count": self.trade_count,
            "order_count": self.order_count,
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    orders: list[OrderIntent]
    final_cash_by_sleeve: dict[str, float]
    final_quantity_by_sleeve: dict[str, dict[str, int]]
    metrics: BacktestMetrics
    metrics_by_sleeve: dict[str, BacktestMetrics]
    snapshots_by_sleeve: dict[str, list[BacktestSnapshot]]
    trades_by_sleeve: dict[str, list[ClosedTrade]]

    def to_report(self) -> dict[str, object]:
        return {
            "metrics": self.metrics.to_report(),
            "metrics_by_sleeve": {
                sleeve_id: metrics.to_report()
                for sleeve_id, metrics in self.metrics_by_sleeve.items()
            },
            "final_cash_by_sleeve": self.final_cash_by_sleeve,
            "final_quantity_by_sleeve": self.final_quantity_by_sleeve,
        }


def build_replay_feed(
    provider: MarketDataProvider,
    symbols: list[Symbol],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[DataSlice]:
    bars_by_symbol = {symbol.key: provider.get_history(symbol, start=start, end=end) for symbol in symbols}
    times = sorted({bar.time for bars in bars_by_symbol.values() for bar in bars})
    feed: list[DataSlice] = []
    for time in times:
        bars = {
            symbol_key: bar
            for symbol_key, series in bars_by_symbol.items()
            for bar in series
            if bar.time == time
        }
        if bars:
            feed.append(DataSlice(time=time, bars=bars))
    return feed


def run_backtest(
    engine: Engine,
    provider: MarketDataProvider,
    symbols: list[Symbol],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> BacktestResult:
    feed = build_replay_feed(provider, symbols, start=start, end=end)
    engine.initialize()
    result_orders: list[OrderIntent] = []
    trackers = {
        sleeve.id: _SleeveBacktestTracker(sleeve_id=sleeve.id, initial_cash=sleeve.portfolio.cash)
        for sleeve in engine.sleeves
    }
    last_prices: dict[str, float] = {}
    for data in feed:
        for bar in data.bars.values():
            last_prices[bar.symbol.key] = bar.close
        for sleeve in engine.sleeves:
            targets = sleeve.on_data(data)
            orders = engine.execution_model.create_orders(sleeve.id, sleeve.portfolio, data, targets)
            result_orders.extend(orders)
            tracker = trackers[sleeve.id]
            for order in orders:
                tracker.record_fill(order, data.time)
                sleeve.portfolio.apply_fill(order)
            tracker.record_snapshot(data.time, sleeve.portfolio.cash, sleeve.portfolio.holdings, last_prices)

    snapshots_by_sleeve = {
        sleeve_id: tracker.snapshots
        for sleeve_id, tracker in trackers.items()
    }
    trades_by_sleeve = {
        sleeve_id: tracker.closed_trades
        for sleeve_id, tracker in trackers.items()
    }
    metrics_by_sleeve = {
        sleeve_id: tracker.metrics()
        for sleeve_id, tracker in trackers.items()
    }
    return BacktestResult(
        orders=result_orders,
        final_cash_by_sleeve={sleeve.id: sleeve.portfolio.cash for sleeve in engine.sleeves},
        final_quantity_by_sleeve={
            sleeve.id: {
                key: holding.quantity
                for key, holding in sleeve.portfolio.holdings.items()
            }
            for sleeve in engine.sleeves
        },
        metrics=_aggregate_metrics(trackers),
        metrics_by_sleeve=metrics_by_sleeve,
        snapshots_by_sleeve=snapshots_by_sleeve,
        trades_by_sleeve=trades_by_sleeve,
    )


@dataclass(slots=True)
class _OpenLot:
    quantity: int
    price: float
    time: datetime


@dataclass(slots=True)
class _SleeveBacktestTracker:
    sleeve_id: str
    initial_cash: float
    traded_notional: float = 0.0
    order_count: int = 0
    lots_by_symbol: dict[str, list[_OpenLot]] = field(default_factory=dict)
    snapshots: list[BacktestSnapshot] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    def record_fill(self, order: OrderIntent, time: datetime) -> None:
        self.order_count += 1
        self.traded_notional += order.notional
        if order.side is OrderSide.BUY:
            self.lots_by_symbol.setdefault(order.symbol.key, []).append(
                _OpenLot(quantity=order.quantity, price=order.reference_price, time=time)
            )
            return
        self._close_lots(order, time)

    def record_snapshot(
        self,
        time: datetime,
        cash: float,
        holdings: dict[str, object],
        last_prices: dict[str, float],
    ) -> None:
        gross_exposure = 0.0
        for symbol_key, holding in holdings.items():
            price = last_prices.get(symbol_key)
            if price is None:
                continue
            gross_exposure += abs(getattr(holding, "quantity")) * price
        self.snapshots.append(
            BacktestSnapshot(
                time=time,
                equity=cash + gross_exposure,
                cash=cash,
                gross_exposure=gross_exposure,
            )
        )

    def metrics(self) -> BacktestMetrics:
        return _calculate_metrics(
            initial_equity=self.initial_cash,
            snapshots=self.snapshots,
            closed_trades=self.closed_trades,
            traded_notional=self.traded_notional,
            order_count=self.order_count,
        )

    def _close_lots(self, order: OrderIntent, time: datetime) -> None:
        remaining = order.quantity
        lots = self.lots_by_symbol.get(order.symbol.key, [])
        total_cost = 0.0
        total_holding_days = 0.0
        closed_quantity = 0
        entry_time: datetime | None = None
        while remaining > 0 and lots:
            lot = lots[0]
            matched_quantity = min(remaining, lot.quantity)
            total_cost += matched_quantity * lot.price
            total_holding_days += matched_quantity * max(0.0, (time - lot.time).total_seconds() / 86400.0)
            closed_quantity += matched_quantity
            entry_time = lot.time if entry_time is None else min(entry_time, lot.time)
            remaining -= matched_quantity
            lot.quantity -= matched_quantity
            if lot.quantity == 0:
                lots.pop(0)
        if not lots:
            self.lots_by_symbol.pop(order.symbol.key, None)
        if closed_quantity == 0:
            return
        average_entry_price = total_cost / closed_quantity
        average_holding_days = total_holding_days / closed_quantity
        self.closed_trades.append(
            ClosedTrade(
                sleeve_id=self.sleeve_id,
                symbol=order.symbol,
                entry_time=entry_time or time,
                exit_time=time,
                quantity=closed_quantity,
                average_entry_price=average_entry_price,
                exit_price=order.reference_price,
                pnl=(order.reference_price * closed_quantity) - total_cost,
                holding_days=average_holding_days,
            )
        )


def _calculate_metrics(
    *,
    initial_equity: float,
    snapshots: list[BacktestSnapshot],
    closed_trades: list[ClosedTrade],
    traded_notional: float,
    order_count: int,
) -> BacktestMetrics:
    final_equity = snapshots[-1].equity if snapshots else initial_equity
    total_return = (final_equity / initial_equity) - 1.0 if initial_equity > 0 else 0.0
    return BacktestMetrics(
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return=total_return,
        cagr=_cagr(initial_equity, final_equity, snapshots),
        sharpe=_sharpe(snapshots),
        mdd=_max_drawdown(snapshots),
        turnover=_turnover(traded_notional, snapshots, initial_equity),
        avg_holding_days=_avg_holding_days(closed_trades),
        avg_exposure=_avg_exposure(snapshots),
        win_rate=_win_rate(closed_trades),
        trade_count=len(closed_trades),
        order_count=order_count,
    )


def _aggregate_metrics(trackers: dict[str, _SleeveBacktestTracker]) -> BacktestMetrics:
    initial_equity = sum(tracker.initial_cash for tracker in trackers.values())
    traded_notional = sum(tracker.traded_notional for tracker in trackers.values())
    order_count = sum(tracker.order_count for tracker in trackers.values())
    closed_trades = [
        trade
        for tracker in trackers.values()
        for trade in tracker.closed_trades
    ]
    snapshots_by_time: dict[datetime, list[BacktestSnapshot]] = {}
    for tracker in trackers.values():
        for snapshot in tracker.snapshots:
            snapshots_by_time.setdefault(snapshot.time, []).append(snapshot)
    snapshots = [
        BacktestSnapshot(
            time=time,
            equity=sum(snapshot.equity for snapshot in snapshots),
            cash=sum(snapshot.cash for snapshot in snapshots),
            gross_exposure=sum(snapshot.gross_exposure for snapshot in snapshots),
        )
        for time, snapshots in sorted(snapshots_by_time.items())
    ]
    return _calculate_metrics(
        initial_equity=initial_equity,
        snapshots=snapshots,
        closed_trades=closed_trades,
        traded_notional=traded_notional,
        order_count=order_count,
    )


def _cagr(initial_equity: float, final_equity: float, snapshots: list[BacktestSnapshot]) -> float:
    if initial_equity <= 0 or final_equity <= 0 or len(snapshots) < 2:
        return 0.0
    days = (snapshots[-1].time - snapshots[0].time).total_seconds() / 86400.0
    if days <= 0:
        return 0.0
    return (final_equity / initial_equity) ** (365.25 / days) - 1.0


def _sharpe(snapshots: list[BacktestSnapshot]) -> float:
    returns = [
        (current.equity / previous.equity) - 1.0
        for previous, current in zip(snapshots, snapshots[1:])
        if previous.equity > 0
    ]
    if len(returns) < 2:
        return 0.0
    average = sum(returns) / len(returns)
    variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
    standard_deviation = math.sqrt(variance)
    return 0.0 if standard_deviation == 0 else (average / standard_deviation) * math.sqrt(252.0)


def _max_drawdown(snapshots: list[BacktestSnapshot]) -> float:
    peak = 0.0
    max_drawdown = 0.0
    for snapshot in snapshots:
        peak = max(peak, snapshot.equity)
        if peak <= 0:
            continue
        drawdown = (peak - snapshot.equity) / peak
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _turnover(traded_notional: float, snapshots: list[BacktestSnapshot], initial_equity: float) -> float:
    denominator = _average([snapshot.equity for snapshot in snapshots]) if snapshots else initial_equity
    return traded_notional / denominator if denominator > 0 else 0.0


def _avg_holding_days(closed_trades: list[ClosedTrade]) -> float:
    return _average([trade.holding_days for trade in closed_trades])


def _avg_exposure(snapshots: list[BacktestSnapshot]) -> float:
    return _average([snapshot.exposure for snapshot in snapshots])


def _win_rate(closed_trades: list[ClosedTrade]) -> float:
    return sum(1 for trade in closed_trades if trade.pnl > 0) / len(closed_trades) if closed_trades else 0.0


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0

def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text)
