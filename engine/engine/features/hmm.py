"""Frozen 2-state Gaussian HMM regime gate.

Fit OFFLINE on ESM5 1h Kaufman-efficiency (Baum-Welch), then FROZEN and applied
CAUSALLY as an online forward-filter (filtered posterior; state = argmax). The
filter resets at the start of each ET session; the "trend" state is the
higher-efficiency one. Params live in config/hmm_es_1h.json — no fitting at
runtime, no look-ahead.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

_SQRT2PI = np.sqrt(2.0 * np.pi)


def _gauss(x: float, mu: float, var: float) -> float:
    return float(np.exp(-0.5 * (x - mu) ** 2 / var) / (_SQRT2PI * np.sqrt(var)))


def fit_gaussian_hmm(obs, n_iter: int = 35, seed_means=(0.3, 0.9), var_floor: float = 1e-6):
    """Scaled Baum-Welch for a 2-state scalar-Gaussian HMM. Returns a dict of
    frozen params (pi, A, means, vars, trend_state).

    `var_floor` is the per-state variance floor (like hmmlearn's min_covar): a
    tiny floor lets the trend state collapse to a near-delta at ER=1.0 (the
    documented chop 0.44 / trend 1.0 means, but a non-sticky, rarely-active trend
    regime); a larger floor broadens the trend state so it stays engaged through
    a trending session. Tuned against Gate B parity."""
    x = np.asarray(obs, dtype=float)
    x = x[~np.isnan(x)]
    T, K = len(x), 2
    mu = np.array(seed_means, dtype=float)
    var = np.array([max(x.var(), var_floor)] * K)
    A = np.array([[0.9, 0.1], [0.1, 0.9]])
    pi = np.array([0.5, 0.5])

    for _ in range(n_iter):
        B = np.empty((T, K))
        for k in range(K):
            B[:, k] = np.exp(-0.5 * (x - mu[k]) ** 2 / var[k]) / (_SQRT2PI * np.sqrt(var[k]))
        B = np.clip(B, 1e-300, None)
        # forward (scaled)
        alpha = np.zeros((T, K))
        c = np.zeros(T)
        alpha[0] = pi * B[0]
        c[0] = alpha[0].sum()
        alpha[0] /= c[0]
        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ A) * B[t]
            c[t] = alpha[t].sum()
            alpha[t] /= c[t]
        # backward (scaled)
        beta = np.zeros((T, K))
        beta[-1] = 1.0
        for t in range(T - 2, -1, -1):
            beta[t] = (A @ (B[t + 1] * beta[t + 1])) / c[t + 1]
        gamma = alpha * beta
        gamma /= gamma.sum(axis=1, keepdims=True)
        # transitions
        xi_sum = np.zeros((K, K))
        for t in range(T - 1):
            num = alpha[t][:, None] * A * (B[t + 1] * beta[t + 1])[None, :]
            xi_sum += num / num.sum()
        # M-step
        pi = gamma[0]
        A = xi_sum / xi_sum.sum(axis=1, keepdims=True)
        for k in range(K):
            w = gamma[:, k]
            mu[k] = (w * x).sum() / w.sum()
            var[k] = max((w * (x - mu[k]) ** 2).sum() / w.sum(), var_floor)

    trend_state = int(np.argmax(mu))
    return {
        "pi": pi.tolist(), "A": A.tolist(), "means": mu.tolist(),
        "vars": var.tolist(), "trend_state": trend_state, "n_iter": n_iter,
    }


class GaussianHMM2:
    """Frozen params + causal online forward-filter (per-session reset)."""

    def __init__(self, pi, A, means, vars, trend_state: int, lookback: int = 3) -> None:
        self.pi = np.asarray(pi, float)
        self.A = np.asarray(A, float)
        self.means = np.asarray(means, float)
        self.vars = np.asarray(vars, float)
        self.trend_state = int(trend_state)
        self.lookback = lookback
        self._alpha: np.ndarray | None = None
        self._day: str | None = None
        self.state: int | None = None

    @classmethod
    def load(cls, path: str | Path) -> "GaussianHMM2":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(d["pi"], d["A"], d["means"], d["vars"], d["trend_state"],
                   d.get("lookback", 3))

    def _emit(self, er: float) -> np.ndarray:
        return np.array([_gauss(er, self.means[k], self.vars[k]) for k in range(2)])

    def update(self, er: float, day_key: str) -> int:
        """Feed one hourly ER observation; returns the filtered state (0/1)."""
        if day_key != self._day:           # forward-filter resets each session
            self._day = day_key
            self._alpha = None
        b = np.clip(self._emit(er), 1e-300, None)
        a = (b * self.pi) if self._alpha is None else (self._alpha @ self.A) * b
        a = a / a.sum()
        self._alpha = a
        self.state = int(np.argmax(a))
        return self.state

    def is_trend(self) -> bool:
        return self.state is not None and self.state == self.trend_state


__all__ = ["fit_gaussian_hmm", "GaussianHMM2"]
