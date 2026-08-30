"""NinjaTrader 8 .ncd minute-bar reader.

Reverse-engineered 2026-08-29 and validated bar-for-bar against QuestDB
(claude_bars_live / claude_bars_1m). NT8 ships no exporter for this format.

HEADER (28 bytes, little-endian)
    int32    version        (1)
    float64  tickSize       (0.25 for ES/NQ)
    float64  basePrice      the first record's OPEN
    int64    baseTime       .NET ticks (100ns since 0001-01-01) at 00:01 on the
                            file's date, in the PC's LOCAL timezone.
                            This machine runs Europe/Paris -- which is why the
                            record<->clock offset shifts on the EUROPEAN DST
                            date, not the US one. Getting this wrong silently
                            slides every bar by an hour for part of the year.

RECORDS  (one per minute of the 23h session, typically 1380)
    byte b1, byte b2, then the data bytes for whichever fields are present.
    Record i is the bar OPENING at local midnight + i minutes.

    Each price field has a NARROW form (1 byte) and a WIDE form (2 bytes,
    big-endian). Fields appear in this fixed order; an absent field is 0:

        open delta   b1 bit2 narrow, bias 128   b1 bit3 wide, bias 32768
        high - open  b2 bit4 narrow             b2 bit5 wide
        open - low   b2 bit6 narrow             b2 bit7 wide
        close - low  b2 bit0 narrow             b2 bit1 wide

    The open delta is relative to the PREVIOUS BAR'S OPEN, so the opens form a
    chain anchored on basePrice: one missed byte corrupts every later bar.

    Volume is keyed on b1 & 0xF0 (big-endian):
        0x20 -> 1 byte x1     0x40 -> 1 byte x100 (round lots stored in hundreds)
        0xa0 -> 2 bytes x1    0xc0 -> 4 bytes x1

RECORD LENGTH is popcount(b1) + popcount(b2) PLUS one byte for each wide price
field and two more for the 0xc0 volume form. Those extras are the whole trap:
they are rare (~2% of records, and far rarer on ES than NQ), and missing one
desynchronises the stream from that record to the end of the file, which looks
like plausible-but-wrong prices rather than an obvious crash.
"""
from __future__ import annotations
import struct, datetime
from zoneinfo import ZoneInfo
import numpy as np

_LOCAL = ZoneInfo("Europe/Paris")
_VOL = {0x20: (1, 1), 0x40: (1, 100), 0xa0: (2, 1), 0xc0: (4, 1)}


def _vol_spec(b1):
    return _VOL.get(b1 & 0xF0, (bin(b1 & 0xF0).count("1"), 1))


def _rec_len(b1, b2):
    n = bin(b1).count("1") + bin(b2).count("1")
    n += (b1 >> 3) & 1                                   # wide open delta
    n += ((b2 >> 5) & 1) + ((b2 >> 7) & 1) + ((b2 >> 1) & 1)   # wide h/l/c
    nb, _ = _vol_spec(b1)
    return n - bin(b1 & 0xF0).count("1") + nb


def read(path, tz="UTC"):
    """-> dict(ts, o, h, l, c, v, tick, base, n) or None. ts are timezone-aware."""
    b = open(path, "rb").read()
    if len(b) < 36:
        return None
    tick, = struct.unpack_from("<d", b, 4)
    base, = struct.unpack_from("<d", b, 12)
    t0, = struct.unpack_from("<q", b, 20)
    naive = datetime.datetime(1, 1, 1) + datetime.timedelta(microseconds=t0 / 10)
    start = naive.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=_LOCAL)

    ts, O, H, L, C, V = [], [], [], [], [], []
    px = base; pos = 28; i = 0
    while pos + 2 <= len(b):
        b1, b2 = b[pos], b[pos + 1]
        n = _rec_len(b1, b2)
        if pos + 2 + n > len(b):
            break
        v = b[pos + 2:pos + 2 + n]; pos += 2 + n
        k = 0

        def fld(nb, wb, flags, bias=0):
            nonlocal k
            if (flags >> wb) & 1:
                x = ((v[k] << 8) | v[k + 1]) - (32768 if bias else 0); k += 2
            elif (flags >> nb) & 1:
                x = v[k] - (128 if bias else 0); k += 1
            else:
                x = 0
            return x

        px += fld(2, 3, b1, bias=1) * tick
        ho = fld(4, 5, b2) * tick
        ol = fld(6, 7, b2) * tick
        cl = fld(0, 1, b2) * tick
        nbytes, mult = _vol_spec(b1)
        vol = 0
        for _ in range(nbytes):
            vol = (vol << 8) | v[k]; k += 1
        ts.append(start + datetime.timedelta(minutes=i))
        O.append(px); H.append(px + ho); L.append(px - ol); C.append(px - ol + cl)
        V.append(vol * mult); i += 1

    tzi = ZoneInfo(tz)
    return dict(ts=[t.astimezone(tzi) for t in ts], o=np.array(O), h=np.array(H),
                l=np.array(L), c=np.array(C), v=np.array(V, dtype=np.int64),
                tick=tick, base=base, n=i)


def session_files(root, symbol, day):
    """All .ncd files holding `day` for `symbol` (several contract months can)."""
    import glob, os
    out = []
    for folder in glob.glob(os.path.join(root, f"{symbol} *")):
        p = os.path.join(folder, f"{day:%Y%m%d}.Last.ncd")
        if os.path.exists(p) and os.path.getsize(p) > 4000:
            out.append(p)
    return out


def read_best(root, symbol, day, tz="UTC"):
    """The liquid front-month file for a day: the one with the most records.

    A back-month file for the same date decodes to garbage far more often --
    it is thin, so its minute-to-minute open gaps overflow the narrow field
    constantly, and one overflow desynchronises the rest of the file.
    """
    best = None
    for p in session_files(root, symbol, day):
        try:
            r = read(p, tz=tz)
        except Exception:
            continue
        if r and r["n"] > 600 and (best is None or r["n"] > best["n"]):
            best = r
    return best


def looks_desynced(bars, prev_close=None, tol=None):
    """Cheap guard for the ~15% of sessions that still desynchronise.

    A desync does not crash -- it yields plausible-looking but wrong prices --
    so never use a decoded session without checking it. Two signals:
      * the session's own range is absurd for the instrument
      * the first open is far from the prior session's close
    `tol` defaults to 20 points for ES-scale, 80 for NQ-scale.
    """
    import numpy as np
    if bars is None or bars["n"] < 300:
        return True
    rng = float(np.nanmax(bars["h"]) - np.nanmin(bars["l"]))
    lvl = float(np.nanmedian(bars["c"]))
    if not np.isfinite(rng) or not np.isfinite(lvl) or lvl <= 0:
        return True
    if rng > 0.25 * lvl:                      # a 25% intraday range is not real
        return True
    if prev_close is not None:
        if tol is None:
            tol = 20.0 if lvl < 15000 else 80.0
        if abs(float(bars["o"][0]) - prev_close) > tol:
            return True
    return False


__all__ = ["read", "read_best", "session_files", "looks_desynced"]
