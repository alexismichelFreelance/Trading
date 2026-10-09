"""StateStore — the PURE, transport-free accumulator behind the state-publish
adapter.

This module owns the entire state-publish SCHEMA and the snapshot/delta
semantics that a future dashboard (and a WebSocket binding) will consume. It is
deliberately pure: no engine references, no I/O, no asyncio, no logging side
effects. The dirty adapter (engine/adapters/state_publish.py) feeds it with
plain payloads extracted from the engine's real objects.

WHY A PURE STORE. The hexagonal core is pure and the adapters implement ports by
shape. State-publish could be a port; this keeps the *contract* (what the
dashboard is allowed to depend on) in one place, unit-testable without spinning
up an engine, and identical whether the producer is a live engine, a replay, or
a test. Snapshot + delta correctness is tested here directly.

## Schema (the 5-line README the dashboard consumes blind)

snapshot() -> {
  "seq": int,                       # monotonic; every apply bumps it
  "ts":  int,                       # last event epoch-ns UTC (0 until any event)
  "lanes": { "<sym>" : {
     "bars_raw":  [BarDict],        # last N closed 1m bars, asc by ts
     "bars":      {tf: [BarDict]},  # engine-aggregated higher-TF candles, asc
     "indicators":[PointSeries],    # engine-computed indicator lines, tagged
     "zones":     [ZoneDict],       # engine zone geometry + lifecycle status
     "positions":[PositionDict],    # open fills/sleeves with entry/target/stop
  }},
  "log":  [LogRecord],              # last N info/debug engine log records
  "errors":[LogRecord],            # ERROR/WARNING surfaced separately
}

BarDict      = {"ts","tf","o","h","l","c","v","complete"}
PointSeries  = {"id","producer","label","unit","points":[{"ts","v"}]}
ZoneDict     = {"id","tf","top","bot","direction","virgin","broken","touches",
                "composite","formed_ts","proximal","distal"}
PositionDict = {"sleeve","symbol","qty","avg_px","entry","target","stop"}

Delta: {"seq":N,"type":<kind>,"lane":<sym|"">,"payload":{...}}
  kinds: bar | indicator | zone | position | fill | log | error | session | lane

TRIGGERS (when each kind is published):
  bar        every closed 1m bar per lane (+ any aggregated TF bars that close)
  indicator  per bar (vwap) / per session rollover (pivots, gamma) — see the
             adapter's source extractors for the exact cadence
  zone       per bar (geometry/status from the engine ZoneBook)
  position   per fill and per bar (levels from strategy.chart_marks())
  fill       each live/paper fill
  log/error  each engine log record (error/warning vs info/debug split)
  session    on ET session rollover
  lane       on feed disconnect / lane-stale transitions
"""
from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any

MAX_RAW_BARS = 300          # closed 1m bars retained per lane
MAX_AGG_BARS = 400          # aggregated candles retained per TF per lane
MAX_LOG = 200               # log records retained
MAX_ERRORS = 60             # error/warning records retained


