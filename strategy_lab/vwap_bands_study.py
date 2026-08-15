"""Are VWAP +/- k*sigma good reversal points? Observation first, on the RIGHT line.

The original tools/avwap_study.py answered a version of this and found nothing.
It could not have found anything: it ran on a curated RTH-only minute table and
anchored the VWAP at 09:30 ET. The line the user trades is NT8's VWAPX -- reset
at midnight on the chart clock (18:00 ET), calculated on bar close. On
2026-08-05 those two lines sat 6 POINTS apart. Every band drawn off the old one
was in the wrong place, so "no reversal edge at the bands" was never a finding
about the market.

This redoes it on the correct line. Method:

  * VWAP and sigma accumulate from the 18:00 ET reset, bar close x bar volume,
    using the engine's own AnchoredVWAP so the study and the sleeve cannot drift
    apart.
  * A TOUCH of band k is the first bar whose range reaches vwap +/- k*sigma
    after having been inside it. First touch per band per side per session only:
    a level poked five times in ten minutes is one event, not five, and counting
    it five times is how a study invents significance.
  * Only touches between 09:30 and 15:00 ET are used, so every event has an hour
    of session left to be measured against.
  * Reversion return is signed so POSITIVE = the band held (price came back
    toward VWAP). Upper touch -> short; lower touch -> long.
  * CONTROL: for every touch, a random bar from the same session and the same
    RTH window, measured identically. Drift, time-of-day and the session's own
    character are in both arms, so only the band itself can separate them. The
    number that matters is (touch - control), never the raw touch return.

What this cannot tell you: ~35 sessions of one instrument in one regime. It can
rule a band out, or say "worth carrying forward". It cannot establish an edge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB          # noqa: E402
from engine.features.avwap import AnchoredVWAP       # noqa: E402

ET = "America/New_York"
GLOBEX_OPEN_MIN = 18 * 60
RTH_OPEN, RTH_LAST_ENTRY, RTH_CLOSE = 9 * 60 + 30, 15 * 60, 15 * 60 + 59
BANDS = (1.0, 2.0, 3.0)
HORIZONS = (15, 30, 60)          # minutes forward
PV = 50.0                        # ES $/point
RNG = np.random.default_rng(7)


def load(symbol: str = "ES") -> pd.DataFrame:
    q = QuestDB(timeout=120)
    df = q.df(f"SELECT ts,o,h,l,c,vol FROM claude_bars_live WHERE symbol='{symbol}' "
              f"ORDER BY ts")
    df["et"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(ET)
    df["mod"] = df["et"].dt.hour * 60 + df["et"].dt.minute
    # session key: 18:00 ET starts the NEXT session, matching VWAPX's midnight
    # reset on a UTC+2 chart
    day = df["et"].dt.normalize()
    df["sess"] = np.where(df["mod"] >= GLOBEX_OPEN_MIN,
                          (day + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d"),
                          day.dt.strftime("%Y-%m-%d"))
    return df


def session_lines(g: pd.DataFrame) -> pd.DataFrame:
    """VWAP + sigma bar by bar, exactly as the sleeve builds them."""
    av = AnchoredVWAP()
    v, s = [], []
    for c, vol in zip(g["c"].to_numpy(), g["vol"].to_numpy()):
        av.add(float(c), float(vol or 0))
        v.append(av.value)
        s.append(av.sigma)
    out = g.copy()
    out["vwap"] = v
    out["sigma"] = s
    return out


def forward(g: pd.DataFrame, i: int, mins: int) -> float | None:
    """Close `mins` bars ahead (1m bars), or None if the session ends first."""
    j = i + mins
    if j >= len(g):
        return None
    if g["mod"].iat[j] > RTH_CLOSE or g["sess"].iat[j] != g["sess"].iat[i]:
        return None
    return float(g["c"].iat[j])


def touches(g: pd.DataFrame) -> list[dict]:
    """First touch of each band/side per session, inside the entry window."""
    out: list[dict] = []
    done: set = set()
    mod = g["mod"].to_numpy()
    hi, lo = g["h"].to_numpy(), g["l"].to_numpy()
    vw, sg = g["vwap"].to_numpy(), g["sigma"].to_numpy()
    for i in range(1, len(g)):
        if not (RTH_OPEN <= mod[i] <= RTH_LAST_ENTRY):
            continue
        if sg[i] <= 0:
            continue
        for k in BANDS:
            for side, level, reached in (
                    (-1, vw[i] + k * sg[i], hi[i] >= vw[i] + k * sg[i]),   # upper -> short
                    (+1, vw[i] - k * sg[i], lo[i] <= vw[i] - k * sg[i])):  # lower -> long
                key = (k, side)
                if key in done or not reached:
                    continue
                done.add(key)
                out.append({"i": i, "k": k, "side": side, "level": float(level),
                            "mod": int(mod[i]), "vwap": float(vw[i]),
                            "sigma": float(sg[i])})
    return out


def control_rows(g: pd.DataFrame, n: int) -> list[int]:
    """Random bars from the same session's entry window — the drift baseline."""
    idx = np.where((g["mod"].to_numpy() >= RTH_OPEN)
                   & (g["mod"].to_numpy() <= RTH_LAST_ENTRY))[0]
    if len(idx) == 0:
        return []
    return list(RNG.choice(idx, size=n, replace=True))


