"""Gate A — the online feature engine must reproduce the precomputed
claude_sec_feat columns EXACTLY (str/adelta/avol/pxc), with dir matching on
every non-zero-adelta row. Proves the online recomputation is causally faithful.
"""
import asyncio

import numpy as np
import pytest

from engine.adapters.feeds.replay_questdb import ReplayFeed
from engine.adapters.questdb import AsyncQuestDB, QuestDB
from engine.core.events import BookFlow, Trade
from engine.features.online import IgnitionFeatures


def _db_up() -> bool:
    try:
        QuestDB(timeout=5).query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="QuestDB not reachable at localhost:9000")


def _run_features(table: str, day: str):
    feed = ReplayFeed(AsyncQuestDB(), table=table, days=[day])
    fe = IgnitionFeatures()
    out = []

    async def go():
        async for e in feed.stream():
            if isinstance(e, Trade):
                fe.add_trade(e)
            elif isinstance(e, BookFlow):
                fe.close_second(e)
                out.append((fe.ts, fe.adelta, fe.avol, fe.strength, fe.dir, fe.pxc))

    asyncio.run(go())
    return out


@pytest.mark.parametrize("table,day", [
    ("claude_sec_feat", "2025-04-01"),
    ("claude_sec_feat", "2025-04-02"),
    ("claude_sec_feat", "2025-05-15"),
    ("claude_sec_feat_esh5", "2025-02-18"),
])
def test_feature_parity_exact(table, day):
    q = QuestDB()
    tbl = q.df(f"SELECT ts, pxc, adelta, avol, str, dir FROM {table} "
               f"WHERE day='{day}T00:00:00.000000Z' ORDER BY ts")
    feats = _run_features(table, day)
    assert len(feats) == len(tbl), (len(feats), len(tbl))

    ts_f = np.array([f[0] for f in feats], dtype="int64")
    ts_t = tbl["ts"].astype("int64").to_numpy()
    assert np.array_equal(ts_f, ts_t), "timestamp alignment"

    ad_f = np.array([f[1] for f in feats], dtype="int64")
    ad_t = tbl["adelta"].astype("int64").to_numpy()
    assert np.array_equal(ad_f, ad_t), "adelta"

    av_f = np.array([f[2] for f in feats], dtype="int64")
    av_t = tbl["avol"].astype("int64").to_numpy()
    assert np.array_equal(av_f, av_t), "avol"

    # str: exact where the table has a value; identical null pattern
    str_f = np.array([np.nan if f[3] is None else f[3] for f in feats], dtype=float)
    str_t = tbl["str"].astype(float).to_numpy()
    assert np.array_equal(np.isnan(str_f), np.isnan(str_t)), "str null pattern"
    m = ~np.isnan(str_t)
    assert np.allclose(str_f[m], str_t[m], atol=1e-9, rtol=0), \
        f"str max err {np.abs(str_f[m] - str_t[m]).max():.2e}"

    # dir: exact on every non-zero-adelta row (ignition only fires there)
    dir_f = np.array([f[4] for f in feats], dtype="int64")
    dir_t = tbl["dir"].astype("int64").to_numpy()
    nz = ad_t != 0
    assert np.array_equal(dir_f[nz], dir_t[nz]), "dir on nonzero adelta"

    # pxc: exact on every traded second (avol>0)
    px_f = np.array([np.nan if f[5] is None else f[5] for f in feats], dtype=float)
    px_t = tbl["pxc"].astype(float).to_numpy()
    traded = av_t > 0
    assert np.allclose(px_f[traded], px_t[traded], atol=1e-9, rtol=0), "pxc on traded seconds"
