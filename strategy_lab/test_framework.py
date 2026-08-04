"""Unit tests for es_costs + es_metrics on synthetic data with known answers."""
import numpy as np
import es_costs as C
import es_metrics as M

def approx(a, b, tol=1e-6): return abs(a - b) <= tol
ok = 0; bad = 0
def check(name, cond):
    global ok, bad
    print(("  PASS " if cond else "  FAIL ") + name); ok += cond; bad += (not cond)

print("── cost model ──")
check("DEFAULT round-turn in (0.4,0.7) pt", 0.4 < C.DEFAULT.round_turn_points() < 0.7)
check("PASSIVE_BOTH cheapest", C.PASSIVE_BOTH.round_turn_points() < C.AGGRESSIVE.round_turn_points())
print(f"    presets: passive_both={C.PASSIVE_BOTH.round_turn_points():.3f} "
      f"default={C.DEFAULT.round_turn_points():.3f} aggr={C.AGGRESSIVE.round_turn_points():.3f} pt")

print("── normal cdf/ppf ──")
check("cdf(0)=0.5", approx(M.norm_cdf(0), 0.5, 1e-9))
check("ppf(0.975)~1.95996", approx(M.norm_ppf(0.975), 1.959963985, 1e-4))
check("ppf(cdf(1.3))~1.3", approx(M.norm_ppf(M.norm_cdf(1.3)), 1.3, 1e-4))

print("── trade stats ──")
pnl = np.array([2.0, -1.0, 3.0, -1.0, -1.0])   # 3 wins? no: wins=[2,3], losses=[-1,-1,-1]
ts = M.trade_stats(pnl)
check("n=5", ts["n"] == 5)
check("win_rate=0.4", approx(ts["win_rate"], 0.4))
check("profit_factor=5/3", approx(ts["profit_factor"], 5/3, 1e-9))
check("payoff=2.5/1", approx(ts["payoff"], 2.5, 1e-9))
check("expectancy=0.4", approx(ts["expectancy_pt"], 0.4, 1e-9))

print("── drawdown ──")
eq = np.cumsum(np.array([1.0, 1, -3, 1, 1]))   # 1,2,-1,0,1 -> peak 2, trough -1 -> dd=3
dd = M.max_drawdown(eq)
check("max_dd=3", approx(dd["max_dd_pt"], 3.0, 1e-9))

print("── sharpe annualisation ──")
rng = np.random.default_rng(42)
daily = rng.normal(0.5, 2.0, 2520)             # mean .5 std 2 -> SR/day .25 -> annual ~3.97
sh = M.sharpe_annual(daily)
check("annual sharpe ~ 0.25*sqrt(252)", approx(sh, 0.25*np.sqrt(252), 0.9))
print(f"    sharpe_annual={sh:.2f} (expected ~{0.25*np.sqrt(252):.2f})")

print("── deflated sharpe: selection penalty ──")
# strong genuine edge, single trial -> high DSR
good = rng.normal(0.3, 1.0, 1000)
d1 = M.deflated_sharpe(good, n_trials=1)["dsr"]
d100 = M.deflated_sharpe(good, n_trials=100)["dsr"]
check("DSR high for real edge, 1 trial", d1 > 0.95)
check("DSR decreases with more trials", d100 < d1)
# pure noise selected from many trials -> DSR should be low
noise = rng.normal(0.06, 1.0, 1000)            # tiny apparent SR
dn = M.deflated_sharpe(noise, n_trials=200)["dsr"]
check("DSR low for marginal edge under heavy selection", dn < 0.9)
print(f"    real-edge DSR: 1 trial={d1:.3f}, 100 trials={d100:.3f} | "
      f"marginal/200 trials={dn:.3f}")

print("── end-to-end summarize (net of cost) ──")
# simulate 400 trades, small gross edge, apply DEFAULT cost
ts_idx = np.array(np.datetime64('2025-02-18') +
                  np.sort(rng.integers(0, 21, 400)).astype('timedelta64[D]'))
gross = rng.normal(0.9, 3.0, 400)              # +0.9 pt gross edge
net = C.net_points(gross, C.DEFAULT)
summ = M.summarize(ts_idx, net, n_trials=10, cost_label="net@DEFAULT")
print(M.pretty(summ))
check("expectancy reduced by ~cost", approx(summ["expectancy_pt"],
       gross.mean() - C.DEFAULT.round_turn_points(), 1e-9))
check("bootstrap CI brackets expectancy",
      summ["expectancy_ci95"][0] <= summ["expectancy_pt"] <= summ["expectancy_ci95"][1])

print(f"\n{'='*50}\n  {ok} passed, {bad} failed\n{'='*50}")
