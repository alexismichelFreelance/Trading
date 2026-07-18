"""
es_metrics.py — performance metrics + overfitting guards. Pure numpy/pandas.

Design goals
------------
* Everything works on per-TRADE point PnL (the natural unit here) and can
  aggregate to daily for annualised ratios.
* Includes the guards that matter most given only ~21-30 days of data:
    - Deflated Sharpe Ratio (Bailey & Lopez de Prado): discounts a Sharpe for
      the number of strategy variants tried (multiple-testing) and for
      non-normal returns. This is the antidote to "I tried 50 things and one
      looked great."
    - Bootstrap CIs on expectancy / Sharpe.
    - Walk-forward and purged-KFold split helpers for out-of-sample testing.
* No scipy: normal CDF via math.erf, inverse-normal via Acklam's algorithm.
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ── normal CDF / inverse CDF (no scipy) ──────────────────────────────────────
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, ~1e-9 accurate)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ── trade-level stats ────────────────────────────────────────────────────────
def trade_stats(pnl_pts) -> dict:
    p = np.asarray(pnl_pts, dtype=float)
    p = p[~np.isnan(p)]
    n = len(p)
    if n == 0:
        return {"n": 0}
    wins = p[p > 0]; losses = p[p < 0]
    gross_win = wins.sum(); gross_loss = -losses.sum()
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "avg_pt": p.mean(),
        "median_pt": np.median(p),
        "total_pt": p.sum(),
        "avg_win": wins.mean() if len(wins) else 0.0,
        "avg_loss": losses.mean() if len(losses) else 0.0,
        "payoff": (wins.mean() / -losses.mean()) if len(wins) and len(losses) else np.nan,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else np.inf,
        "expectancy_pt": p.mean(),
        "expectancy_usd": p.mean() * 50.0,
        "std_pt": p.std(ddof=1) if n > 1 else 0.0,
        "sharpe_per_trade": (p.mean() / p.std(ddof=1)) if n > 1 and p.std(ddof=1) > 0 else 0.0,
    }


def max_drawdown(equity) -> dict:
    eq = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    i = int(np.argmin(dd))
    return {"max_dd_pt": float(-dd.min()),
            "max_dd_usd": float(-dd.min()) * 50.0,
            "trough_idx": i}


def daily_returns_from_trades(timestamps, pnl_pts) -> pd.Series:
    """Aggregate per-trade point PnL into daily totals (UTC date)."""
    s = pd.Series(np.asarray(pnl_pts, dtype=float),
                  index=pd.to_datetime(timestamps, utc=True))
    return s.groupby(s.index.date).sum()


def sharpe_annual(daily_pnl_pts, rf=0.0) -> float:
    d = np.asarray(daily_pnl_pts, dtype=float)
    if len(d) < 2 or d.std(ddof=1) == 0:
        return 0.0
    return (d.mean() - rf) / d.std(ddof=1) * math.sqrt(TRADING_DAYS)


def sortino_annual(daily_pnl_pts, rf=0.0) -> float:
    d = np.asarray(daily_pnl_pts, dtype=float)
    downside = d[d < 0]
    dd = downside.std(ddof=1) if len(downside) > 1 else 0.0
    if dd == 0:
        return 0.0
    return (d.mean() - rf) / dd * math.sqrt(TRADING_DAYS)


def deflated_sharpe(returns, n_trials: int, sr_variance_across_trials: float | None = None) -> dict:
    """
    Deflated Sharpe Ratio. `returns` is the per-observation PnL series of the
    SELECTED strategy. n_trials = how many variants were tried (the search space).
    sr_variance_across_trials: variance of the (non-annualised) SR estimates
    across all trials; if None, a conservative proxy 1/T is used.

    Returns the probability that the true SR > 0 after accounting for selection.
    DSR > 0.95 is the usual "survives multiple testing" bar.
    """
    r = np.asarray(returns, dtype=float); r = r[~np.isnan(r)]
    T = len(r)
    if T < 10 or r.std(ddof=1) == 0:
        return {"sr": 0.0, "dsr": 0.0, "sr0": 0.0, "T": T}
    sr = r.mean() / r.std(ddof=1)                      # per-observation SR
    # skew / kurtosis (Fisher, excess) of returns
    z = (r - r.mean()) / r.std(ddof=0)
    skew = np.mean(z**3)
    kurt = np.mean(z**4)                                # raw kurtosis (3 for normal)
    V = sr_variance_across_trials if sr_variance_across_trials is not None else 1.0 / T
    V = max(V, 1e-12)
    gamma = 0.5772156649015329                          # Euler-Mascheroni
    N = max(int(n_trials), 1)
    # expected maximum SR under the null across N independent trials
    e1 = norm_ppf(1 - 1.0 / N)
    e2 = norm_ppf(1 - 1.0 / (N * math.e))
    sr0 = math.sqrt(V) * ((1 - gamma) * e1 + gamma * e2)
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4.0 * sr * sr, 1e-9))
    dsr = norm_cdf((sr - sr0) * math.sqrt(T - 1) / denom)
    return {"sr": sr, "sr0": sr0, "dsr": dsr, "skew": skew, "kurt": kurt, "T": T}


def bootstrap_ci(pnl_pts, stat="mean", n_boot=5000, alpha=0.05, seed=0) -> tuple:
    p = np.asarray(pnl_pts, dtype=float); p = p[~np.isnan(p)]
    if len(p) < 5:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(p), size=(n_boot, len(p)))
    samp = p[idx]
    if stat == "mean":
        vals = samp.mean(axis=1)
    elif stat == "sharpe":
        m = samp.mean(axis=1); s = samp.std(axis=1, ddof=1)
        vals = np.where(s > 0, m / s, 0.0)
    elif stat == "profit_factor":
        gw = np.where(samp > 0, samp, 0).sum(axis=1)
        gl = -np.where(samp < 0, samp, 0).sum(axis=1)
        vals = np.where(gl > 0, gw / gl, np.inf)
    else:
        raise ValueError(stat)
    return (float(np.quantile(vals, alpha/2)), float(np.quantile(vals, 1-alpha/2)))


def summarize(timestamps, pnl_pts, n_trials: int = 1, cost_label="net") -> dict:
    ts = trade_stats(pnl_pts)
    if ts["n"] == 0:
        return ts
    eq = np.cumsum(np.asarray(pnl_pts, dtype=float))
    dd = max_drawdown(eq)
    daily = daily_returns_from_trades(timestamps, pnl_pts)
    shp = sharpe_annual(daily.values)
    srt = sortino_annual(daily.values)
    dsr = deflated_sharpe(pnl_pts, n_trials=n_trials)
    ci = bootstrap_ci(pnl_pts, "mean")
    mar = (ts["total_pt"] / dd["max_dd_pt"]) if dd["max_dd_pt"] > 0 else np.inf
    out = {**ts, **dd, "cost": cost_label,
           "sharpe_annual": shp, "sortino_annual": srt,
           "mar_total_over_maxdd": mar,
           "deflated_sharpe": dsr["dsr"], "n_trials": n_trials,
           "expectancy_ci95": ci, "n_days": int(len(daily))}
    return out


def pretty(summary: dict) -> str:
    if summary.get("n", 0) == 0:
        return "  (no trades)"
    s = summary
    return (f"  trades={s['n']}  days={s.get('n_days','?')}  win={100*s['win_rate']:.1f}%  "
            f"PF={s['profit_factor']:.2f}  exp={s['expectancy_pt']:+.3f}pt "
            f"(${s['expectancy_usd']:+.1f})  CI95={tuple(round(x,3) for x in s['expectancy_ci95'])}\n"
            f"  total={s['total_pt']:+.1f}pt  maxDD={s['max_dd_pt']:.1f}pt  "
            f"MAR={s['mar_total_over_maxdd']:.2f}  Sharpe={s['sharpe_annual']:.2f}  "
            f"Sortino={s['sortino_annual']:.2f}  DSR={s['deflated_sharpe']:.3f} "
            f"(trials={s['n_trials']})")


# ── split helpers for out-of-sample testing ─────────────────────────────────
def walk_forward_splits(n, n_folds=5, min_train=0.4):
    """Yield (train_idx, test_idx) expanding-window splits over range(n)."""
    start_test = int(n * min_train)
    fold = (n - start_test) // n_folds
    if fold <= 0:
        return
    for k in range(n_folds):
        a = start_test + k * fold
        b = n if k == n_folds - 1 else start_test + (k + 1) * fold
        yield np.arange(0, a), np.arange(a, b)


def purged_kfold(n, k=5, embargo=0):
    """K-fold with a purge+embargo gap to prevent leakage across the test block."""
    idx = np.arange(n)
    fold = n // k
    for i in range(k):
        a = i * fold
        b = n if i == k - 1 else (i + 1) * fold
        test = idx[a:b]
        lo = max(0, a - embargo); hi = min(n, b + embargo)
        train = np.concatenate([idx[:lo], idx[hi:]])
        yield train, test


if __name__ == "__main__":
    print("self-test in test_framework.py")
