"""RiskSupervisor — central, engine-level order safety (live path only).

Born from the 2026-07-09 16:09 ET burst (105 contracts churned in 3 seconds).
The anatomy exposed three gaps, each closed here:

1. Duplicate flattens: reduce-only orders were sized off the CONFIRMED
   position while an identical flatten was still in flight, so the second
   21-lot flatten passed vetting, overshot to short, and triggered a
   counter-flatten oscillation. Fix: reduces are vetted against the confirmed
   position MINUS in-flight reduces on the same book; entries against the
   confirmed position PLUS all in-flight exposure.
2. Signal re-fire: a sleeve emitted 10 orders in 1.1s (one per tick).
   Fix: per-strategy rolling order-rate limit.
3. Last-minute entries: zones opened 30 fresh lots in the final data-minute
   of RTH and then fought the end-of-day logic. Fix: an ET entry lockout and
   a single supervisor-owned EOD flatten path.

Plus per-sleeve / account-gross position caps and a daily-loss kill switch
(flatten everything, halt until the next ET session).

Every production limit defaults to None (off) so unit-test engines and the
replay/parity path are untouched; tools/run_live.py builds the live config.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from .orders import Order
from .timeutil import et_minute_of_day, et_session_date

log = logging.getLogger("engine.risk")

# lockout/EOD blocks apply from their ET cutoff until the 18:00 ET Globex
# reopen — NOT until midnight, so deliberately-deployed evening sleeves would
# not be silently blocked.
EVENING_ET_MIN = 18 * 60


def _sign(x) -> int:
    return int(x > 0) - int(x < 0)


@dataclass
class RiskConfig:
    point_value: float = 50.0                # fallback $/pt (single-instrument legacy)
    point_usd: dict[str, float] | None = None   # per-symbol $/pt (multi-instrument)
    max_pos_per_sleeve: int | None = None    # cap on |attributed position| per strategy
    max_account_gross: int | None = None     # cap on sum of |sleeve exposure| (contracts)
    # dollar-notional caps — comparable across instruments (contracts x px x $/pt).
    # Contract caps above stay enforceable IN ADDITION (belt and suspenders).
    max_sleeve_notional_usd: float | None = None
    max_gross_notional_usd: float | None = None
    rate_max_orders: int | None = None       # per strategy per rolling rate_window_s
    rate_window_s: float = 5.0
    daily_loss_halt: float | None = None     # USD (negative); marked = realized + open
    entry_lockout_et: tuple[int, int] | None = None   # e.g. (15, 45): no new exposure
    eod_flatten_et: tuple[int, int] | None = None     # e.g. (15, 58): flatten all books
    inflight_ttl_s: float = 20.0             # pending order expiry (lost-fill safety)
    reemit_s: float = 10.0                   # retry cadence for eod/halt flattens
    swing_sleeves: tuple = ()                # sleeves that ENTER late + HOLD overnight
    # by design (e.g. IBS at 15:59): exempt from the entry lockout and the EOD
    # flatten so they can actually trade. The daily-loss kill switch still
    # applies (catastrophe backstop).


class RiskSupervisor:
    """One instance per LiveEngine. Not thread-safe by design: the engine's
    single-consumer queue is the only caller."""

    def __init__(self, cfg: RiskConfig | None = None, now_fn=time.monotonic) -> None:
        self.cfg = cfg or RiskConfig()
        self._now = now_fn
        # in-flight: order_id -> [sid, signed_qty_remaining, monotonic_deadline]
        self._pending: dict[str, list] = {}
        self._stamps: dict[int, deque] = defaultdict(deque)   # sid -> order times
        # supervisor's own avg-cost books (independent of engine attribution,
        # so the kill switch is self-contained and unit-testable)
        self._pos: dict[int, int] = {}
        self._avg: dict[int, float] = {}
        self._sym: dict[int, str] = {}       # sid -> symbol (learned from orders/books)
        self._px: dict[str, float] = {}      # symbol -> last mark (note_price)
        self._realized = 0.0
        self._day: str | None = None
        self.halted = False
        self._eod_done = False
        self._last_emit = -1e18
        self.denials = 0

    # ── session / bookkeeping ────────────────────────────────────────────
    def _roll_day(self, ts: int) -> None:
        day = et_session_date(ts)
        if day != self._day:
            if self._day is not None:
                log.info("risk: new session %s (was %s); counters reset", day, self._day)
            self._day = day
            self._realized = 0.0
            self.halted = False
            self._eod_done = False
            self._stamps.clear()

    def _expire_pending(self) -> None:
        now = self._now()
        dead = [oid for oid, (_, _, dl) in self._pending.items() if now >= dl]
        for oid in dead:
            sid, q, _ = self._pending.pop(oid)
            log.warning("risk: pending order %s (%+d) expired without fill", oid, q)

    def _pending_net(self, sid: int) -> int:
        return sum(q for s, q, _ in self._pending.values() if s == sid)

    def _pending_reduces(self, sid: int, pos_sign: int) -> int:
        """In-flight qty already working AGAINST a position of sign pos_sign."""
        return sum(abs(q) for s, q, _ in self._pending.values()
                   if s == sid and _sign(q) == -pos_sign)

    def _gross(self) -> int:
        expo: dict[int, int] = dict(self._pos)
        for s, q, _ in self._pending.values():
            expo[s] = expo.get(s, 0) + q
        return sum(abs(v) for v in expo.values())

    # ── multi-instrument helpers ─────────────────────────────────────────
    def _pv(self, symbol: str) -> float:
        """$/pt for a symbol; falls back to the legacy scalar point_value."""
        if self.cfg.point_usd:
            v = self.cfg.point_usd.get(symbol)
            if v is not None:
                return v
        return self.cfg.point_value

    def note_price(self, symbol: str, px: float) -> None:
        """Engine feeds every lane's last price (also during warmup) so dollar
        caps always have a mark by the time the first live order is vetted."""
        if px > 0:
            self._px[symbol] = px

    def _mark(self, symbol: str) -> float:
        return self._px.get(symbol) or self._px.get("", 0.0)

    def _gross_usd(self) -> float:
        """Account gross in dollars: sum |confirmed + in-flight| x px x $/pt."""
        expo: dict[int, int] = dict(self._pos)
        for s, q, _ in self._pending.values():
            expo[s] = expo.get(s, 0) + q
        out = 0.0
        for s, q in expo.items():
            if q == 0:
                continue
            sym = self._sym.get(s, "")
            px = self._mark(sym)
            if px <= 0:
                log.warning("risk: no mark for %r book (sid %d) — its notional "
                            "reads as 0 in the gross cap", sym, s)
                continue
            out += abs(q) * px * self._pv(sym)
        return out

    # ── the vet ──────────────────────────────────────────────────────────
    def vet(self, sid: int, name: str, o: Order, ts: int, pos: int) -> Order | None:
        """Returns the (possibly clamped) order, or None to drop. `pos` is the
        engine's confirmed attributed position for this strategy."""
        self._roll_day(ts)
        self._expire_pending()
        c = self.cfg
        self._sym[sid] = o.symbol            # learn the sleeve's instrument

        if self.halted:
            self._deny(name, o, "halted (daily loss)")
            return None
        m = et_minute_of_day(ts)
        swing = name in c.swing_sleeves          # enters late + holds overnight by design
        if self._eod_done and m < EVENING_ET_MIN and not swing:
            self._deny(name, o, "post-EOD")
            return None

        if o.reduce_only:
            if pos == 0 or _sign(o.side) == _sign(pos):
                log.info("dropped reduce_only %s from %s (own pos %d)", o.tag, name, pos)
                return None
            avail = abs(pos) - self._pending_reduces(sid, _sign(pos))
            if avail <= 0:
                self._deny(name, o, f"reduce already in flight (pos {pos:+d})")
                return None
            if o.qty > avail:
                o = dataclasses.replace(o, qty=avail)
        else:
            base = pos + self._pending_net(sid)
            projected = base + o.side * o.qty
            increases = abs(projected) > abs(base)
            if increases and c.entry_lockout_et is not None and not swing:
                lock = c.entry_lockout_et[0] * 60 + c.entry_lockout_et[1]
                if lock <= m < EVENING_ET_MIN:
                    self._deny(name, o, "entry lockout (late session)")
                    return None
            if increases and c.max_pos_per_sleeve is not None:
                room = c.max_pos_per_sleeve - abs(base) if _sign(projected) == o.side \
                    else c.max_pos_per_sleeve + abs(base)
                if room <= 0:
                    self._deny(name, o, f"sleeve cap {c.max_pos_per_sleeve} (expo {base:+d})")
                    return None
                if o.qty > room:
                    log.warning("risk: clamped %s %s qty %d -> %d (sleeve cap)",
                                name, o.tag, o.qty, room)
                    o = dataclasses.replace(o, qty=room)
            if increases and c.max_account_gross is not None:
                head = c.max_account_gross - self._gross()
                if head <= 0:
                    self._deny(name, o, f"account gross cap {c.max_account_gross}")
                    return None
                if o.qty > head:
                    log.warning("risk: clamped %s %s qty %d -> %d (account cap)",
                                name, o.tag, o.qty, head)
                    o = dataclasses.replace(o, qty=head)
            if increases and (c.max_sleeve_notional_usd is not None
                              or c.max_gross_notional_usd is not None):
                px = self._mark(o.symbol)
                if px <= 0:                    # never let an unpriced lane bypass $ caps
                    self._deny(name, o, f"no mark price for {o.symbol!r} (notional cap)")
                    return None
                unit = px * self._pv(o.symbol)             # $ per contract
                if c.max_sleeve_notional_usd is not None:
                    room = int((c.max_sleeve_notional_usd - abs(base) * unit) // unit)
                    if room <= 0:
                        self._deny(name, o, f"sleeve notional cap "
                                            f"${c.max_sleeve_notional_usd:,.0f} (expo {base:+d})")
                        return None
                    if o.qty > room:
                        log.warning("risk: clamped %s %s qty %d -> %d (sleeve $ cap)",
                                    name, o.tag, o.qty, room)
                        o = dataclasses.replace(o, qty=room)
                if c.max_gross_notional_usd is not None:
                    head = int((c.max_gross_notional_usd - self._gross_usd()) // unit)
                    if head <= 0:
                        self._deny(name, o, f"gross notional cap "
                                            f"${c.max_gross_notional_usd:,.0f}")
                        return None
                    if o.qty > head:
                        log.warning("risk: clamped %s %s qty %d -> %d (gross $ cap)",
                                    name, o.tag, o.qty, head)
                        o = dataclasses.replace(o, qty=head)

        if c.rate_max_orders is not None:
            now = self._now()
            st = self._stamps[sid]
            while st and now - st[0] > c.rate_window_s:
                st.popleft()
            if len(st) >= c.rate_max_orders:
                self._deny(name, o, f"rate limit {c.rate_max_orders}/{c.rate_window_s}s")
                return None
        return o

    def _deny(self, name: str, o: Order, why: str) -> None:
        self.denials += 1
        log.warning("risk DENY %s %s side=%+d qty=%d: %s", name, o.tag, o.side, o.qty, why)

    # ── lifecycle hooks ──────────────────────────────────────────────────
    def on_submit(self, sid: int, o: Order) -> None:
        self._pending[o.order_id] = [sid, o.side * o.qty,
                                     self._now() + self.cfg.inflight_ttl_s]
        self._stamps[sid].append(self._now())

    def on_fill(self, sid: int, order_id: str, size: int, price: float,
                symbol: str = "") -> None:
        """size is SIGNED filled qty. `symbol` keys the per-symbol $/pt; omitted
        (legacy callers) falls back to the sleeve's learned symbol."""
        if symbol:
            self._sym[sid] = symbol
        pv = self._pv(symbol or self._sym.get(sid, ""))
        p = self._pending.get(order_id)
        if p is not None:
            p[1] -= size
            if p[1] == 0 or _sign(p[1]) != _sign(p[1] + size):
                self._pending.pop(order_id, None)
        old = self._pos.get(sid, 0)
        new = old + size
        if old == 0 or _sign(new) != _sign(old):
            if old != 0:                                   # crossed through flat
                self._realized += (price - self._avg.get(sid, price)) * old * pv
            self._avg[sid] = price
        elif _sign(size) == _sign(old):
            self._avg[sid] = (self._avg[sid] * abs(old) + price * abs(size)) \
                / (abs(old) + abs(size))
        else:                                              # partial reduce
            self._realized += (price - self._avg.get(sid, price)) * _sign(old) \
                * min(abs(size), abs(old)) * pv
        self._pos[sid] = new

    # ── supervisor-owned actions (EOD flatten, kill switch) ──────────────
    def marked_pnl(self, last_px: float | dict[str, float]) -> float:
        """`last_px` may be one price (legacy single-instrument) or a
        {symbol: price} dict. A book with no usable mark is SKIPPED with an
        error log (never silently mispriced with another symbol's price)."""
        prices = last_px if isinstance(last_px, dict) else None
        unreal = 0.0
        for s, p in self._pos.items():
            if p == 0:
                continue
            sym = self._sym.get(s, "")
            px = (prices.get(sym) or prices.get("", 0.0)) if prices is not None \
                else float(last_px)
            if px <= 0:
                log.error("risk: no mark price for %r book (sid %d) — skipped "
                          "in marked P&L", sym, s)
                continue
            unreal += (px - self._avg.get(s, px)) * p * self._pv(sym)
        return self._realized + unreal

    def on_market(self, ts: int, last_px: float | dict[str, float],
                  books: list[tuple[int, str, str, int]]) -> list[tuple[int, Order]]:
        """Called by the engine on market events (live only). `last_px` is one
        price (legacy) or {symbol: price}. `books` is [(sid, name, symbol,
        confirmed_pos)]. Returns supervisor flatten orders as (sid, Order) —
        the engine submits them owned by that strategy so attribution stays
        consistent. Do NOT re-vet them."""
        self._roll_day(ts)
        self._expire_pending()
        if isinstance(last_px, dict):
            for sym, px in last_px.items():
                self.note_price(sym, px)
        for sid, _n, symbol, _p in books:                  # learn sid -> symbol
            self._sym[sid] = symbol
        c = self.cfg
        have_px = bool(last_px) if isinstance(last_px, dict) else last_px > 0
        tag = None
        if c.daily_loss_halt is not None and not self.halted and have_px \
                and self.marked_pnl(last_px) <= c.daily_loss_halt:
            self.halted = True
            tag = "risk_halt"
            log.error("risk KILL SWITCH: marked P&L %.0f <= %.0f — flattening all books",
                      self.marked_pnl(last_px), c.daily_loss_halt)
        if c.eod_flatten_et is not None and not self._eod_done:
            m = et_minute_of_day(ts)
            if c.eod_flatten_et[0] * 60 + c.eod_flatten_et[1] <= m < EVENING_ET_MIN:
                self._eod_done = True
                tag = tag or "risk_eod"
                log.info("risk: EOD flatten at %s", et_session_date(ts))
        if not (self.halted or self._eod_done):
            return []
        if tag is None:                                    # retry path
            if self._now() - self._last_emit < c.reemit_s:
                return []
            tag = "risk_halt" if self.halted else "risk_eod"
        out: list[tuple[int, Order]] = []
        eod_only = tag == "risk_eod"                       # kill switch flattens ALL;
        for sid, name, symbol, pos in books:               # EOD spares swing sleeves
            if pos == 0:
                continue
            if eod_only and name in c.swing_sleeves:
                continue                                   # IBS holds overnight by design
            avail = abs(pos) - self._pending_reduces(sid, _sign(pos))
            if avail <= 0:
                continue                                   # flatten already working
            o = Order(symbol, -_sign(pos), avail, tag=tag, reduce_only=True)
            log.warning("risk: %s flatten %s %+d -> submit %s qty %d",
                        tag, name, pos, "SELL" if pos > 0 else "BUY", avail)
            out.append((sid, o))
        if out:
            self._last_emit = self._now()
        return out


__all__ = ["RiskConfig", "RiskSupervisor"]
