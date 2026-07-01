"""Risk-based position sizing (shared by the sized sleeves).

contracts = floor(risk$ / (stop_pts * point_usd)), capped. Default risk$ is 2%
of a $100k account ($2,000); cap 30. NOT margin-based, NOT 1-contract.
"""
from __future__ import annotations

import math


def position_size(risk_usd: float, stop_points: float, point_usd: float,
                  max_contracts: int = 30) -> int:
    if stop_points <= 0 or risk_usd <= 0:
        return 0
    n = math.floor(risk_usd / (stop_points * point_usd))
    return max(0, min(n, max_contracts))


__all__ = ["position_size"]
