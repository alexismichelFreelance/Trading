"""Synthetic unit tests for es_indicators.py — verify each indicator is correct."""
import numpy as np, pandas as pd
import es_indicators as I

ok = 0
bad = 0
def check(name, cond):
    global ok, bad
    if cond:
        ok += 1; print("  PASS " + name)
    else:
        bad += 1; print("  FAIL " + name)

def bars(prices, vols=None, deltas=None, session="2025-04-01"):
    prices = np.asarray(prices, float)
    n = len(prices)
    o = np.r_[prices[0], prices[:-1]]
    return pd.DataFrame({
        "ts": pd.date_range("2025-04-01 13:30", periods=n, freq="min", tz="UTC"),
        "open": o,
        "high": np.maximum(o, prices) + 0.25,
        "low": np.minimum(o, prices) - 0.25,
        "close": prices,
        "vol": (np.ones(n) if vols is None else np.asarray(vols, float)),
        "delta": (np.zeros(n) if deltas is None else np.asarray(deltas, float)),
        "session": session,
    })

print("-- order_flow_imbalance --")
df = bars([100, 101, 102], vols=[10, 10, 10], deltas=[10, -10, 5])
ofi = I.order_flow_imbalance(df, 1)
check("delta=vol -> +1", abs(ofi.iloc[0] - 1.0) < 1e-9)
check("delta=-vol -> -1", abs(ofi.iloc[1] + 1.0) < 1e-9)

print("-- session_vwap_bands --")
df = bars(np.linspace(100, 110, 30), vols=np.ones(30))
v = I.session_vwap_bands(df)
check("vwap within price range", 100 < v["sess_vwap"].iloc[-1] < 110)
check("sigma > 0 mid-session", v["sess_sigma"].iloc[-1] > 0)
check("z>0 when price above vwap", v["vwap_z"].iloc[-1] > 0)
check("bands ordered p1<p2<p3", v["vwap_p1"].iloc[-1] < v["vwap_p2"].iloc[-1] < v["vwap_p3"].iloc[-1])

print("-- efficiency_ratio --")
ramp = pd.Series(np.arange(100, 200, 1.0))
chop = pd.Series(100 + np.tile([0, 1.0], 50))
check("pure trend ER ~ 1", I.efficiency_ratio(ramp, 20).iloc[-1] > 0.99)
check("chop ER ~ 0", I.efficiency_ratio(chop, 20).iloc[-1] < 0.1)

print("-- daily_pivots --")
p = I.daily_pivots(110, 90, 105)
check("PP correct", abs(p["PP"] - 101.66666667) < 1e-6)
check("R1 = 2PP - L", abs(p["R1"] - (2 * p["PP"] - 90)) < 1e-9)
check("PDH/PDL", p["PDH"] == 110 and p["PDL"] == 90)

print("-- confluence_count --")
lv = {"R1": 5025.3, "PP": 5000.1, "S1": 4975.0}
conf = I.confluence_count(lv, tol=1.5, round_step=25.0, lo=4950, hi=5050)
check("R1 confluent with round 5025", conf["R1"] >= 2)
check("PP confluent with round 5000", conf["PP"] >= 2)

print("-- vwap_rebound_signals --")
z = np.r_[np.zeros(5), np.linspace(0, 2.2, 8), np.linspace(2.2, 0.1, 8), [0.1, 0.1]]
deltas = np.zeros(len(z))
deltas[-3:] = 50.0
d = bars(100 + z, deltas=deltas)
d["vwap_z"] = z
sig = I.vwap_rebound_signals(d, impulse_sigma=1.5, pullback_z=0.4, lookback=15, confirm_delta=True)
check("emits a long rebound after impulse+pullback", bool((sig == 1).any()))
check("no short signals here", not bool((sig == -1).any()))

print("-- detect_sd_zones --")
o = [100, 105.0, 110.0]
c = [105, 105.4, 116.0]   # mid candle: body 0.4, tiny wicks (<50% body), small range = balance
d = pd.DataFrame({"ts": pd.date_range("2025-04-01", periods=3, freq="min"),
                  "open": o, "close": c,
                  "high": [105.1, 105.43, 116.1], "low": [99.9, 104.97, 109.9],
                  "vol": [100, 20, 100], "delta": [0, 0, 0]})
zones = I.detect_sd_zones(d, impulse_body_mult=0.5, wick_body_max=0.5, body_window=3)
check("detects an up (demand) zone", len(zones) >= 1 and zones["direction"].iloc[0] == 1)

print("=" * 46)
print("  " + str(ok) + " passed, " + str(bad) + " failed")
print("=" * 46)
