"""GammaCurve — the prior session's per-strike curve, answered at the LIVE price.

GammaLevels (this module's predecessor, still used for the flat daily row) gives
one flip, one call wall, one put wall and one sign, all fixed when the file is
read. That is enough to draw lines and not enough to say what regime price is in
right now, because the regime depends on which side of a flip price sits -- and
price moves all day.

Separating the two staleness problems is what makes this cheap:

  THE CURVE AGES. Each option's gamma depends on spot, time and implied vol,
  and open interest is published end-of-day, so today's 0DTE positioning is
  invisible. Fixing that needs intraday chain pulls or re-pricing. Real, and not
  solved here -- `sess` is exposed so a consumer can see how old the snapshot is.

  WHICH POCKET PRICE IS IN DOES NOT AGE. Cumulative gamma is a function of
  STRIKE, so the sign changes sit at fixed prices. As price crosses them the
  regime changes, and reading that off a stored curve is one interpolation and
  no new data at all.

The second is the one that was actually wrong. 2026-08-13: net_sign +1 held for
the whole session while ES ran 45 points from a short-gamma pocket into the flip
at 7837.20 (SPX 7795.20 + 42 basis) and reversed within 1.05 points of it. Even
a perfectly fresh curve, reduced to one scalar at midnight, could not have said
that -- the information is in WHERE price is on the curve, not in the curve's
summary.

Strikes are index points (SPX/NDX); everything returned is converted to FUTURE
terms with `basis`, because that is what the sleeves trade.
"""
from __future__ import annotations

import bisect
import logging

from .gamma_profile import gamma_profile

log = logging.getLogger("engine.gamma")


class GammaCurve:
    def __init__(self, rows: list, spot: float, basis: float,
                 underlying: str = "SPX", sess: str = "") -> None:
        self.underlying = underlying
        self.basis = float(basis)
        self.sess = sess
        self._prof = gamma_profile({k: (c, p) for k, c, p in rows}, spot) if rows else None
        self.ok = self._prof is not None
        if self._prof:
            self._flips = sorted(f + self.basis for f in self._prof["flips"])
            # TWO orderings, and conflating them is a bug: ranked by SIZE
            # answers "where is the wall", sorted by PRICE answers "which wall
            # is overhead". Taking the last of the price-sorted list as the
            # call wall returns the highest strike, not the biggest one.
            self._call_rank = [k + self.basis for k, _ in self._prof["call_walls"]]
            self._put_rank = [k + self.basis for k, _ in self._prof["put_walls"]]
            self._calls = sorted(self._call_rank)
            self._puts = sorted(self._put_rank)
            self._curve = [(k + self.basis, n, c) for k, n, c in self._prof["curve"]]
            # every strike ranked by |net|, carrying the net so direction is
            # decidable at a strike that tops both the call and the put list
            self._walls = sorted(((k + self.basis, n) for k, n, _ in
                                  self._prof["curve"]),
                                 key=lambda x: -abs(x[1]))[:8]
        else:
            self._flips = self._calls = self._puts = self._curve = []
            self._call_rank = self._put_rank = self._walls = []

    # ── construction ─────────────────────────────────────────────────────
    @classmethod
    def from_rows(cls, rows, spot: float, basis: float,
                  underlying: str = "SPX", sess: str = "") -> "GammaCurve":
        return cls(list(rows), spot, basis, underlying, sess)

    @classmethod
    def load_prev(cls, q, day: str, basis: float, underlying: str = "SPX",
                  table: str = "claude_gex_strikes",
                  buckets: tuple | None = None) -> "GammaCurve":
        """The most recent session STRICTLY BEFORE `day` -- the standing option
        structure the session opens into, never the row being formed today.

        `buckets` restricts by DTE ('0dte', '1-7', '8-30', '31+'). Excluding
        0dte is a legitimate choice, not a default: its open interest is the
        most stale part of the book, and also the part that moves price most."""
        where = f"WHERE underlying = '{underlying}' AND ts < '{day}T00:00:00.000000Z'"
        if buckets:
            where += " AND bucket IN (" + ",".join(f"'{b}'" for b in buckets) + ")"
        try:
            df = q.df(f"SELECT ts, strike, call_gex, put_gex, spot FROM {table} "
                      f"{where} ORDER BY ts")
        except Exception as ex:                       # noqa: BLE001
            log.warning("gamma curve unavailable (%s); falling back to the flat "
                        "daily levels row", ex)
            return cls([], 0.0, basis, underlying)
        if not len(df):
            return cls([], 0.0, basis, underlying)
        last = df["ts"].max()
        d = df[df["ts"] == last]
        # one session may carry several DTE buckets per strike: sum them
        agg: dict = {}
        for r in d.itertuples():
            c, p = agg.get(r.strike, (0.0, 0.0))
            agg[r.strike] = (c + float(r.call_gex), p + float(r.put_gex))
        rows = [(k, c, p) for k, (c, p) in agg.items()]
        return cls(rows, float(d["spot"].iloc[0]), basis, underlying,
                   sess=str(last)[:10])

    # ── the question worth asking ────────────────────────────────────────
    def at(self, price: float) -> dict | None:
        """Regime and structure AT `price`, in future terms. None if no curve."""
        if not self.ok:
            return None
        idx = float(price) - self.basis                # back to index terms
        cum = _interp(self._prof["curve"], idx)
        sign = 1 if cum > 0 else (-1 if cum < 0 else 0)
        below = [f for f in self._flips if f <= price]
        above = [f for f in self._flips if f > price]
        lo, hi = (max(below) if below else None), (min(above) if above else None)
        ca = bisect.bisect_right(self._calls, price)
        pb = bisect.bisect_left(self._puts, price)
        return dict(
            local_sign=sign, cum=cum, sess=self.sess, basis=self.basis,
            pocket=(lo, hi),
            dist_to_flip=(hi - price) if hi is not None else
                         ((lo - price) if lo is not None else None),
            flip=min(self._flips, key=lambda z: abs(z - price)) if self._flips else None,
            flips=list(self._flips),
            call_wall=self._call_rank[0] if self._call_rank else None,
            put_wall=self._put_rank[0] if self._put_rank else None,
            call_walls=list(self._call_rank), put_walls=list(self._put_rank),
            # (price, net_gex) ranked by SIZE of the net. A strike can top BOTH
            # ranked lists -- SPX on 2026-08-14 had 7800 and 8000 in each -- so
            # "is this support or resistance" cannot be answered from the lists.
            # The NET at the strike answers it: call-heavy caps, put-heavy
            # supports. Consumers that need a direction must read this.
            walls=list(self._walls),
            wall_above=self._calls[ca] if ca < len(self._calls) else None,
            wall_below=self._puts[pb - 1] if pb > 0 else None,
            underlying=self.underlying,
        )


def _interp(curve: list, x: float) -> float:
    if x <= curve[0][0]:
        return curve[0][2]
    if x >= curve[-1][0]:
        return curve[-1][2]
    for i in range(1, len(curve)):
        k0, _, c0 = curve[i - 1]
        k1, _, c1 = curve[i]
        if k0 <= x <= k1:
            return c0 if k1 == k0 else c0 + (c1 - c0) * (x - k0) / (k1 - k0)
    return curve[-1][2]


__all__ = ["GammaCurve"]
