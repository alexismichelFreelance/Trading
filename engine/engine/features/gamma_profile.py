"""The dealer-gamma curve, kept whole instead of reduced to three numbers.

tools/fetch_cboe_gex.py already built the full per-strike profile and already
found EVERY zero crossing -- and then wrote one flip, one call wall, one put
wall and a single sign for the book, discarding the rest. This is that same
computation with its output kept.

WHY IT MATTERS, measured. 2026-08-13 SPX: spot 7748.50, flip 7795.20,
net_sign +1, total_gex +2.34e10. Every *_gex sleeve reads net_sign +1 as
"dealers long gamma, pinning". But net_sign is the sign of the TOTAL across the
whole book, and spot sat 47 points BELOW the flip -- inside the pocket where
cumulative gamma is NEGATIVE, where hedging AMPLIFIES moves rather than damping
them. ES opened 7792.50, ran 45 points to 7838.25, and reversed to 7798.50. The
flip in future terms (basis +42) is 7837.20: the high missed it by 1.05 points.
The day did exactly what a short-gamma pocket into a sign change should do,
while the stored scalar described the opposite regime.

THE CONSTRUCTION is unchanged on purpose: net GEX per strike, summed across
strikes in ascending order, and a flip wherever that running total crosses zero.
It is an approximation -- a true profile re-prices every option's gamma at each
hypothetical spot -- but changing the method is a different question from
keeping what the method already produces. Swap it later, with evidence; do not
silently change two things at once.

WHAT IS NOT KNOWN, and no amount of curve detail fixes: the sign convention.
Calls are counted positive and puts negative, i.e. "dealers are long calls and
short puts". Nobody observes dealer inventory; it is a heuristic, and when it is
wrong the entire map inverts. Open interest is also published end-of-day, so
same-session 0DTE positioning is invisible here no matter how the curve is cut.
"""
from __future__ import annotations

N_WALLS = 5          # how many ranked concentrations to keep per side


def _cum_at(curve: list, spot: float) -> float:
    """Cumulative gamma at an arbitrary price, linearly between strikes.

    Below the lowest strike the book has not started; the nearest meaningful
    value is the first strike's own cumulative, so that is what is returned
    rather than a spurious zero that would read as "no regime"."""
    if spot <= curve[0][0]:
        return curve[0][2]
    if spot >= curve[-1][0]:
        return curve[-1][2]
    for i in range(1, len(curve)):
        k0, _, c0 = curve[i - 1]
        k1, _, c1 = curve[i]
        if k0 <= spot <= k1:
            if k1 == k0:
                return c1
            return c0 + (c1 - c0) * (spot - k0) / (k1 - k0)
    return curve[-1][2]


def gamma_profile(per_strike: dict, spot: float, n_walls: int = N_WALLS) -> dict | None:
    """{strike: (call_gex, put_gex)} -> the curve and everything read off it.

    `put_gex` is expected already NEGATIVE, as the fetcher signs it.

    Returns None for an empty book -- the caller decides whether that is a bad
    fetch or a quiet day; it is not this function's job to invent a level.

    Keys:
      curve        [(strike, net_gex, cumulative)] ascending
      flips        EVERY zero crossing, interpolated, ascending
      flip         the crossing nearest spot (what the old row carried)
      local_sign   sign of cumulative gamma AT SPOT -- the regime that applies
                   to price now, which is NOT total_sign
      total_sign   sign of the whole book (the old net_sign)
      pocket       (lower_edge, upper_edge) of the same-sign zone containing
                   spot; None on a side means the book ends without changing
                   sign again
      dist_to_flip signed distance to the next crossing above spot... or below,
                   whichever bounds the pocket in the direction price is moving
                   toward; None when the pocket is unbounded that way
      call_walls   [(strike, gex)] ranked by call gamma, biggest first
      put_walls    [(strike, gex)] ranked by |put gamma|, biggest first
      call_wall    the top one (unchanged meaning from the old row)
      put_wall     the top one
    """
    if not per_strike:
        return None
    rows = sorted((float(k), float(c), float(p)) for k, (c, p) in per_strike.items())
    curve, cum = [], 0.0
    for k, c, p in rows:
        net = c + p
        cum += net
        curve.append((k, net, cum))

    # every crossing, interpolated between the bracketing strikes
    flips: list[float] = []
    for i in range(1, len(curve)):
        k0, _, c0 = curve[i - 1]
        k1, _, c1 = curve[i]
        if (c0 <= 0 < c1 or c0 >= 0 > c1) and c1 != c0:
            flips.append(k0 + (0.0 - c0) / (c1 - c0) * (k1 - k0))

    total = curve[-1][2]
    total_sign = 1 if total > 0 else (-1 if total < 0 else 0)

    # cumulative AT SPOT, INTERPOLATED. Reading the last strike at or below spot
    # is wrong and quietly so: the curve is piecewise linear between strikes, and
    # a crossing lands BETWEEN them. A spot past the crossing but short of the
    # next strike then reports the sign of the pocket it has already left --
    # which is the whole error this module exists to remove, reintroduced one
    # level down.
    at = _cum_at(curve, spot)
    local_sign = 1 if at > 0 else (-1 if at < 0 else 0)

    below = [f for f in flips if f <= spot]
    above = [f for f in flips if f > spot]
    pocket = (max(below) if below else None, min(above) if above else None)
    nearest = min(flips, key=lambda z: abs(z - spot)) if flips else None
    dist = (pocket[1] - spot) if pocket[1] is not None else (
        (pocket[0] - spot) if pocket[0] is not None else None)

    call_walls = sorted(((k, c) for k, c, _ in rows), key=lambda x: -x[1])[:n_walls]
    put_walls = sorted(((k, p) for k, _, p in rows), key=lambda x: x[1])[:n_walls]

    return dict(
        curve=curve, flips=flips, flip=nearest,
        local_sign=local_sign, total_sign=total_sign, total_gex=total,
        pocket=pocket, dist_to_flip=dist,
        call_walls=call_walls, put_walls=put_walls,
        call_wall=call_walls[0][0] if call_walls else None,
        put_wall=put_walls[0][0] if put_walls else None,
        spot=float(spot), n_strikes=len(rows),
    )


__all__ = ["gamma_profile", "N_WALLS"]
