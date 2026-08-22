"""A CATALOGUE of session highs, one row each. Not a statistic.

METHODOLOGY, deliberately different from everything before it. Previous passes
pooled thousands of minutes and looked for an average gradient across deciles.
That answers "do all tops share one signature", whose answer is obviously no --
if absorption tops are 15% of tops and the rest end for unrelated reasons,
pooling is guaranteed to return flat. It did.

This describes each top INDIVIDUALLY so distinct mechanisms can be recognised
and one of them characterised precisely. Nothing is averaged across tops here.

Per session high, from the MBO stream at prices within `BAND` ticks of the high:

  APPROACH (the 10 minutes into the high)
    buy_v/sell_v   aggressive volume by side
    push           points gained per 1,000 contracts of net buying -- how much
                   the buyers had to pay for the last leg
    ask_add        size ADDED to the offer near the high: supply arriving
    ask_cxl        size CANCELLED from the offer: supply leaving
    refresh        ask_add / traded-at-ask. Above 1 means the offer was being
                   REPLENISHED as fast as it was eaten -- somebody standing
                   there. This is the absorption fingerprint and it cannot be
                   seen without order-level data.

  AT THE HIGH (the high minute itself)
    v_hi           volume, vs the session's median minute
    conv           |net delta| / volume: conviction, or churn if near zero

  AFTER (the 10 minutes out)
    bid_cxl        bid size cancelled: the book withdrawing rather than selling
    drop           points fallen

Read the rows, do not average them.
"""
import sys
import time

sys.path.insert(0, r"D:\Trading\engine")
import pandas as pd
from engine.adapters.questdb import QuestDB

SRC = r"D:\Trading\strategy_lab\mbo_minutes.csv"
OUT = r"D:\Trading\strategy_lab\top_catalogue.csv"
BAND = 1.0        # points either side of the high price
PRE, POST = 10, 10

Q = """select
  sum(case when action='T' and side='B' then size else 0 end) buy_v,
  sum(case when action='T' and side='A' then size else 0 end) sell_v,
  sum(case when action='A' and side='A' then size else 0 end) ask_add,
  sum(case when action='C' and side='A' then size else 0 end) ask_cxl,
  sum(case when action='A' and side='B' then size else 0 end) bid_add,
  sum(case when action='C' and side='B' then size else 0 end) bid_cxl
from mbo_events
where symbol='{sym}' and ts_recv >= '{a}' and ts_recv < '{b}'
  and price >= {lo} and price <= {hi}"""


def main():
    m = pd.read_csv(SRC)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)]
    q = QuestDB(timeout=900)
    rows = []
    days = sorted(m.day.unique())
    for i, day in enumerate(days):
        d = m[m.day == day].sort_values("mod")
        if len(d) < 200:
            continue
        sym = d.symbol.iloc[0]
        j = int(d.hi.values.argmax())
        top = d.iloc[j]
        hi_px = float(top.hi)
        t = pd.Timestamp(top.ts_recv)
        med_v = float(d.vol.median())
        pre = d[(d["mod"] >= top["mod"] - PRE) & (d["mod"] < top["mod"])]
        post = d[(d["mod"] > top["mod"]) & (d["mod"] <= top["mod"] + POST)]
        if len(pre) < 5 or len(post) < 5:
            continue
        lo_p, hi_p = hi_px - BAND, hi_px + BAND

        def book(a, b):
            try:
                return q.df(Q.format(sym=sym, a=a.isoformat(), b=b.isoformat(),
                                     lo=lo_p, hi=hi_p)).iloc[0]
            except Exception:                       # noqa: BLE001
                return None

        bp = book(t - pd.Timedelta(minutes=PRE), t + pd.Timedelta(minutes=1))
        ap = book(t + pd.Timedelta(minutes=1), t + pd.Timedelta(minutes=POST))
        if bp is None or ap is None:
            continue
        net = float(pre.buy_v.sum() - pre.sell_v.sum())
        gain = float(top.px - pre.px.iloc[0])
        traded_ask = float(bp.buy_v)          # buys lift the offer
        rows.append(dict(
            day=day, sym=sym, hi=hi_px, at=str(t)[11:16],
            gain10=round(gain, 2),
            push=round(gain / (net / 1000.0), 1) if abs(net) > 100 else None,
            v_hi=round(float(top.vol) / med_v, 2) if med_v else None,
            conv=round(abs(float(top.buy_v - top.sell_v)) / max(float(top.vol), 1), 3),
            ask_add=int(bp.ask_add), ask_cxl=int(bp.ask_cxl),
            refresh=round(bp.ask_add / traded_ask, 2) if traded_ask > 50 else None,
            bid_cxl_after=int(ap.bid_cxl), ask_add_after=int(ap.ask_add),
            drop10=round(float(post.px.iloc[-1] - top.px), 2)))
        if i % 10 == 0:
            print("  ...%d/%d" % (i, len(days)), flush=True)
    t = pd.DataFrame(rows)
    t.to_csv(OUT, index=False)
    print("\n%d tops catalogued -> %s\n" % (len(t), OUT))
    pd.set_option("display.width", 200)
    print(t.to_string(index=False))


if __name__ == "__main__":
    main()
