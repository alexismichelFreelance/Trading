"""Plausibility filter for bar history read back from `claude_bars_live`.

WHY THIS EXISTS
---------------
Between 2026-09-29T22:01Z and 2026-10-02T21:00Z the recorder wrote 4,140
rows under symbol ``ES`` that carried NQ prices (30,657 - 31,280 while ES
traded 7,700 - 7,900) -- the ingest picked up the wrong instrument for a few
days. Those rows are still in QuestDB, so every loader that reads history has
to defend itself.

The damage was not limited to the chart: the zone detector seeds its book
from replayed 1m history, so a zone whose base window overlapped the bad
block inherited a bogus edge (31,159.25 / 30,768.25 / 30,700.50). The
dashboard then drew those levels on the same price scale as the candles,
stretching the scale 4x and squeezing the candles into an invisible sliver at
the bottom of the chart.

HOW THE FILTER WORKS
--------------------
The anchor is the MEDIAN close of the window -- robust to a minority of bad
rows (4,140 of ~86,000 in the affected window is 4.8%). A bar is dropped when
its o/h/l/c sit outside a wide band around that median. 50% is far wider than
any real move inside a 90-day window, so legitimate market structure is never
touched; a cross-instrument contamination is off by a factor of four, so it
always lands outside the band.

The filter is deliberately conservative: it drops rows, it never rewrites
them, and it is a no-op when the window is empty, has no usable closes, or has
a non-positive median.
"""
from __future__ import annotations

from statistics import median

# +/- 50% around the window's median close.
DEFAULT_BAND = 0.5

_PRICE_FIELDS = ("o", "h", "l", "c")


def _band(ref: float, band: float) -> tuple[float, float]:
    return (ref * (1.0 - band), ref * (1.0 + band))


def plausible_window(closes, band: float = DEFAULT_BAND) -> tuple[float, float] | None:
    """The (low, high) price band a bar in this window must fall inside.

    Returns None when the window carries no usable closes -- callers then
    leave the data alone rather than guessing.
    """
    usable = [c for c in closes if isinstance(c, (int, float)) and c > 0]
    if not usable:
        return None
    ref = float(median(usable))
    if ref <= 0:
        return None
    return _band(ref, band)


def filter_frame(df, band: float = DEFAULT_BAND):
    """Drop rows of a bars DataFrame whose prices are implausible.

    `df` is the QuestDB shape (ts, o, h, l, c, vol). Returns (kept_df, n_dropped).
    """
    if df is None or not len(df):
        return df, 0
    import pandas as pd

    closes = pd.to_numeric(df["c"], errors="coerce")
    band = plausible_window(closes.dropna().tolist(), band)
    if band is None:
        return df, 0
    lo, hi = band
    keep = pd.Series(True, index=df.index)
    for col in _PRICE_FIELDS:
        if col not in df.columns:
            continue
        v = pd.to_numeric(df[col], errors="coerce")
        keep &= v.notna() & (v >= lo) & (v <= hi)
    dropped = int((~keep).sum())
    if not dropped:
        return df, 0
    return df[keep], dropped


def filter_bars(bars, band: float = DEFAULT_BAND):
    """Drop Bar objects whose prices are implausible. Returns (kept, n_dropped)."""
    bars = list(bars or [])
    if not bars:
        return bars, 0
    closes = [getattr(b, "c", None) for b in bars]
    win = plausible_window([c for c in closes if c is not None], band)
    if win is None:
        return bars, 0
    lo, hi = win
    kept = []
    for b in bars:
        vals = [getattr(b, f, None) for f in _PRICE_FIELDS]
        if all(v is not None and lo <= v <= hi for v in vals):
            kept.append(b)
    return kept, len(bars) - len(kept)
