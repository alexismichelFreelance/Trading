"""NTChartPainter — draws on the NT8 chart through the EngineOverlay INDICATOR's
socket (draw port 36004), NOT the strategy.

The drawing lives in an indicator (EngineOverlay) so it coexists with NT's native
order/execution display — a STRATEGY on a chart hides those, an indicator does
not. This client only ever SENDS "draw" messages (arrow / rect / hline / line /
text / status / remove). Same-tag redraw REPLACES the object on the chart (NT
semantics). Fire-and-forget: a dead socket disables painting with a warning
rather than touching the trading path.
"""
from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("engine.painter")


class NTChartPainter:
    def __init__(self, host: str = "127.0.0.1", port: int = 36004) -> None:
        self.host, self.port = host, port
        self._w: asyncio.StreamWriter | None = None
        self.enabled = False

    async def connect(self) -> bool:
        try:
            _, self._w = await asyncio.open_connection(self.host, self.port)
            self.enabled = True
        except OSError as ex:
            log.warning("painter: cannot connect %s:%d (%s) — painting disabled",
                        self.host, self.port, ex)
            self.enabled = False
        return self.enabled

    async def close(self) -> None:
        if self._w is not None:
            try:
                self._w.close()
                await self._w.wait_closed()
            except Exception:                     # noqa: BLE001
                pass
        self.enabled = False

    async def _send(self, obj: dict) -> None:
        if not self.enabled or self._w is None:
            return
        obj["t"] = "draw"
        try:
            self._w.write((json.dumps(obj) + "\n").encode())
            await self._w.drain()
        except Exception as ex:                   # noqa: BLE001
            log.warning("painter: send failed (%s) — painting disabled", ex)
            self.enabled = False

    # ── drawing vocabulary ────────────────────────────────────────────────
    async def arrow(self, tag: str, ts: int, price: float, direction: int,
                    color: str = "", label: str = "") -> None:
        await self._send(dict(kind="arrow", tag=tag, ts=ts, price=price,
                              dir=direction, color=color, label=label))

    async def rect(self, tag: str, t1: int, p1: float, t2: int, p2: float,
                   color: str = "", opacity: int = 18) -> None:
        await self._send(dict(kind="rect", tag=tag, t1=t1, p1=p1, t2=t2, p2=p2,
                              color=color, opacity=opacity))

    async def hline(self, tag: str, price: float, color: str = "") -> None:
        await self._send(dict(kind="hline", tag=tag, price=price, color=color))

    async def line(self, tag: str, t1: int, p1: float, t2: int, p2: float,
                   color: str = "") -> None:
        await self._send(dict(kind="line", tag=tag, t1=t1, p1=p1, t2=t2, p2=p2,
                              color=color))

    async def text(self, tag: str, ts: int, price: float, label: str,
                   color: str = "") -> None:
        await self._send(dict(kind="text", tag=tag, ts=ts, price=price,
                              label=label, color=color))

    async def status(self, label: str, tag: str = "eng-status",
                     pos: str = "topleft") -> None:
        await self._send(dict(kind="status", tag=tag, label=label, pos=pos))

    async def remove(self, tag: str) -> None:
        await self._send(dict(kind="remove", tag=tag))


__all__ = ["NTChartPainter"]
