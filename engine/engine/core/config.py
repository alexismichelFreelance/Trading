"""Configuration: per-instrument specs and a run config. Multi-instrument-ready
(ES now; NQ/GC later) — params come from config/instruments.yaml, not code."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .costs import CostModel, DEFAULT


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str          # root symbol, e.g. 'ES'
    point_usd: float     # $ per 1.0 point
    tick: float          # minimum price increment
    rth_start_min: int = 570   # ET minute-of-day, 09:30
    rth_end_min: int = 960     # ET minute-of-day, 16:00

    @property
    def tick_usd(self) -> float:
        return self.tick * self.point_usd


def root_symbol(contract: str) -> str:
    """'ESM5' / 'ESH5' -> 'ES'. Futures roots here are 2 letters."""
    return contract[:2].upper()


def load_instruments(path: str | Path) -> dict[str, InstrumentSpec]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    out: dict[str, InstrumentSpec] = {}
    for sym, spec in (data.get("instruments") or {}).items():
        out[sym] = InstrumentSpec(symbol=sym, **spec)
    return out


@dataclass(frozen=True)
class RunConfig:
    """One replay/live session."""
    symbol: str                       # contract, e.g. 'ESM5'
    instrument: InstrumentSpec
    account: float = 100_000.0
    risk_per_trade: float = 2_000.0   # 2% of account
    max_contracts: int = 30
    cost: CostModel = DEFAULT
    start: str | None = None          # ISO date (inclusive)
    end: str | None = None            # ISO date (exclusive)
    speed: float = 0.0                # 0 = as-fast-as-possible; >0 = real-time * speed
    extra: dict = field(default_factory=dict)


__all__ = ["InstrumentSpec", "RunConfig", "root_symbol", "load_instruments"]