class StateStore:
    """Pure snapshot + delta accumulator. Thread-free, engine-free."""

    def __init__(self) -> None:
        self.seq = 0
        self.ts = 0
        self._lanes: dict[str, dict] = {}   # sym -> lane state (OrderedDicts)
        self._log: deque[dict] = deque(maxlen=MAX_LOG)
        self._errors: deque[dict] = deque(maxlen=MAX_ERRORS)

    # ── lane helpers ─────────────────────────────────────────────────────
    def _lane(self, sym: str) -> dict:
        lane = self._lanes.get(sym)
        if lane is None:
            lane = self._lanes[sym] = {
                "bars_raw": deque(maxlen=MAX_RAW_BARS),
                "bars": {},                       # tf -> deque
                "indicators": {},                 # id -> {"id","producer","label",
                                                  #        "unit","points": deque}
                "zones": {},                      # id -> ZoneDict
                "positions": {},                  # sleeve -> PositionDict
                "extra": {},                      # anything a lane wants to keep
            }
        return lane

    def _next(self, kind: str, lane: str, payload: dict) -> dict:
        self.seq += 1
        return {"seq": self.seq, "type": kind, "lane": lane, "payload": payload}

    # ── apply: the only way state changes ────────────────────────────────
    def apply_bar(self, b: dict) -> dict:
        """Record one closed bar (raw 1m canonical). `b` is a BarDict dict."""
        lane = self._lane(b["symbol"])
        self.ts = max(self.ts, b["ts"])
        lane["bars_raw"].append(b)
        return self._next("bar", b["symbol"], {"bar": b})

    def apply_agg_bar(self, b: dict) -> dict | None:
        """Record one engine-aggregated higher-TF candle (already built by the
        engine's BarAggregator). Returns a delta or None if the symbol is
        unknown (cannot place without a lane)."""
        lane = self._lane(b["symbol"])
        self.ts = max(self.ts, b["ts"])
        q = lane["bars"].setdefault(b["tf"], deque(maxlen=MAX_AGG_BARS))
        q.append(b)
        return self._next("bar", b["symbol"], {"bar": b, "aggregated": True})

    def apply_indicator(self, lane: str, series: dict) -> dict:
        """Set/replace one indicator point series. `series` carries id/producer/
        label/unit/points+current. Each point {"ts","v"} — plus an optional explicit
        "key" to dedupe (two static levels that share a ts must not overwrite each
        other); default key = f"{ts}-{v}". Returns an 'indicator' delta."""
        l = self._lane(lane)
        sid = series["id"]
        pts = l["indicators"].setdefault(sid, {"id": sid, "producer": "",
                                            "label": "", "unit": "",
                                            "points": OrderedDict()})
        # overwrite the metadata, keep the point ring
        pts["producer"] = series.get("producer", pts.get("producer", ""))
        pts["label"] = series.get("label", pts.get("label", ""))
        pts["unit"] = series.get("unit", pts.get("unit", ""))
        for p in series.get("points", []):
            key = str(p.get("key", "{}-{}".format(p["ts"], p["v"])))
            pts["points"][key] = {"ts": int(p["ts"]), "v": p["v"], "key": key}
        payload = {"id": sid, "producer": pts["producer"], "label": pts["label"],
                   "unit": pts["unit"],
                   "points": [{"ts": p["ts"], "v": p["v"]}
                              for p in pts["points"].values()]}
        return self._next("indicator", lane, payload)

    def apply_zones(self, lane: str, zones: list) -> dict:
        """Replace the lane's zone set with `zones` (each a ZoneDict). Returns a
        'zone' delta carrying the changes since the last publish."""
        l = self._lane(lane)
        old = dict(l["zones"])
        new = {z["id"]: z for z in zones}
        payload = {"added": [], "updated": [], "removed": []}
        for zid, z in new.items():
            if zid not in old or old[zid] != z:
                payload["updated" if zid in old else "added"].append(z)
        payload["removed"] = sorted(set(old) - set(new))
        l["zones"] = new
        return self._next("zone", lane, payload)

    def apply_position(self, position: dict) -> dict | None:
        """Upsert one open position; None clears it. Returns a 'position' delta
        only when the position actually changed."""
        lane = self._lane(position["symbol"])
        key = position["sleeve"]
        old = lane["positions"].get(key)
        if old == position:
            return None
        lane["positions"][key] = position
        return self._next("position", position["symbol"], {"position": position})

    def apply_fill(self, f: dict) -> dict:
        """Not a position change, but a fill MARK: a one-off point at ts/price."""
        self.ts = max(self.ts, f.get("ts", 0))
        return self._next("fill", f.get("symbol", ""), f)

    def apply_log(self, record: dict) -> dict:
        """Record one engine log line (already dictified: ts, level, message,
        logger). ERROR/WARNING are surfaced in the errors ring too."""
        self._log.append(record)
        kind = "log"
        if record.get("level") in ("ERROR", "WARNING", "CRITICAL"):
            self._errors.append(record)
            kind = "error"
        return self._next(kind, record.get("lane", ""), record)

    def apply_session(self, day: str) -> dict:
        return self._next("session", "", {"day": day})

    def apply_lane(self, lane: str, status: dict) -> dict:
        """Lane feed status: e.g. {"symbol": s, "action": "disconnect", "n": x}."""
        return self._next("lane", lane, status)

    # ── snapshot ─────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        lanes = {}
        for sym, l in self._lanes.items():
            lanes[sym] = {
                "symbol": sym,
                "bars_raw": list(l["bars_raw"]),
                "bars": {tf: list(q) for tf, q in l["bars"].items()},
                "indicators": [self._series_dict(sid, v) for sid, v
                               in sorted(l["indicators"].items())],
                "zones": sorted(l["zones"].values(),
                                key=lambda z: (z.get("tf", ""), z.get("formed_ts", 0))),
                "positions": sorted(l["positions"].values(),
                                    key=lambda p: p.get("sleeve", "")),
            }
        return {
            "seq": self.seq,
            "ts": self.ts,
            "lanes": lanes,
            "log": list(self._log),
            "errors": list(self._errors),
        }

    @staticmethod
    def _series_dict(sid: str, v: dict) -> dict:
        return {"id": sid, "producer": v["producer"], "label": v["label"],
                "unit": v["unit"],
                "points": [{"ts": p["ts"], "v": p["v"]} for p in v["points"].values()]}


__all__ = ["StateStore"]
