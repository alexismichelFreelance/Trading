"""Configuration: per-instrument specs and a run config. Multi-instrument-ready
(ES now; NQ/GC later) — params come from config/instruments.yaml, not code."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Iterable

import yaml


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str          # root symbol, e.g. 'ES'
    point_usd: float     # $ per 1.0 point (contract multiplier)
    tick: float          # minimum price increment
    rth_start_min: int = 570   # ET minute-of-day, 09:30
    rth_end_min: int = 960     # ET minute-of-day, 16:00
    asset_class: str = "future"        # 'future' now; 'option' later
    hmm_path: str | None = None        # per-instrument frozen regime model
    round_step: float | None = None    # round-number grid (ES: 50.0)
    extra: dict = field(default_factory=dict)  # anything else from yaml
    # (options later: strike/expiry/right/underlying live in `extra` until
    #  a dedicated OptionSpec is warranted — symbol stays a free-form string)

    @property
    def tick_usd(self) -> float:
        return self.tick * self.point_usd


# CME-style month codes for contract suffixes like ESM5 / NQH26.
_CONTRACT_RE = re.compile(r"^([A-Z]+?)[FGHJKMNQUVXZ]\d{1,2}$")


def root_symbol(contract: str, known: Iterable[str] | None = None) -> str:
    """'ESM5' -> 'ES', 'MESM5' -> 'MES' (given known roots or the month-code
    pattern). Longest known-root prefix wins; falls back to the month-code
    regex, then to the legacy 2-letter slice."""
    c = contract.upper()
    if known:
        hit = max((r for r in known if c.startswith(r.upper())), key=len, default=None)
        if hit:
            return hit
    m = _CONTRACT_RE.match(c)
    if m:
        return m.group(1)
    return c[:2]


def load_instruments(path: str | Path) -> dict[str, InstrumentSpec]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    known = {f.name for f in fields(InstrumentSpec)} - {"symbol", "extra"}
    out: dict[str, InstrumentSpec] = {}
    for sym, spec in (data.get("instruments") or {}).items():
        spec = dict(spec or {})
        extra = {k: spec.pop(k) for k in list(spec) if k not in known}
        out[sym] = InstrumentSpec(symbol=sym, extra=extra, **spec)
    return out


__all__ = ["InstrumentSpec", "root_symbol", "load_instruments"]
