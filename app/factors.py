"""因子层：从 panel_data 构建分析所需的标准化因子面板。

处理三大数据卫生问题（见探查）：
1. close=0（次新股上市前）→ 视为缺失（NaN），收益率不爆炸
2. 41% 预期亏损（fy1_np_avg<=0）→ 不用 ΔF/F，改用市值标准化 ΔF/mkt_cap
3. 极端值 → winsorize 1%/99%

★ 跨年修复：FY1/FY2 按日历年取数（s_west_netprofit(code, YEAR(date))），每年元旦
   目标年 +1，fy1_np_avg 会机械跳成上一周的 FY2 值。直接 diff() 会把这个"换标的"
   当成大幅上调（每年 1 月全市场假上调）。故换年首周的预期修正一律置 NaN。

因子库（全部统一为"值越大越看多"，注册在 FACTORS）：
    预期类   rev_norm / fy2_rev_norm / ntm_rev_norm / rev_norm_4w / rev_diffusion_8w / eps_rev_norm
    分歧覆盖 disagree_drop / instnum_chg
    价量估值 mom_12w / reversal_1w / low_vol_12w / ep
    背离     exp_price_divergence

用法：
    from factors import build_factor_panel, FACTORS
    fac = build_factor_panel()
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "wind_history.db"


# ---- 因子注册表：列名 → (中文名, 白话解释, 是否需要分析师覆盖) ----
class F:
    __slots__ = ("name", "desc", "needs_coverage")

    def __init__(self, name, desc, needs_coverage=False):
        self.name = name
        self.desc = desc
        self.needs_coverage = needs_coverage


FACTORS = {
    # 预期类（需分析师覆盖）
    "fy1_rev_norm":        F("FY1 预期修正", "本周当年净利润一致预期的上调幅度÷市值（已修跨年跳变）", True),
    "fy2_rev_norm":        F("FY2 预期修正", "本周下一年净利润预期的上调幅度÷市值（已修跨年跳变）", True),
    "ntm_rev_norm":        F("NTM 预期修正", "未来12个月口径净利预期修正÷市值，跨年平滑不跳", True),
    "rev_norm_4w":         F("FY1 修正动量", "过去4周累计的 FY1 预期修正，平滑掉大量零值噪声", True),
    "rev_diffusion_8w":    F("修正扩散度", "过去8周(上调周数−下调周数)/8，衡量修正方向的一致性", True),
    "eps_rev_norm":        F("FY1 EPS 修正", "本周 FY1 每股盈利预期修正÷股价（含股本变化，与净利口径互补）", True),
    # 分歧 & 覆盖
    "disagree_drop":       F("分歧收敛", "分析师分歧度的下降幅度，越收敛越看多", True),
    "instnum_chg":         F("覆盖增加", "覆盖该股的分析师机构数的增加，关注度上升", True),
    # 价量 & 估值
    "mom_12w":             F("12周动量", "过去12周价格涨幅，经典价格动量", False),
    "reversal_1w":         F("短期反转", "上周收益取负，买上周输家（短期反转）", False),
    "low_vol_12w":         F("低波动", "过去12周收益波动取负，越稳越看多", False),
    "ep":                  F("盈利收益率", "1/滚动PE，越便宜越高", False),
    "beta":               F("低Beta", "过去52周对恒生科技的Beta取负，越低越看多", False),
    # 背离
    "exp_price_divergence": F("预期-价格背离", "预期在升但股价还没跟上（修正动量z − 价格动量z）", True),
    # 增长（用一致预期派生）
    "fwd_ep":             F("远期盈利收益率", "FY1每股盈利÷股价（远期PE的倒数，越便宜越高）", True),
    "eps_growth":         F("预期EPS增速", "FY2/FY1 每股盈利−1，一致预期隐含的明年增速", True),
    # 估值（需新采 Wind 字段，回拉前自动留空、不影响其他因子）
    "bp":                 F("账面收益率", "1/市净率PB，越便宜越高（需采PB）", False),
    "roe_fwd":            F("预期ROE", "一致预期净资产收益率，越高越好（需采ROE）", True),
    "div_yield":          F("股息率", "每股股息÷股价，越高越好（需采股息率）", False),
    "ebitda_yield":       F("EV/EBITDA倒数", "1/(EV/EBITDA)，企业价值口径越便宜越高（需采EV/EBITDA）", False),
    "low_leverage":       F("低杠杆", "净负债率取负，越稳健越看多（需采净负债率）", False),
    "profit_alert":       F("盈警", "港股业绩预告，负面预警看空（需采盈警）", False),
}


def _winsorize(s: pd.Series, lower=0.01, upper=0.99) -> pd.Series:
    """按分位截断极端值（截面）。"""
    if s.notna().sum() < 10:
        return s
    lo, hi = s.quantile(lower), s.quantile(upper)
    return s.clip(lo, hi)


def _zscore(s: pd.Series) -> pd.Series:
    sd = s.std()
    if sd is None or sd < 1e-12 or s.notna().sum() < 5:
        return s * 0.0
    return (s - s.mean()) / sd


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
    # 新采估值原始列：未回拉时为 NULL，读出是 object/None → 强制转 numeric（全空→NaN）
    for col in ["pb", "roe_fwd", "div_yield", "ev_ebitda", "nde", "profit_alert"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
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
    df = df.merge(mkt, on="trade_date", how="left")    # 提前并入，供 beta 计算

    out = []
    for code, g in df.groupby("wind_code"):
        g = g.sort_values("trade_date").copy()

        # 换年标记：目标年（=日历年）发生变化的首周，预期修正失真，置 NaN
        year_changed = g["trade_date"].dt.year.ne(g["trade_date"].dt.year.shift(1))

        # 收益率
        g["ret"] = g["close_hkd"].pct_change()

        # ---- 预期修正（净利口径，FY1/FY2，修跨年跳变） ----
        for fy, avg_col, std_col, inst_col in [
            ("fy1", "fy1_np_avg", "fy1_np_std", "fy1_instnum"),
            ("fy2", "fy2_np_avg", "fy2_np_std", "fy2_instnum"),
        ]:
            delta = g[avg_col].diff().where(~year_changed)
            g[f"{fy}_rev_norm"] = delta / g["mkt_cap"]
            g[f"{fy}_disagree"] = g[std_col] / g[avg_col].abs().replace(0, np.nan)
            g[f"{fy}_instnum"] = g[inst_col]

        # ---- NTM 滚动口径（跨年平滑，无需边界 NaN） ----
        doy = g["trade_date"].dt.dayofyear
        days_in_year = np.where(g["trade_date"].dt.is_leap_year, 366, 365)
        w = 1.0 - (doy - 1) / days_in_year           # 年初≈1（偏FY1）→ 年底≈0（偏FY2）
        ntm_np = w * g["fy1_np_avg"] + (1 - w) * g["fy2_np_avg"]
        g["ntm_rev_norm"] = ntm_np.diff() / g["mkt_cap"]

        # ---- 预期类增强 ----
        g["rev_norm_4w"] = g["fy1_rev_norm"].rolling(4, min_periods=2).sum()
        up = (g["fy1_rev_norm"] > 0).astype(float)
        dn = (g["fy1_rev_norm"] < 0).astype(float)
        g["rev_diffusion_8w"] = (up.rolling(8, min_periods=3).sum()
                                 - dn.rolling(8, min_periods=3).sum()) / 8.0
        g["eps_rev_norm"] = (g["fy1_eps"].diff().where(~year_changed)) / g["close_hkd"]

        # ---- 分歧 & 覆盖 ----
        g["disagree_drop"] = -g["fy1_disagree"].diff()
        g["instnum_chg"] = g["fy1_instnum"].diff()

        # ---- 价量 & 估值 ----
        g["mom_12w"] = g["close_hkd"] / g["close_hkd"].shift(12) - 1
        g["reversal_1w"] = -g["ret"]
        g["low_vol_12w"] = -g["ret"].rolling(12, min_periods=6).std()
        g["ep"] = 1.0 / g["pe_ttm"].where(g["pe_ttm"] > 0)
        # 低Beta：过去52周对恒生科技的 Beta，取负（低beta=看多）
        cov = g["ret"].rolling(52, min_periods=26).cov(g["mkt_ret"])
        var = g["mkt_ret"].rolling(52, min_periods=26).var()
        g["beta"] = -(cov / var.where(var > 1e-12))

        # ---- 增长 / 远期估值（用一致预期派生） ----
        g["fwd_ep"] = g["fy1_eps"] / g["close_hkd"]
        g["eps_growth"] = g["fy2_eps"] / g["fy1_eps"].where(g["fy1_eps"] > 0) - 1

        # ---- 估值因子（需新采 Wind 原始列；缺列时跳过，回拉后自动点亮） ----
        if "pb" in g.columns:
            g["bp"] = 1.0 / g["pb"].where(g["pb"] > 0)
        if "roe_fwd" in g.columns:
            g["roe_fwd"] = g["roe_fwd"]
        if "div_yield" in g.columns:
            g["div_yield"] = g["div_yield"]
        if "ev_ebitda" in g.columns:
            g["ebitda_yield"] = 1.0 / g["ev_ebitda"].where(g["ev_ebitda"] > 0)
        if "nde" in g.columns:
            g["low_leverage"] = -g["nde"]
        if "profit_alert" in g.columns:
            g["profit_alert"] = g["profit_alert"]

        # 价格动量(4周)中间量，供背离因子
        g["_mom_4w"] = g["close_hkd"] / g["close_hkd"].shift(4) - 1

        out.append(g)

    fac = pd.concat(out, ignore_index=True)

    # 超额收益 = 个股收益 − 市场收益（mkt_ret 已在 df 阶段并入）
    fac["exret"] = fac["ret"] - fac["mkt_ret"]

    # ---- 预期-价格背离（截面 z 分：修正动量 − 价格动量） ----
    z_rev = fac.groupby("trade_date")["rev_norm_4w"].transform(_zscore)
    z_mom = fac.groupby("trade_date")["_mom_4w"].transform(_zscore)
    fac["exp_price_divergence"] = z_rev - z_mom
    fac = fac.drop(columns=["_mom_4w"])

    # 截面 winsorize（按周分组截断极端值）
    wins_cols = ["ret", "exret"] + list(FACTORS.keys())
    for col in wins_cols:
        if col in fac.columns:
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
    print("\n各因子非空率：")
    for c, meta in FACTORS.items():
        rate = fac[c].notna().mean() * 100 if c in fac.columns else float("nan")
        print(f"  {c:22s} {rate:5.1f}%  {meta.name}")
