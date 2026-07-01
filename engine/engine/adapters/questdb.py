"""Thin QuestDB HTTP client.

QuestDB exposes /exec (JSON) and /exp (CSV) at http://localhost:9000. Pushing
aggregation into SQL and pulling compact results is the project convention. Both
sync (tools/tests) and async (engine) helpers are provided.
"""
from __future__ import annotations

import httpx
import pandas as pd

DEFAULT_URL = "http://localhost:9000"


def _to_df(payload: dict) -> pd.DataFrame:
    cols = [c["name"] for c in payload["columns"]]
    df = pd.DataFrame(payload.get("dataset", []), columns=cols)
    # parse any TIMESTAMP columns to tz-aware UTC at NANOSECOND resolution.
    # (QuestDB is microsecond-precision; pandas would otherwise keep us, and the
    # engine's internal clock is epoch-ns — astype('int64') must yield ns.)
    for c in payload["columns"]:
        if c["type"] == "TIMESTAMP" and c["name"] in df.columns:
            df[c["name"]] = pd.to_datetime(df[c["name"]], utc=True).dt.as_unit("ns")
    return df


class QuestDB:
    """Synchronous client (used by tools and tests)."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def query(self, sql: str) -> dict:
        r = httpx.get(f"{self.url}/exec", params={"query": sql}, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"QuestDB error: {j['error']} | query={sql[:200]}")
        return j

    def df(self, sql: str) -> pd.DataFrame:
        return _to_df(self.query(sql))


class AsyncQuestDB:
    """Async client (used by the engine / live recorder)."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    async def query(self, sql: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.url}/exec", params={"query": sql})
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(f"QuestDB error: {j['error']} | query={sql[:200]}")
            return j

    async def df(self, sql: str) -> pd.DataFrame:
        return _to_df(await self.query(sql))


__all__ = ["QuestDB", "AsyncQuestDB", "DEFAULT_URL"]
