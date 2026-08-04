"""
es_costs.py — realistic ES execution / cost model.

Every backtest result in this project is reported NET of these costs. Gross PnL
on ES is meaningless: 1 tick = 0.25 pt = $12.50, and the bid/ask spread plus
queue/slippage is usually the difference between a "profitable" backtest and a
losing live system.

Cost components (all converted to ES points so they subtract directly from a
trade's point PnL):
  - commission   : broker round-turn, in $ (typical retail futures ~$2-4 RT).
  - spread cross : if you take liquidity, you pay ~half the spread on entry and
                   half on exit. ES is ~1 tick wide in RTH, wider/thin overnight.
  - slippage     : extra adverse fill beyond the quoted spread (size, latency,
                   fast markets).

Presets express the execution *style*, not magic numbers — they map directly to
how the order is worked:
  passive_both   : limit in, limit out (you EARN the spread but risk non-fills).
  passive_entry  : limit in, market out (pay half-spread once).      <- default
  aggressive     : market in, market out (pay ~full spread round-turn).
"""
from __future__ import annotations
from dataclasses import dataclass

TICK = 0.25
POINT_USD = 50.0
TICK_USD = TICK * POINT_USD          # $12.50


@dataclass(frozen=True)
class CostModel:
    commission_usd_rt: float = 4.0   # round-turn commission in dollars
    spread_ticks: float = 1.0        # quoted spread width in ticks (RTH ES ~1)
    entry_cross: float = 0.5         # fraction of spread paid on entry (0=passive,1=full)
    exit_cross: float = 0.5          # fraction of spread paid on exit
    slippage_ticks_rt: float = 0.25  # extra adverse ticks, round-turn

    def round_turn_points(self) -> float:
        """Total round-turn cost in ES points."""
        spread_pt = self.spread_ticks * TICK
        cross_pt = (self.entry_cross + self.exit_cross) * spread_pt
        slip_pt = self.slippage_ticks_rt * TICK
        comm_pt = self.commission_usd_rt / POINT_USD
        return cross_pt + slip_pt + comm_pt

    def round_turn_usd(self) -> float:
        return self.round_turn_points() * POINT_USD


# Presets ---------------------------------------------------------------------
PASSIVE_BOTH = CostModel(entry_cross=0.0, exit_cross=0.0, slippage_ticks_rt=0.0)
PASSIVE_ENTRY = CostModel(entry_cross=0.0, exit_cross=1.0, slippage_ticks_rt=0.25)
AGGRESSIVE = CostModel(entry_cross=1.0, exit_cross=1.0, slippage_ticks_rt=0.5)
# Default used across the project: realistic for a semi-aggressive intraday system.
DEFAULT = CostModel(entry_cross=0.5, exit_cross=1.0, slippage_ticks_rt=0.25)


def net_points(gross_points, cost: CostModel = DEFAULT):
    """Subtract round-turn cost from an array/scalar of gross per-trade points."""
    import numpy as np
    return np.asarray(gross_points, dtype=float) - cost.round_turn_points()


if __name__ == "__main__":
    for name, c in [("PASSIVE_BOTH", PASSIVE_BOTH), ("PASSIVE_ENTRY", PASSIVE_ENTRY),
                    ("DEFAULT", DEFAULT), ("AGGRESSIVE", AGGRESSIVE)]:
        print(f"{name:14s} round-turn = {c.round_turn_points():.3f} pt "
              f"(${c.round_turn_usd():.2f})")
