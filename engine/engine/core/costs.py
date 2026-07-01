"""Execution / cost model — ports strategy_lab/es_costs.py.

Every P&L number is reported NET of these costs. The flat `CostModel` (DEFAULT
round-turn = 0.517 pt) is used for the 1-contract points strategies (ignition,
flow). The sized zone sleeve uses NinjaTrader-Free commission ($5.28 RT) plus
REGIME-COHERENT slippage (slippage scales with prevailing realized range, NOT
with the ignition spike itself — see sim/SIM_FINDINGS.md for why that distinction
matters). Limit fills (zone scale-outs, flow adds) pay NO slippage.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    commission_usd_rt: float = 4.0   # round-turn commission, $
    spread_ticks: float = 1.0        # quoted spread width, ticks (RTH ES ~1)
    entry_cross: float = 0.5         # fraction of spread paid on entry
    exit_cross: float = 1.0          # fraction of spread paid on exit
    slippage_ticks_rt: float = 0.25  # extra adverse ticks, round-turn
    tick: float = 0.25
    point_usd: float = 50.0

    def round_turn_points(self) -> float:
        spread_pt = self.spread_ticks * self.tick
        cross_pt = (self.entry_cross + self.exit_cross) * spread_pt
        slip_pt = self.slippage_ticks_rt * self.tick
        comm_pt = self.commission_usd_rt / self.point_usd
        return cross_pt + slip_pt + comm_pt

    def round_turn_usd(self) -> float:
        return self.round_turn_points() * self.point_usd


# Presets (express execution STYLE, not magic numbers) -----------------------
PASSIVE_BOTH = CostModel(entry_cross=0.0, exit_cross=0.0, slippage_ticks_rt=0.0)
PASSIVE_ENTRY = CostModel(entry_cross=0.0, exit_cross=1.0, slippage_ticks_rt=0.25)
AGGRESSIVE = CostModel(entry_cross=1.0, exit_cross=1.0, slippage_ticks_rt=0.5)
DEFAULT = CostModel()                       # 0.5175 pt — the project default
NT_FREE = CostModel(commission_usd_rt=5.28)  # NinjaTrader-Free RT (sized sleeves)


def regime_slippage_points(vol_proxy_pts: float, mult: float,
                           cap_pts: float = 1.0, tick: float = 0.25) -> float:
    """Slippage for a market/stop fill: clamp(tick, cap, mult * vol_proxy).

    `vol_proxy_pts` is the prevailing realized range (the regime), e.g. prior
    5-min range for ignition fills or avg 30-min range for zone fills. Limit
    fills should pass slippage = 0 (do not call this for them).
    """
    return min(cap_pts, max(tick, mult * vol_proxy_pts))


__all__ = [
    "CostModel", "PASSIVE_BOTH", "PASSIVE_ENTRY", "AGGRESSIVE", "DEFAULT",
    "NT_FREE", "regime_slippage_points",
]
