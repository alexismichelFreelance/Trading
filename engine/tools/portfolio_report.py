"""Portfolio view: combine the four sleeves' per-trade dumps into a daily $ P&L
report — equity, drawdown, correlations, concentration, per-month, ex-April.

    python tools/portfolio_report.py <dir-with-daily_*.json> [--json out.json]

Sizing (declared, $100k account):
  ignition   1 contract  (trailing exit, causal)      cost 0.5175 pt/RT
  opendrive  1 contract                               cost 0.5175 pt/RT
  flow       0.1 x study size (max ~5 contracts)      cost 0.5175 pt/RT/contract
  zones      risk-sized 2%/$2k (engine, single-pos)   cost $5.28/contract RT
NOTE flow scaling is a linear approximation of running one-tenth size (rounding
at small size would differ slightly).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # for engine.*

PT = 50.0
COST_PT = 0.5175
SLEEVES = ("ignition", "opendrive", "flow", "zones")
SCALE = {"ignition": 1.0, "opendrive": 1.0, "flow": 0.1, "zones": 1.0}
TREND = ("ignition", "opendrive", "flow")     # gated by the dealer-gamma regime


def trade_usd(sleeve: str, t: dict) -> float:
    if sleeve == "zones":
        return t["gross_points"] * PT - 5.28 * t["contracts"]
    usd = (t["gross_points"] - COST_PT * t["contracts"]) * PT
    return usd * SCALE[sleeve]


def load(d: Path) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for s in SLEEVES:
        f = d / f"daily_{s}.json"
        daily: dict[str, float] = defaultdict(float)
        for t in json.loads(f.read_text()):
            daily[t["day"]] += trade_usd(s, t)
        out[s] = dict(daily)
    return out


def stats(x: np.ndarray) -> dict:
    eq = np.cumsum(x)
    dd = eq - np.maximum.accumulate(eq)
    pos = x[x > 0]
    tot = x.sum()
    return dict(
        total=tot, mean=x.mean(), sd=x.std(),
        tstat=x.mean() / (x.std() / np.sqrt(len(x))) if x.std() > 0 else 0.0,
        maxdd=dd.min(), worst=x.min(), best=x.max(),
        pos_days=float((x > 0).mean()),
        top5=np.sort(x)[::-1][:5].sum() / tot if tot > 0 else float("nan"),
    )


def apply_gex_gate(daily: dict, days: list) -> tuple[dict, dict]:
    """Zero the trend sleeves' P&L on non-short-gamma days (gexp_prev > 1/3).
    Trend sleeves reset flat intraday, so zeroing the day == not trading it,
    which is exactly what the live RegimeGate does (suppress entries). Returns
    (gated_daily, regime_by_day). Reproduces gamma/GEX_FINDINGS.md section D."""
    from engine.features.gamma import GammaRegime
    g = GammaRegime()
    regime = {day: g.is_short_gamma(day) for day in days}
    gated = {s: dict(v) for s, v in daily.items()}
    for s in TREND:
        for day in days:
            if regime.get(day) is False:          # KNOWN long/mid gamma -> off
                gated[s][day] = 0.0
    n_off = sum(1 for day in days if regime.get(day) is False)
    n_on = sum(1 for day in days if regime.get(day) is True)
    n_unk = len(days) - n_off - n_on
    print(f"GEX gate: {n_on} short-gamma days (trend ON), {n_off} long/mid (trend OFF), "
          f"{n_unk} unknown (allow)\n")
    return gated, regime


def main() -> None:
    d = Path(sys.argv[1])
    daily = load(d)
    days = sorted(set().union(*[set(v) for v in daily.values()]))
    gated = "--gex-gate" in sys.argv
    if gated:
        ungated_M = np.array([[daily[s].get(day, 0.0) for s in SLEEVES] for day in days])
        daily, _ = apply_gex_gate(daily, days)
    M = np.array([[daily[s].get(day, 0.0) for s in SLEEVES] for day in days])
    combo = M.sum(axis=1)
    if gated:
        u = ungated_M.sum(axis=1)
        ueq = np.cumsum(u); udd = (ueq - np.maximum.accumulate(ueq)).min()
        geq = np.cumsum(combo); gdd = (geq - np.maximum.accumulate(geq)).min()
        keep = combo.sum() / u.sum() if u.sum() else float("nan")
        print(f"portfolio  ungated ${u.sum():+,.0f} (maxDD ${udd:,.0f})  ->  "
              f"gated ${combo.sum():+,.0f} (maxDD ${gdd:,.0f})   "
              f"kept {keep:.0%} of P&L at {gdd/udd-1:+.0%} drawdown\n")

    print(f"days: {len(days)}  ({days[0]} .. {days[-1]})\n")
    print(f"{'sleeve':<10}{'total$':>10}{'mean/d':>8}{'sd':>8}{'t':>6}{'maxDD$':>9}"
          f"{'worst$':>9}{'pos%':>6}{'top5':>6}")
    for i, s in enumerate(SLEEVES):
        st = stats(M[:, i])
        print(f"{s:<10}{st['total']:>10,.0f}{st['mean']:>8,.0f}{st['sd']:>8,.0f}"
              f"{st['tstat']:>6.1f}{st['maxdd']:>9,.0f}{st['worst']:>9,.0f}"
              f"{st['pos_days']:>6.0%}{st['top5']:>6.0%}")
    st = stats(combo)
    print(f"{'PORTFOLIO':<10}{st['total']:>10,.0f}{st['mean']:>8,.0f}{st['sd']:>8,.0f}"
          f"{st['tstat']:>6.1f}{st['maxdd']:>9,.0f}{st['worst']:>9,.0f}"
          f"{st['pos_days']:>6.0%}{st['top5']:>6.0%}")
    solo_sd = sum(stats(M[:, i])["sd"] for i in range(len(SLEEVES)))
    print(f"\ndiversification: portfolio sd {st['sd']:,.0f} vs sum-of-solo sd {solo_sd:,.0f} "
          f"({st['sd']/solo_sd:.0%})")

    print("\ncorrelation matrix (daily $):")
    C = np.corrcoef(M.T)
    print("           " + "".join(f"{s[:8]:>9}" for s in SLEEVES))
    for i, s in enumerate(SLEEVES):
        print(f"{s:<10} " + "".join(f"{C[i, j]:>9.2f}" for j in range(len(SLEEVES))))

    print("\nper-month ($, by sleeve + portfolio):")
    months = sorted(set(day[:7] for day in days))
    print(f"{'month':<9}" + "".join(f"{s[:8]:>10}" for s in SLEEVES) + f"{'PORT':>10}")
    exapr = 0.0
    for m in months:
        idx = [i for i, day in enumerate(days) if day.startswith(m)]
        row = [M[idx, j].sum() for j in range(len(SLEEVES))]
        p = combo[idx].sum()
        if m != "2025-04":
            exapr += p
        print(f"{m:<9}" + "".join(f"{v:>10,.0f}" for v in row) + f"{p:>10,.0f}")
    print(f"\nportfolio ex-April: ${exapr:+,.0f}   "
          f"(April share: {(st['total']-exapr)/st['total']:.0%})")

    if "--json" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--json") + 1])
        eq = {s: np.cumsum(M[:, i]).tolist() for i, s in enumerate(SLEEVES)}
        eq["PORTFOLIO"] = np.cumsum(combo).tolist()
        out.write_text(json.dumps({"days": days, "equity": eq,
                                   "corr": C.tolist(), "sleeves": list(SLEEVES)}))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