def main(symbol: str = "ES") -> None:
    df = load(symbol)
    rows: list[dict] = []
    sessions = 0
    for sess, g in df.groupby("sess", sort=True):
        g = g.sort_values("ts").reset_index(drop=True)
        if (g["mod"].between(RTH_OPEN, RTH_CLOSE)).sum() < 300:
            continue                                  # incomplete session
        sessions += 1
        g = session_lines(g)
        ts = touches(g)
        ctrl = control_rows(g, len(ts))
        for t, ci in zip(ts, ctrl):
            for h in HORIZONS:
                fw = forward(g, t["i"], h)
                cf = forward(g, int(ci), h)
                if fw is None or cf is None:
                    continue
                # POSITIVE = the band held (price reverted toward VWAP)
                rev = (fw - t["level"]) * t["side"]
                cbase = float(g["c"].iat[int(ci)])
                cre = (cf - cbase) * t["side"]
                rows.append({"sess": sess, "k": t["k"], "side": t["side"], "h": h,
                             "rev": rev, "ctrl": cre})
    r = pd.DataFrame(rows)
    if r.empty:
        print("no touches found")
        return

    print(f"\nVWAP BAND REVERSION — {symbol}, {sessions} sessions, "
          f"midnight-anchored (VWAPX), bar close\n")
    print("POSITIVE = price reverted toward VWAP after touching the band.")
    print("'edge' = touch minus a random bar in the same session/window.\n")
    print(f"{'band':>6} {'side':>6} {'horizon':>8} {'n':>4} {'rev pts':>9} "
          f"{'ctrl pts':>9} {'edge pts':>9} {'edge $':>9} {'win%':>6}")
    for (k, side, h), g2 in r.groupby(["k", "side", "h"]):
        if len(g2) < 5:
            continue
        edge = g2["rev"].mean() - g2["ctrl"].mean()
        print(f"{k:>6.0f} {'short' if side < 0 else 'long':>6} {h:>7}m "
              f"{len(g2):>4} {g2['rev'].mean():>9.2f} {g2['ctrl'].mean():>9.2f} "
              f"{edge:>9.2f} {edge * PV:>9.0f} {(g2['rev'] > 0).mean() * 100:>5.1f}%")

    print("\nPOOLED BY BAND (both sides, all horizons):")
    for k, g2 in r.groupby("k"):
        edge = g2["rev"].mean() - g2["ctrl"].mean()
        se = g2["rev"].std(ddof=1) / max(1, np.sqrt(len(g2)))
        t = (g2["rev"].mean() - g2["ctrl"].mean()) / se if se else 0.0
        print(f"  {k:.0f} sigma: n={len(g2):>4}  edge {edge:>+7.2f} pts "
              f"(${edge * PV:>+7.0f})  t={t:>5.2f}")
    print("\n~35 sessions, one instrument, one regime. This can rule a band OUT.")
    print("It cannot establish an edge.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ES")
