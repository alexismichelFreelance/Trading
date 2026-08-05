"""Incremental bar aggregation: input bars -> higher timeframes (30m, 1h).

CONVENTION: every Bar's `ts` is its CLOSE time (the instant it becomes known) —
the causal convention the feed uses. An input 1m bar with close ts T therefore
covers [T-60s, T); it is bucketed by its PERIOD START (T - input_secs), so the
last minute of each hour lands in that hour (not the next). Buckets align to the
UTC calendar, matching QuestDB `SAMPLE BY 30m/1h ALIGN TO CALENDAR`. A higher-TF
bar is emitted when the first input bar of the NEXT bucket arrives (strictly
causal); its `ts` is the bucket CLOSE = (bucket+1)*span. `flush()` emits any
still-open bars at end-of-stream.
"""
from __future__ import annotations

from ..core.events import Bar

_NS = 1_000_000_000
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
               "4h": 14400, "1d": 86400}


def tf_seconds(tf: str) -> int:
    return _TF_SECONDS[tf]


class _Acc:
    __slots__ = ("bucket", "span", "o", "h", "l", "c", "v", "symbol",
                 "first_ts", "last_ts")

    def __init__(self, bar: Bar, bucket: int, span: int) -> None:
        self.bucket = bucket
        self.span = span
        self.o, self.h, self.l, self.c, self.v = bar.o, bar.h, bar.l, bar.c, bar.v
        # Carried, not dropped. Bar.symbol defaults to "" and dispatch_market
        # treats "" as BROADCAST, so an aggregated bar with no symbol would be
        # delivered to every lane's strategies. Nothing dispatches these today,
        # which is the only reason it has not bitten.
        self.symbol = bar.symbol
        self.first_ts = bar.ts
        self.last_ts = bar.ts

    def merge(self, bar: Bar) -> None:
        self.h = max(self.h, bar.h)
        self.l = min(self.l, bar.l)
        self.c = bar.c
        self.v += bar.v
        self.last_ts = bar.ts

    def emit(self, tf: str, input_ns: int) -> Bar:
        # Completeness is about SPAN, not count. Counting inputs would mark a
        # merely QUIET bucket incomplete -- NT8 emits a bar only when there is
        # activity, so an overnight 30m bucket legitimately holds far fewer than
        # 30 one-minute bars. What actually breaks a bar is a truncated one: an
        # outage or the halt leaves inputs covering only part of the period, and
        # its small range then drags the zone detector's average down. So ask
        # whether the inputs reached across the bucket.
        covered = (self.last_ts - self.first_ts) + input_ns
        return Bar((self.bucket + 1) * self.span, tf, self.o, self.h, self.l,
                   self.c, self.v, self.symbol,
                   covered * 2 >= self.span)      # >= half the period spanned


class BarAggregator:
    def __init__(self, timeframes=("30m", "1h"), input_tf: str = "1m") -> None:
        self.secs = {tf: tf_seconds(tf) for tf in timeframes}
        self.input_ns = tf_seconds(input_tf) * _NS
        self._cur: dict[str, _Acc | None] = {tf: None for tf in timeframes}

    def update(self, bar: Bar) -> list[Bar]:
        """Feed an input (close-stamped) bar; return higher-TF bars that closed."""
        out: list[Bar] = []
        period_start = bar.ts - self.input_ns
        for tf, secs in self.secs.items():
            span = secs * _NS
            bucket = period_start // span
            cur = self._cur[tf]
            if cur is None:
                self._cur[tf] = _Acc(bar, bucket, span)
            elif bucket != cur.bucket:
                out.append(cur.emit(tf, self.input_ns))
                self._cur[tf] = _Acc(bar, bucket, span)
            else:
                cur.merge(bar)
        return out

    def flush(self) -> list[Bar]:
        out = []
        for tf, cur in self._cur.items():
            if cur is not None:
                out.append(cur.emit(tf, self.input_ns))
                self._cur[tf] = None
        return out


__all__ = ["BarAggregator", "tf_seconds"]
