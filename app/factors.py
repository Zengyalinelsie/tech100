"""因子层：从 panel_data 构建分析所需的标准化因子面板。

处理三大数据卫生问题（见探查）：
1. close=0（次新股上市前）→ 视为缺失（NaN），收益率不爆炸
2. 41% 预期亏损（fy1_np_avg<=0）→ 不用 ΔF/F，改用市值标准化 ΔF/mkt_cap
3. 极端值 → winsorize 1%/99%

核心因子（FY1 + FY2 各一套）：
    rev_norm   预期修正（市值标准化）= (F_t − F_{t-1}) / mkt_cap   ← 核心 X
    ret        周收益率 = close.pct_change()                        ← 核心 Y
    exret      超额收益 = ret − 恒生科技指数收益
    disagree   分歧度 = np_std / |np_avg|
    instnum    覆盖机构数（信号质量）

用法：
    from factors import build_factor_panel
    fac = build_factor_panel()          # 返回长表 DataFrame
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "wind_history.db"


def _winsorize(s: pd.Series, lower=0.01, upper=0.99) -> pd.Series:
    """按分位截断极端值（截面）。"""
    if s.notna().sum() < 10:
        return s
    lo, hi = s.quantile(lower), s.quantile(upper)
    return s.clip(lo, hi)


def load_panel(conn=None) -> pd.DataFrame:
    own = conn is None
    if own:
        conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT * FROM panel_data ORDER BY wind_code, trade_date", conn)
    bench = pd.read_sql("SELECT * FROM benchmark", conn)
    if own:
        conn.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    # close=0 视为缺失（次新股上市前 / 停牌）
    df.loc[df["close_hkd"] <= 0, "close_hkd"] = np.nan
    df.loc[df["mkt_cap"] <= 0, "mkt_cap"] = np.nan
    return df, bench


def build_factor_panel(conn=None) -> pd.DataFrame:
    df, bench = load_panel(conn)

    # 市场收益（恒生科技指数）
    bench = bench.sort_values("trade_date")
    bench["mkt_ret"] = bench["hstech_close"].pct_change()
    mkt = bench[["trade_date", "mkt_ret"]]

    out = []
    for code, g in df.groupby("wind_code"):
        g = g.sort_values("trade_date").copy()

        # 收益率（close 已含 NaN，pct_change 自动跳过缺口）
        g["ret"] = g["close_hkd"].pct_change()

        for fy, avg_col, std_col, inst_col in [
            ("fy1", "fy1_np_avg", "fy1_np_std", "fy1_instnum"),
            ("fy2", "fy2_np_avg", "fy2_np_std", "fy2_instnum"),
        ]:
            # 预期修正（市值标准化，不受盈亏符号影响）
            delta = g[avg_col].diff()
            g[f"{fy}_rev_norm"] = delta / g["mkt_cap"]
            # 分歧度（用 |avg| 避免负号问题）
            g[f"{fy}_disagree"] = g[std_col] / g[avg_col].abs().replace(0, np.nan)
            g[f"{fy}_instnum"] = g[inst_col]

        out.append(g)

    fac = pd.concat(out, ignore_index=True)

    # 超额收益 = 个股收益 − 市场收益
    fac = fac.merge(mkt, on="trade_date", how="left")
    fac["exret"] = fac["ret"] - fac["mkt_ret"]

    # 截面 winsorize（按周分组截断极端值）
    wins_cols = ["ret", "exret", "fy1_rev_norm", "fy2_rev_norm",
                 "fy1_disagree", "fy2_disagree"]
    for col in wins_cols:
        fac[col] = fac.groupby("trade_date")[col].transform(_winsorize)

    return fac


def attach_industry(fac: pd.DataFrame, conn=None) -> pd.DataFrame:
    """合并行业（用于行业切片分析）。"""
    own = conn is None
    if own:
        conn = sqlite3.connect(DB_PATH)
    static = pd.read_sql("SELECT wind_code, name, industry_l1, industry_l2 FROM static_info", conn)
    if own:
        conn.close()
    return fac.merge(static, on="wind_code", how="left")


if __name__ == "__main__":
    fac = build_factor_panel()
    print(f"因子面板: {len(fac):,} 行 × {fac.shape[1]} 列")
    print(f"股票数: {fac['wind_code'].nunique()} | 周数: {fac['trade_date'].nunique()}")
    print("\n关键因子非空率：")
    for c in ["ret", "exret", "fy1_rev_norm", "fy2_rev_norm", "fy1_disagree", "fy1_instnum"]:
        print(f"  {c:16s}: {fac[c].notna().mean()*100:5.1f}%")
    print("\n因子分布（winsorize 后）：")
    print(fac[["ret", "exret", "fy1_rev_norm", "fy2_rev_norm", "fy1_disagree"]].describe().round(4).to_string())
