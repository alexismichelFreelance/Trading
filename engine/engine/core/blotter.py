"""Trade blotter + P&L ledger + structured logging.

Consumes the engine's event stream. The ledger turns Fills into closed
round-trip trades using average-cost accounting (handles scale-in / scale-out /
flip), so a position from first-entry to flat counts as ONE trade — matching how
the research counted trades. `gross_points` is in contract-points (per-contract
point move x contracts), so a 1-contract strategy's gross_points is just the
point move; `* point_usd` gives dollars for sized sleeves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .events import AccountUpdate, BrokerEvent, Fill, PositionUpdate
from .timeutil import et

log = logging.getLogger("engine.blotter")


@dataclass
class TradeRecord:
    symbol: str
    direction: int            # +1 long, -1 short (of the opening fill)
    entry_ts: int
    exit_ts: int | None = None
    entry_px: float = 0.0
    exit_px: float = 0.0
    contracts: int = 0        # peak absolute exposure
    gross_points: float = 0.0  # realized contract-points (pre-commission)
    commission_usd: float = 0.0
    tags: list[str] = field(default_factory=list)

    @property
    def month(self) -> str:
        return et(self.entry_ts).strftime("%Y-%m")


class TradeLedger:
    """Average-cost position tracker producing closed TradeRecords."""

    def __init__(self, symbol: str, point_usd: float) -> None:
        self.symbol = symbol
        self.point_usd = point_usd
        self.qty = 0
        self.avg_px = 0.0
        self.trades: list[TradeRecord] = []
        self._open: TradeRecord | None = None

    def on_fill(self, f: Fill) -> None:
        q = f.size                      # signed
        if q == 0:
            return
        if self.qty == 0:
            self._begin(f)
        elif (q > 0) == (self.qty > 0):
            # adding in the same direction -> update average
            new_qty = self.qty + q
            self.avg_px = (self.avg_px * self.qty + f.price * q) / new_qty
            self.qty = new_qty
            self._open.contracts = max(self._open.contracts, abs(self.qty))
            self._open.commission_usd += f.commission
            self._tag(f)
        else:
            # reducing / closing / flipping
            closed = min(abs(q), abs(self.qty))
            direction = 1 if self.qty > 0 else -1
            self._open.gross_points += (f.price - self.avg_px) * direction * closed
            self._open.commission_usd += f.commission
            self._tag(f)
            self.qty += q
            if self.qty == 0:
                self._finalize(f)
            elif (self.qty > 0) != (direction > 0):
                # flipped through zero: close, then open the remainder
                remainder = self.qty
                self._finalize(f, exit_qty_consumed=True)
                self.qty = 0
                self._begin(Fill(f.ts, f.order_id, f.symbol, f.price, remainder,
                                 0.0, f.slippage, f.tag))

    # ── internals ────────────────────────────────────────────────────────
    def _begin(self, f: Fill) -> None:
        self.qty = f.size
        self.avg_px = f.price
        self._open = TradeRecord(
            symbol=f.symbol, direction=1 if f.size > 0 else -1, entry_ts=f.ts,
            entry_px=f.price, contracts=abs(f.size), commission_usd=f.commission,
        )
        self._tag(f)

    def _finalize(self, f: Fill, exit_qty_consumed: bool = False) -> None:
        assert self._open is not None
        self._open.exit_ts = f.ts
        self._open.exit_px = f.price
        self.trades.append(self._open)
        self._open = None
        self.avg_px = 0.0

    def _tag(self, f: Fill) -> None:
        if self._open is not None and f.tag and f.tag not in self._open.tags:
            self._open.tags.append(f.tag)


class Blotter:
    """Logging + the ledger + parity-friendly summaries."""

    def __init__(self, symbol: str, point_usd: float, verbose: bool = False) -> None:
        self.ledger = TradeLedger(symbol, point_usd)
        self.point_usd = point_usd
        self.verbose = verbose
        self.n_market = 0
        self.n_orders = 0
        self.n_fills = 0

    # event hooks ----------------------------------------------------------
    def on_market_event(self, e) -> None:
        self.n_market += 1

    def on_order(self, order) -> None:
        self.n_orders += 1
        if self.verbose:
            log.info("ORDER %s", order)

    def on_broker_event(self, be: BrokerEvent) -> None:
        if isinstance(be, Fill):
            self.n_fills += 1
            self.ledger.on_fill(be)
            if self.verbose:
                log.info("FILL %s", be)
        elif isinstance(be, (PositionUpdate, AccountUpdate)) and self.verbose:
            log.info("%s", be)

    # summaries ------------------------------------------------------------
    @property
    def trades(self) -> list[TradeRecord]:
        return self.ledger.trades

    def net_points(self, flat_cost_pts: float = 0.0) -> float:
        """Total net contract-points. `flat_cost_pts` subtracts a flat round-turn
        cost per trade (used for the 0.517-pt ignition/flow parity)."""
        return sum(t.gross_points for t in self.trades) - flat_cost_pts * len(self.trades)

    def net_usd(self, flat_cost_pts: float = 0.0) -> float:
        gross = sum(t.gross_points for t in self.trades) * self.point_usd
        comm = sum(t.commission_usd for t in self.trades)
        return gross - comm - flat_cost_pts * self.point_usd * len(self.trades)

    def by_month(self, flat_cost_pts: float = 0.0) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for t in self.trades:
            m = out.setdefault(t.month, {"n": 0, "pts": 0.0, "usd": 0.0, "wins": 0})
            net_pts = t.gross_points - flat_cost_pts
            net_usd = t.gross_points * self.point_usd - t.commission_usd - flat_cost_pts * self.point_usd
            m["n"] += 1
            m["pts"] += net_pts
            m["usd"] += net_usd
            m["wins"] += 1 if net_pts > 0 else 0
        return dict(sorted(out.items()))

    def summary(self, flat_cost_pts: float = 0.0) -> str:
        rows = self.by_month(flat_cost_pts)
        lines = [f"{'month':<9} {'n':>4} {'win%':>5} {'net_pts':>9} {'net_usd':>11}"]
        for m, r in rows.items():
            win = 100.0 * r["wins"] / r["n"] if r["n"] else 0.0
            lines.append(f"{m:<9} {r['n']:>4} {win:>5.0f} {r['pts']:>9.1f} {r['usd']:>11,.0f}")
        lines.append(f"{'TOTAL':<9} {len(self.trades):>4} {'':>5} "
                     f"{self.net_points(flat_cost_pts):>9.1f} {self.net_usd(flat_cost_pts):>11,.0f}")
        return "\n".join(lines)


__all__ = ["TradeRecord", "TradeLedger", "Blotter"]
