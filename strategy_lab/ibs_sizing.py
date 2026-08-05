"""IBS position sizing — from the measured tail, not from a target return.

IBSSwingStrategy runs NO STOP by design (the classic spec; stops break daily
mean reversion). That makes contract count the ONLY risk control, so it has to
be derived from the loss distribution rather than picked.

Measured on the 16-year re-derivation (strategy_lab/ibs_verify.py, 486 trades,
net of 0.517pt round-turn):

    worst single trade   -321 pt   = -$16,050 / contract
    1st-percentile trade -160 pt   = -$8,010  / contract
    max equity drawdown  -386 pt   = -$19,281 / contract
    one losing year in 16 (2018, -364pt)

PLANNING_MULT applies a margin over the worst drawdown actually observed. A
16-year sample does not contain the worst drawdown the strategy can produce, and
sizing to the observed maximum implicitly assumes it does. 1.5x is a convention,
not a measurement -- it is the one number here that is a judgement call, and it
is exposed so it can be argued with.

This prints a table. It does not choose for you: which drawdown you are willing
to carry is not a fact about the strategy.

    python ibs_sizing.py
"""
from __future__ import annotations

ES_POINT_USD = 50.0
MAX_DD_PT = 386.0            # observed, 2010-2026
WORST_TRADE_PT = 321.0
P1_TRADE_PT = 160.0
PLANNING_MULT = 1.5          # judgement, not measurement -- see module docstring

EQUITIES = (25_000, 50_000, 100_000, 250_000, 500_000)
DD_TOLERANCE = (0.05, 0.10, 0.15, 0.20)


def main() -> None:
    plan_dd = MAX_DD_PT * PLANNING_MULT * ES_POINT_USD
    print(f"\n{'='*90}")
    print("IBS SIZING — contracts such that a planned drawdown stays inside tolerance")
    print(f"{'='*90}")
    print(f"  observed max drawdown   {MAX_DD_PT:>6.0f} pt  "
          f"${MAX_DD_PT*ES_POINT_USD:>9,.0f} / contract")
    print(f"  planning drawdown x{PLANNING_MULT}  {MAX_DD_PT*PLANNING_MULT:>6.0f} pt  "
          f"${plan_dd:>9,.0f} / contract")
    print(f"  worst single trade      {WORST_TRADE_PT:>6.0f} pt  "
          f"${WORST_TRADE_PT*ES_POINT_USD:>9,.0f} / contract  (no stop — this is "
          f"taken in full)")
    print(f"\n{'account':>10}" + "".join(f"{int(d*100):>7}% dd" for d in DD_TOLERANCE))
    for eq in EQUITIES:
        row = f"{eq:>10,}"
        for d in DD_TOLERANCE:
            n = int(eq * d // plan_dd)
            row += f"{n:>9}"
        print(row)

    print(f"\n  Reading the table: the number is CONTRACTS. A 0 means the account "
          f"cannot\n  carry one ES contract of this sleeve at that tolerance — "
          f"MES (1/10 size,\n  ${ES_POINT_USD/10:.0f}/pt) is the instrument for "
          f"those rows, at 10x the count.")
    print(f"\n  Single-trade check — at N contracts the worst OBSERVED trade costs:")
    for n in (1, 2, 3, 5):
        print(f"    {n} contract{'s' if n > 1 else ' '}  "
              f"${n*WORST_TRADE_PT*ES_POINT_USD:>10,.0f}   "
              f"(1st-pctl trade ${n*P1_TRADE_PT*ES_POINT_USD:>9,.0f})")
    print(f"\n  Held overnight, 3.1 days average: this is gap-exposed capital in a\n"
          f"  different risk class from the intraday book. It must be counted "
          f"against\n  the account's gross overnight exposure, not netted "
          f"against intraday sleeves.")
    print(f"{'='*90}\n")


if __name__ == "__main__":
    main()
