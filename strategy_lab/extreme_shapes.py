"""A shape catalogue of session extremes -- tops and bottoms, kept apart.

Type A (vertical push, no conviction at the extreme) is ONE mechanism and it
covers 16% of 2025 tops. The rest end for other reasons: a V that reverses on
the spot, a range that breaks and immediately fails, a pullback that pushes to a
marginal new extreme and dies there, a slow grind that simply stops. Pooling
them is what produced flat table after flat table.

So this measures SHAPE and names nothing in advance. Each extreme gets features
describing how price arrived, how it behaved AT the level, and how it left. The
types are then read off the catalogue rather than assumed.

  APPROACH
    v_in       points per minute over the last 10 minutes into the extreme
    v_in_30    the same over 30 minutes -- a fast v_in with a slow v_in_30 is a
               final acceleration; both fast is one continuous run
    pre_rng    range of the 30 minutes BEFORE the final push, i.e. what the
               market was doing before it went. Small = it broke out of a
               coil; large = it was already swinging
    coil       push_10 / pre_rng. High means the last leg dwarfed the base
    marginal   how far the extreme exceeded the highest high (lowest low) of
               the preceding hour. Near zero = a marginal new extreme, the
               classic failed retest; large = genuine new ground

  AT THE LEVEL
    conv       |net delta| / volume on the extreme minute -- churn or conviction
    dwell      minutes in the +/-20 window spent within 15% of the day's range
               of the extreme. High = the level was worked, low = a spike

  DEPARTURE
    v_out      points per minute over the 10 minutes out
    give30     the move away over 30 minutes -- the outcome, signed so that
               negative always means the extreme REVERSED, for tops and bottoms

Bottoms are computed with every sign mirrored and are NEVER averaged together
with tops: the whole point is that they may not share parameters.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd

SRC = r"D:\Trading\strategy_lab\mbo_minutes.csv"
OUT = r"D:\Trading\strategy_lab\extreme_shapes.csv"


def main():
    m = pd.read_csv(SRC)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)].copy()
    m["delta"] = m.buy_v - m.sell_v
    rows = []
    for day, d in m.groupby("day", sort=True):
        d = d.sort_values("mod").reset_index(drop=True)
        if len(d) < 250:
            continue
        rng = float(d.px.max() - d.px.min())
        if rng <= 0:
            continue
        med_v = float(d.vol.median())
        for kind, i in (("TOP", int(d.hi.values.argmax())),
                        ("BOTTOM", int(d.lo.values.argmin()))):
            if i < 45 or i > len(d) - 35:
                continue
            s = 1.0 if kind == "TOP" else -1.0        # mirror everything
            px = d.px.values * s
            e = d.iloc[i]
            v_in = (px[i] - px[i - 10]) / 10.0
            v_in30 = (px[i] - px[i - 30]) / 30.0
            pre = px[i - 40:i - 10]
            pre_rng = float(pre.max() - pre.min()) if len(pre) else np.nan
            push10 = px[i] - px[i - 10]
            prior = px[max(0, i - 70):i - 10]
            marginal = px[i] - float(prior.max()) if len(prior) else np.nan
            v_out = (px[i + 10] - px[i]) / 10.0
            give30 = px[min(i + 30, len(px) - 1)] - px[i]
            near = np.abs(px[max(0, i - 20):i + 21] - px[i]) <= 0.15 * rng
            rows.append(dict(
                day=day, sym=d.symbol.iloc[0], kind=kind, at=int(e["mod"]),
                rng=round(rng, 2),
                v_in=round(v_in, 2), v_in30=round(v_in30, 2),
                pre_rng=round(pre_rng, 2),
                coil=round(push10 / pre_rng, 2) if pre_rng and pre_rng > 0.5 else None,
                marginal=round(marginal, 2),
                conv=round(abs(float(e.delta)) / max(float(e.vol), 1.0), 3),
                v_hi=round(float(e.vol) / med_v, 2) if med_v else None,
                dwell=int(near.sum()),
                v_out=round(v_out, 2), give30=round(give30, 2)))
    t = pd.DataFrame(rows)
    t.to_csv(OUT, index=False)
    pd.set_option("display.width", 240)
    for kind in ("TOP", "BOTTOM"):
        k = t[t.kind == kind].sort_values("give30")
        print("\n" + "=" * 118)
        print("%s  --  %d extremes, sorted by outcome (give30 negative = reversed)"
              % (kind, len(k)))
        print("=" * 118)
        print(k.drop(columns=["kind"]).to_string(index=False))
    print("\n-> " + OUT)


if __name__ == "__main__":
    main()
