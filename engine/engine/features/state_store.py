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

import heapq
from collections import OrderedDict, deque
from typing import Any

MAX_RAW_BARS = 1800          # closed 1m bars retained per lane (~3 trading days)
MAX_AGG_BARS = 600           # aggregated candles retained per TF per lane


def _trim_oldest(win: dict, limit: int) -> None:
    """Keep the `limit` NEWEST entries of a ts-keyed window.

    The window is bounded by TIME, not by arrival order. The NT8 feed
    replays its startup backfill NEWEST-FIRST, so a FIFO ring (deque with
    maxlen) ends up holding the OLDEST slice of the replay plus whatever
    live bars came after it -- the dashboard then renders a chart that ends
    days before the live edge. Trimming by ts keeps the window anchored to
    the newest bars whatever order the feed used."""
    excess = len(win) - limit
    if excess <= 0:
        return
    for old in sorted(win)[:excess]:
        win.pop(old, None)
MAX_LOG = 200               # log records retained
MAX_ERRORS = 60             # error/warning records retained
# Indicator points are stamped at the BAR's resolution (1m) while the chart
# draws one candle per 5 minutes: 600 candles is 3,000 minutes of chart, so a
# 1,800-point window stopped the bands 40% short of the left edge -- they
# VANISHED when the view was zoomed out, which reads as "no data" rather than
# "no history". 3,600 points cover 60 hours: the whole candle window plus a
# margin. The frame stays small because the payload is snapped to the candle
# grid before it is sent (see tools/run_dashboard.py).
SNAPSHOT_MAX_INDICATOR_POINTS = 3600


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
                "bars_raw": {},                    # ts -> BarDict (newest window)
                "bars": {},                        # tf -> {ts -> BarDict}
                "indicators": {},                 # id -> {"id","producer","label",
                                                  #        "unit","points": deque}
                "ind_max_ts": 0,                  # newest ts in the lane's indicator series
                "gamma": None,                    # GammaMap (structured, per lane)
                "strategies_roster": [],          # [RosterEntry]
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
        raw = lane["bars_raw"]
        raw[int(b["ts"])] = b
        _trim_oldest(raw, MAX_RAW_BARS)
        return self._next("bar", b["symbol"], {"bar": b})

    def apply_agg_bar(self, b: dict) -> dict | None:
        """Record one engine-aggregated higher-TF candle (already built by the
        engine's BarAggregator). Returns a delta or None if the symbol is
        unknown (cannot place without a lane)."""
        lane = self._lane(b["symbol"])
        self.ts = max(self.ts, b["ts"])
        q = lane["bars"].setdefault(b["tf"], {})
        q[int(b["ts"])] = b
        _trim_oldest(q, MAX_AGG_BARS)
        return self._next("bar", b["symbol"], {"bar": b, "aggregated": True})

    def apply_indicator(self, lane: str, series: dict) -> dict | None:
        """Record one indicator point series. `series` carries id/producer/
        label/unit/points+current. Each point {"ts","v"} — plus an optional explicit
        "key" to dedupe (two static levels that share a ts must not overwrite each
        other); default key = f"{ts}-{v}".

        APPEND-ONLY DELTA CONTRACT: the returned 'indicator' delta carries ONLY the
        points that were not already present in the store (the new points since the
        last publish for this series), never the full accumulated array. Returns
        None when nothing is new so the publisher can skip an empty broadcast — this
        is the engine-side fix for the self-DOSing "resend the whole history every
        bar" flow."""
        l = self._lane(lane)
        sid = series["id"]
        entry = l["indicators"].setdefault(sid, {
            "id": sid, "producer": "", "label": "", "unit": "",
            "points": OrderedDict(), "_heap": []})
        # overwrite metadata
        entry["producer"] = series.get("producer", entry["producer"])
        entry["label"] = series.get("label", entry["label"])
        entry["unit"] = series.get("unit", entry["unit"])
        points = entry["points"]
        heap = entry["_heap"]
        new_points = []
        for p in series.get("points", []):
            key = str(p.get("key", f"{p['ts']}-{p['v']}"))
            ts = int(p["ts"])
            if key not in points:
                new_points.append({"ts": ts, "v": p["v"], "key": key})
                # one heap entry per point, not per publication: a static
                # series (the pivot grid) republishes the same keys on every
                # bar, and a heap that grew with every republication would
                # dwarf the series it guards.
                heapq.heappush(heap, (ts, key))
            points[key] = {"ts": ts, "v": p["v"], "key": key}
            heapq.heappush(heap, (ts, key))
            if ts > l["ind_max_ts"]:
                l["ind_max_ts"] = ts
        if not new_points:
            return None
        # Enforce max size, dropping the OLDEST BY TIME.
        #
        # NOT BY ARRIVAL ORDER. The NT8 relay replays its startup backfill
        # AFTER the dashboard lane was seeded with real history, so the
        # replay's points arrive later but are OLDER. A FIFO trim keeps the
        # replay and evicts the seed: the chart then shows the relay's
        # three-day hole while the real four days of history it was given
        # sit in the trash. The window is bounded by TIME, exactly as the
        # bar windows are (see _trim_oldest).
        max_len = SNAPSHOT_MAX_INDICATOR_POINTS
        while len(points) > max_len and heap:
            _ts, k = heapq.heappop(heap)
            if k in points:
                points.pop(k, None)
        payload = {
            "id": sid,
            "producer": entry["producer"],
            "label": entry["label"],
            "unit": entry["unit"],
            # DELTA, not the whole series: sending every point again on every
            # bar is the "resend the whole history every bar" flow this
            # contract exists to prevent (a 600-bar VWAP x 6 band series
            # per bar, per lane). The client merges by ts, so it only needs
            # what is new; the full series travels in the snapshot.
            "points": [{"ts": p["ts"], "v": p["v"]} for p in new_points],
        }
        return self._next("indicator", lane, payload)

    def indicator_floor(self, lane: str) -> int:
        """The newest timestamp this lane's indicator series reaches.

        A bar OLDER than this has already been passed by the lane's indicator
        history: it is a replay, and the values a book holds NOW are the
        values at the LIVE edge. Stamping them at the replay's timestamps
        draws today's VWAP over last week's chart -- and where a seeded
        history and the replay cover the same minute, two different values
        at one timestamp, which the chart library rejects outright.
        """
        l = self._lanes.get(lane)
        return int(l["ind_max_ts"]) if l else 0

    def apply_gamma(self, lane: str, gamma_map: dict | None) -> dict | None:
        """Replace the lane's structured dealer-gamma map. The map carries the
        regime AT A PRICE, so it is refreshed per bar (the publisher recomputes
        it at the latest close — prices cross flips through the day). Returns a
        'gamma' delta only when the map actually changed; None leaves state
        alone (no duplicate deltas when the map is unchanged)."""
        l = self._lane(lane)
        if l["gamma"] == gamma_map:
            return None
        l["gamma"] = gamma_map
        return self._next("gamma", lane, {"gamma": gamma_map})

    def apply_roster(self, lane: str, roster: list) -> dict | None:
        """Replace the lane's strategy-roster list. Returns a 'roster' delta
        when the list changed (binary comparison) so identical broadcasts are
        deduped. Roster is additive + idempotent; see state_sources.strategies_roster_from."""
        l = self._lane(lane)
        if l["strategies_roster"] == roster:
            return None
        l["strategies_roster"] = list(roster)
        return self._next("roster", lane, {"roster": list(roster)})

    def apply_zones(self, lane: str, zones: list) -> dict | None:
        """Replace the lane's zone set with `zones` (each a ZoneDict). Returns a
        'zone' delta carrying the changes since the last publish, or None when
        nothing changed — the same no-op suppression the other apply_* methods
        apply: a bar that leaves the zone book untouched emits nothing, not an
        empty diff to every subscriber on every bar."""
        l = self._lane(lane)
        old = dict(l["zones"])
        new = {z["id"]: z for z in zones}
        payload = {"added": [], "updated": [], "removed": []}
        for zid, z in new.items():
            if zid not in old or old[zid] != z:
                payload["updated" if zid in old else "added"].append(z)
        payload["removed"] = sorted(set(old) - set(new))
        l["zones"] = new
        if not (payload["added"] or payload["updated"] or payload["removed"]):
            return None
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

    @staticmethod
    def _series_dict(sid: str, v: dict, limit: int = None) -> dict:
        pts = v["points"]
        # ASCENDING BY TIME, and the window's newest points last. The store
        # keeps the newest points by ts, but the dict's ORDER is the order
        # they arrived in: a feed that replays history after the live edge
        # leaves the dict with the newest points FIRST. A client that draws
        # the series in the order it was handed would draw it backwards.
        items = sorted(pts.values(), key=lambda p: int(p["ts"]))
        if limit is not None:
            items = items[-limit:]
        return {"id": sid, "producer": v["producer"], "label": v["label"],
                "unit": v["unit"],
                "points": [{"ts": p["ts"], "v": p["v"]} for p in items]}

    # ── snapshot ─────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        lanes = {}
        for sym, l in self._lanes.items():
            # LightweightCharts requires strictly ascending, unique bar time —
            # a backfill can append older bars AFTER newer ones, so sort the
            # retained bars by ts before handing them to any client.
            def _asc(b):
                return sorted(list(b), key=lambda x: int(x.get("ts", 0)))
            lanes[sym] = {
                "symbol": sym,
                "bars_raw": _asc(l["bars_raw"].values()),
                "bars": {tf: _asc(q.values()) for tf, q in l["bars"].items()},
                "indicators": [
                    StateStore._series_dict(sid, v, limit=SNAPSHOT_MAX_INDICATOR_POINTS)
                    for sid, v in sorted(l["indicators"].items())
                ],
                "gamma": l["gamma"],
                "strategies_roster": list(l["strategies_roster"]),
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



    __all__ = ["StateStore"]
