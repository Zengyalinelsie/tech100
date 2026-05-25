"""策略层：把"预期修正"因子变成可交易的多空组合 + 回测 + 选股清单。

设计要点：
- 信号 X = fy1_rev_norm / fy2_rev_norm（市值标准化的预期修正，来自 factors.py）
- 每 hold_weeks 周非重叠换仓：t 时点信号选股 → 持有 hold_weeks 周 → 用此后周收益结算
- 点位正确：信号在 t 已知，收益在 t+1..t+hold 实现，无前视
- 三条线：多头 / 空头（空头簿损益）/ 多空价差（多−空，资金中性）+ 恒生科技基准
- 交易成本：每次换仓按换手率扣 cost_bps（双边）

用法：
    from strategy import backtest, latest_signal
    res = backtest(x_col="fy1_rev_norm", hold_weeks=1)   # res["nav"], res["metrics"], res["trades"]
    buy, sell = latest_signal(x_col="fy1_rev_norm")
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from factors import build_factor_panel, attach_industry

WEEKS_PER_YEAR = 52


def _valid(fac: pd.DataFrame, x_col: str) -> pd.DataFrame:
    """只保留信号、收盘价、市值齐全、且有机构覆盖的可交易行。"""
    inst_col = "fy1_instnum" if x_col.startswith("fy1") else "fy2_instnum"
    m = (
        fac[x_col].notna()
        & fac["close_hkd"].notna()
        & fac["mkt_cap"].notna()
        & (fac.get(inst_col, 1) > 0)
    )
    return fac[m]


def select(fac: pd.DataFrame, x_col: str, date, top_pct: float = 0.2):
    """某周横截面：信号最强 top_pct = 多头，最弱 top_pct = 空头（等权）。"""
    date = pd.Timestamp(date)
    cs = _valid(fac, x_col)
    cs = cs[cs["trade_date"] == date].copy()
    if cs.empty:
        return {"date": date, "long": [], "short": []}
    cs = cs.sort_values(x_col, ascending=False)
    n = max(1, int(round(len(cs) * top_pct)))
    longs = cs.head(n)["wind_code"].tolist()
    shorts = cs.tail(n)["wind_code"].tolist()
    return {"date": date, "long": longs, "short": shorts, "n": n, "pool": len(cs)}


def _bench_returns(conn=None) -> pd.Series:
    import sqlite3
    from factors import DB_PATH
    own = conn is None
    if own:
        conn = sqlite3.connect(DB_PATH)
    bench = pd.read_sql("SELECT * FROM benchmark", conn)
    if own:
        conn.close()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench = bench.sort_values("trade_date")
    s = bench.set_index("trade_date")["hstech_close"].pct_change()
    s.name = "bench_ret"
    return s


def _max_drawdown(nav: pd.Series) -> float:
    peak = nav.cummax()
    return float((nav / peak - 1).min())


def _metrics(weekly: pd.Series, nav: pd.Series) -> dict:
    weekly = weekly.dropna()
    if len(weekly) < 2:
        return {k: np.nan for k in
                ["total_ret", "cagr", "ann_vol", "sharpe", "max_dd", "win_rate"]}
    n = len(weekly)
    total = float(nav.iloc[-1] - 1)
    cagr = float(nav.iloc[-1] ** (WEEKS_PER_YEAR / n) - 1)
    vol = float(weekly.std(ddof=1) * np.sqrt(WEEKS_PER_YEAR))
    mean_a = float(weekly.mean() * WEEKS_PER_YEAR)
    sharpe = mean_a / vol if vol > 0 else np.nan
    return {
        "total_ret": total,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe": sharpe,
        "max_dd": _max_drawdown(nav),
        "win_rate": float((weekly > 0).mean()),
    }


def backtest(
    fac: pd.DataFrame | None = None,
    x_col: str = "fy1_rev_norm",
    hold_weeks: int = 1,
    top_pct: float = 0.2,
    cost_bps: float = 30.0,
    start: str | None = None,
) -> dict:
    """非重叠 hold_weeks 周换仓的多空回测。返回 nav / weekly / metrics / trades / ic。"""
    if fac is None:
        fac = build_factor_panel()
    fac = fac.copy()
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    if start:
        fac = fac[fac["trade_date"] >= pd.Timestamp(start)]

    dates = sorted(fac["trade_date"].unique())
    ret_lookup = fac.set_index(["trade_date", "wind_code"])["ret"]
    cost = cost_bps / 1e4

    rows, trades, ics = [], [], []
    prev_long, prev_short = set(), set()

    i = 0
    while i < len(dates) - 1:
        t = dates[i]
        sel = select(fac, x_col, t, top_pct)
        longs, shorts = sel["long"], sel["short"]
        if not longs or not shorts:
            i += 1
            continue

        # IC：信号(t) vs 前向 1 周收益(t+1)，全池 Spearman
        nxt = dates[i + 1]
        sig_cs = _valid(fac, x_col)
        sig_cs = sig_cs[sig_cs["trade_date"] == t].set_index("wind_code")[x_col]
        fwd = ret_lookup.reindex([(nxt, c) for c in sig_cs.index])
        fwd.index = sig_cs.index
        pair = pd.concat([sig_cs, fwd], axis=1, keys=["sig", "fwd"]).dropna()
        if len(pair) > 5:
            ics.append({"date": t, "ic": pair["sig"].corr(pair["fwd"], method="spearman")})

        # 换手成本（与上次篮子比较，单边换手率）
        turn_l = len(set(longs) - prev_long) / len(longs) if prev_long else 1.0
        turn_s = len(set(shorts) - prev_short) / len(shorts) if prev_short else 1.0
        prev_long, prev_short = set(longs), set(shorts)

        trades.append({"trade_date": t, "side": "LONG", "codes": ",".join(longs)})
        trades.append({"trade_date": t, "side": "SHORT", "codes": ",".join(shorts)})

        # 持有窗口内逐周结算（篮子固定，等权）
        for step in range(1, hold_weeks + 1):
            j = i + step
            if j >= len(dates):
                break
            d = dates[j]
            lr = ret_lookup.reindex([(d, c) for c in longs]).mean()
            sr = ret_lookup.reindex([(d, c) for c in shorts]).mean()
            lr = 0.0 if pd.isna(lr) else float(lr)
            sr = 0.0 if pd.isna(sr) else float(sr)
            # 仅换仓周（step==1）扣成本
            c_l = cost * turn_l if step == 1 else 0.0
            c_s = cost * turn_s if step == 1 else 0.0
            long_ret = lr - c_l
            short_ret = -sr - c_s                 # 空头簿：标的跌 → 赚
            ls_ret = (lr - sr) - (c_l + c_s)      # 多空价差
            rows.append({"trade_date": d, "long_ret": long_ret,
                         "short_ret": short_ret, "ls_ret": ls_ret,
                         "bench_basis": sr})
        i += hold_weeks

    wk = pd.DataFrame(rows).set_index("trade_date").sort_index()
    bench = _bench_returns().reindex(wk.index)
    wk["bench_ret"] = bench

    nav = pd.DataFrame({
        "多头": (1 + wk["long_ret"]).cumprod(),
        "空头": (1 + wk["short_ret"]).cumprod(),
        "多空": (1 + wk["ls_ret"]).cumprod(),
        "恒生科技": (1 + wk["bench_ret"].fillna(0)).cumprod(),
    })

    metrics = pd.DataFrame({
        "多头": _metrics(wk["long_ret"], nav["多头"]),
        "空头": _metrics(wk["short_ret"], nav["空头"]),
        "多空": _metrics(wk["ls_ret"], nav["多空"]),
        "恒生科技": _metrics(wk["bench_ret"], nav["恒生科技"]),
    }).T

    ic_df = pd.DataFrame(ics)
    ic_summary = {}
    if not ic_df.empty:
        ic_summary = {
            "ic_mean": float(ic_df["ic"].mean()),
            "ic_ir": float(ic_df["ic"].mean() / ic_df["ic"].std(ddof=1))
            if ic_df["ic"].std(ddof=1) > 0 else np.nan,
            "ic_pos_rate": float((ic_df["ic"] > 0).mean()),
        }

    return {
        "nav": nav, "weekly": wk, "metrics": metrics,
        "trades": pd.DataFrame(trades), "ic": ic_df, "ic_summary": ic_summary,
        "params": {"x_col": x_col, "hold_weeks": hold_weeks,
                   "top_pct": top_pct, "cost_bps": cost_bps},
    }


def latest_signal(fac: pd.DataFrame | None = None, x_col: str = "fy1_rev_norm",
                  top_pct: float = 0.2) -> pd.DataFrame:
    """最新一周的买入(LONG)/卖出(SHORT)清单，含名称、信号值、参考收盘价。"""
    if fac is None:
        fac = build_factor_panel()
    if "name" not in fac.columns:
        fac = attach_industry(fac)
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    last = fac["trade_date"].max()
    sel = select(fac, x_col, last, top_pct)
    cs = fac[fac["trade_date"] == last].set_index("wind_code")

    out = []
    weight = 1.0 / sel.get("n", 1)
    for code in sel["long"]:
        r = cs.loc[code]
        out.append([last.date(), code, r["name"], "LONG", round(weight, 4),
                    round(float(r[x_col]), 8), round(float(r["close_hkd"]), 3)])
    for code in sel["short"]:
        r = cs.loc[code]
        out.append([last.date(), code, r["name"], "SHORT", round(weight, 4),
                    round(float(r[x_col]), 8), round(float(r["close_hkd"]), 3)])
    return pd.DataFrame(out, columns=["trade_date", "wind_code", "name", "side",
                                      "target_weight", "signal", "ref_close_hkd"])


if __name__ == "__main__":
    res = backtest(x_col="fy1_rev_norm", hold_weeks=1)
    print("=== 指标 ===")
    print(res["metrics"].round(3).to_string())
    print("\n=== IC ===", res["ic_summary"])
    print("\n=== NAV 末值 ===")
    print(res["nav"].iloc[-1].round(3).to_string())
    print("\n=== 本周选股(前6行) ===")
    print(latest_signal(x_col="fy1_rev_norm").head(6).to_string(index=False))
