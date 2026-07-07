"""Visual pipe check: draws an unmissable magenta test arrow + line + status box
on the NT8 chart via the relay, at the current (delayed) market time/price.

    .venv/Scripts/python.exe tools/paint_test.py          # draw the test marks
    .venv/Scripts/python.exe tools/paint_test.py --clear  # remove them

If you see them, the entire draw path (engine -> relay -> chart) is healthy.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.painter import NTChartPainter   # noqa: E402


async def last_trade(host="127.0.0.1", port=36001, wait=15.0):
    """Read the market socket briefly for the latest price/time (skip backfill)."""
    reader, writer = await asyncio.open_connection(host, port)
    px = ts = None
    t0 = time.time()
    try:
        while time.time() - t0 < wait:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if m.get("t") == "trade":
                px, ts = m["price"], m["ts"]
                break                       # first LIVE trade (backfill has none)
            if m.get("t") == "bar":         # remember last backfill bar as fallback
                px, ts = m["c"], m["ts"]
    finally:
        writer.close()
    return ts, px


async def main() -> None:
    p = NTChartPainter()
    if not await p.connect():
        print("cannot reach the relay broker socket — is EngineRelay enabled?")
        return
    if "--clear" in sys.argv:
        for tag in ("eng-paint-test", "eng-paint-test-hl"):
            await p.remove(tag)
        await p.status("ENGINE (paint test cleared)")
        print("test drawings removed")
        await p.close()
        return
    ts, px = await last_trade()
    if px is None:
        print("no market data received — is the feed connected?")
        await p.close()
        return
    await p.arrow("eng-paint-test", ts, px - 2.0, +1, color="#FFFF00FF",
                  label="PAINT TEST")
    await p.hline("eng-paint-test-hl", px + 4.0, color="#FFFF00FF")
    await p.status(f"PAINT TEST OK  px={px:.2f}\\n(if you can read this, the "
                   f"draw pipe works)")
    print(f"test arrow drawn at {px - 2.0:.2f}, line at {px + 4.0:.2f}, "
          f"status box set — check the chart (magenta)")
    await p.close()


if __name__ == "__main__":
    asyncio.run(main())
