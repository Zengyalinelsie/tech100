"""策略层：把"预期修正"因子变成可交易的多空组合 + 回测 + 选股清单。

设计要点：
- 信号 X = fy1_rev_norm / fy2_rev_norm（市值标准化的预期修正，来自 factors.py）
- 每 hold_weeks 周非重叠换仓：t 时点信号选股 → 持有 hold_weeks 周 → 用此后周收益结算
- 点位正确：信号在 t 已知，收益在 t+1..t+hold 实现，无前视
- 三条线：多头 / 空头（空头簿损益）/ 多空价差（多−空，资金中性）+ 恒生科技基准
- 交易成本：每次换仓按换手率扣 cost_bps（双边）

借鉴：
- 股池清洗（可交易 + 流动性 + 退市保险栓）：可交易/流动性用本仓 amount/close；退市读
  config/universe_status.csv（来自 INF stock_lifecycle，见 scripts/build_universe.py）
- n_drop 换手控制：思路借自 Qlib TopkDropoutStrategy（只换最弱 n_drop 只，降换手降成本）
- 诊断：分层 IC / 年化 ICIR / 信息比率 IR（借自 Qlib evaluate / eva.alpha）

用法：
    from strategy import backtest, latest_signal
    res = backtest(x_col="fy1_rev_norm", hold_weeks=1, n_drop=3)
    buy_sell = latest_signal(x_col="fy1_rev_norm")
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from factors import build_factor_panel, attach_industry, FACTORS, _zscore

WEEKS_PER_YEAR = 52
UNIVERSE_STATUS = Path(__file__).parent.parent / "config" / "universe_status.csv"
LIQ_PCT = 0.10        # 流动性下限：剔除滚动 ADTV 最低的 10%
ADTV_WIN = 12         # 滚动 ADTV 窗口（周）


def _load_delist() -> dict:
    if UNIVERSE_STATUS.exists():
        u = pd.read_csv(UNIVERSE_STATUS)
        d = pd.to_datetime(u["delist_date"], errors="coerce")
        return dict(zip(u["wind_code"], d))
    return {}


def _prepare(fac: pd.DataFrame, liq_pct: float = LIQ_PCT) -> pd.DataFrame:
    """给面板加可交易/流动性/退市标记（一次性，供后续按日筛选）。"""
    if "eligible_base" in fac.columns:
        return fac
    fac = fac.sort_values(["wind_code", "trade_date"]).copy()
    fac["adtv"] = fac.groupby("wind_code")["amount"].transform(
        lambda s: s.rolling(ADTV_WIN, min_periods=4).mean())
    thr = fac.groupby("trade_date")["adtv"].transform(lambda s: s.quantile(liq_pct))
    liquid = fac["adtv"] >= thr
    tradable = (fac["close_hkd"] > 0) & (fac["amount"] > 0)
    delist = _load_delist()
    dl = fac["wind_code"].map(delist)
    not_delisted = dl.isna() | (fac["trade_date"] < dl)
    fac["eligible_base"] = tradable & liquid.fillna(False) & not_delisted
    return fac


def _eligible(fac: pd.DataFrame, x_col: str) -> pd.DataFrame:
    """可交易 + 流动 + 未退市 + 信号/市值齐全；预期类因子额外要求有分析师覆盖。"""
    fac = _prepare(fac)
    m = fac["eligible_base"] & fac[x_col].notna() & fac["mkt_cap"].notna()
    meta = FACTORS.get(x_col)
    if meta is not None and meta.needs_coverage:
        inst_col = "fy2_instnum" if x_col.startswith("fy2") else "fy1_instnum"
        m = m & (fac[inst_col].fillna(0) > 0)
    return fac[m]


def _topk(fac: pd.DataFrame, top_pct: float) -> int:
    return max(1, int(round(top_pct * fac["wind_code"].nunique())))


def _dropout(scores: pd.Series, prev: list, topk: int, n_drop: int) -> list:
    """Qlib 风格 dropout 选 topk：保留上期、只丢合并池最弱 n_drop 只、买最强新票补回。

    scores 越大越想要（做空时传 -signal）。prev 为空时直接取 topk（首期全建仓）。
    """
    scores = scores.dropna().sort_values(ascending=False)
    if scores.empty:
        return []
    ranked = list(scores.index)
    last = [c for c in prev if c in scores.index]          # 上期仍合格
    last = list(scores.reindex(last).sort_values(ascending=False).index)
    if not last:
        return ranked[:topk]
    need = max(0, n_drop + topk - len(last))
    today = [c for c in ranked if c not in set(last)][:need]
    comb = list(scores.reindex(last + today).sort_values(ascending=False).index)
    bottom = set(comb[-n_drop:]) if n_drop > 0 else set()
    sell = [c for c in last if c in bottom]
    buy = today[: len(sell) + topk - len(last)]
    new = [c for c in last if c not in set(sell)] + buy
    return new[:topk]


def select(fac: pd.DataFrame, x_col: str, date, top_pct: float = 0.2):
    """某周横截面：信号最强 topk = 多头，最弱 topk = 空头（等权，已做股池清洗）。"""
    date = pd.Timestamp(date)
    fac = _prepare(fac)
    cs = _eligible(fac, x_col)
    topk = _topk(fac, top_pct)
    cs = cs[cs["trade_date"] == date].copy()
    if cs.empty:
        return {"date": date, "long": [], "short": [], "n": topk, "pool": 0}
    cs = cs.sort_values(x_col, ascending=False)
    longs = cs.head(topk)["wind_code"].tolist()
    shorts = cs.tail(topk)["wind_code"].tolist()
    return {"date": date, "long": longs, "short": shorts, "n": topk, "pool": len(cs)}


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


def _metrics(weekly: pd.Series, nav: pd.Series, bench: pd.Series | None = None) -> dict:
    weekly = weekly.dropna()
    keys = ["total_ret", "cagr", "ann_vol", "sharpe", "max_dd", "win_rate", "ir"]
    if len(weekly) < 2:
        return {k: np.nan for k in keys}
    n = len(weekly)
    vol = float(weekly.std(ddof=1) * np.sqrt(WEEKS_PER_YEAR))
    mean_a = float(weekly.mean() * WEEKS_PER_YEAR)
    ir = np.nan
    if bench is not None:
        ex = (weekly - bench.reindex(weekly.index)).dropna()
        te = ex.std(ddof=1)
        ir = float(ex.mean() / te * np.sqrt(WEEKS_PER_YEAR)) if te > 0 else np.nan
    return {
        "total_ret": float(nav.iloc[-1] - 1),
        "cagr": float(nav.iloc[-1] ** (WEEKS_PER_YEAR / n) - 1),
        "ann_vol": vol,
        "sharpe": mean_a / vol if vol > 0 else np.nan,
        "max_dd": _max_drawdown(nav),
        "win_rate": float((weekly > 0).mean()),
        "ir": ir,
    }


def _rolling_ic(fac: pd.DataFrame, x_col: str, horizons=(1, 2, 4)) -> dict:
    """分层 IC：信号 vs 前向 h 周累计收益的截面 Spearman，按日平均。

    返回 {"by_h": {h: mean_ic}, "series1": 每日 h=1 的 IC Series}。
    """
    elig = _eligible(fac, x_col)
    sig = elig.pivot_table(index="trade_date", columns="wind_code", values=x_col)
    close = fac.pivot_table(index="trade_date", columns="wind_code", values="close_hkd")
    by_h, series1 = {}, pd.Series(dtype=float)
    for h in horizons:
        fwd = close.shift(-h) / close - 1
        recs = {}
        for dt in sig.index:
            if dt not in fwd.index:
                continue
            pair = pd.concat([sig.loc[dt], fwd.loc[dt]], axis=1).dropna()
            if len(pair) > 5:
                recs[dt] = pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman")
        s = pd.Series(recs)
        by_h[h] = float(s.mean()) if len(s) else np.nan
        if h == 1:
            series1 = s
    return {"by_h": by_h, "series1": series1}


def subperiod_ic(series1: pd.Series) -> dict:
    """把逐日 IC 序列按日历年分桶求均值,并做 70/30 时序样本内外二分。

    返回 {"by_year": {2021: ic, ...}, "is_oos": {"is": ic, "oos": ic}}。判断信号
    跨区间是否稳定 —— 单一区间表现好但分年/样本外塌掉,就是过拟合信号。
    """
    if series1 is None or len(series1) < 2:
        return {"by_year": {}, "is_oos": {}}
    s = series1.dropna().copy()
    s.index = pd.to_datetime(s.index)
    by_year = {int(y): float(g.mean()) for y, g in s.groupby(s.index.year)}
    k = int(len(s) * 0.7)
    is_oos = ({"is": float(s.iloc[:k].mean()), "oos": float(s.iloc[k:].mean())}
              if 1 <= k < len(s) else {})
    return {"by_year": by_year, "is_oos": is_oos}


def deflated_sharpe(sharpe_annual: float, n_obs: int, n_trials: int,
                    skew: float = 0.0, kurt: float = 3.0,
                    periods_per_year: int = WEEKS_PER_YEAR) -> float:
    """Deflated Sharpe Ratio(Bailey & López de Prado）。

    把"试过 n_trials 个因子/配置"的多重检验偏差扣掉,返回真实 Sharpe>0 的概率 ∈[0,1]。
    n_trials 越大、样本越短、偏度越负/尾部越厚 → DSR 越低。<0.95 应警惕过拟合。
    """
    if n_obs < 3 or not np.isfinite(sharpe_annual):
        return float("nan")
    sr = sharpe_annual / np.sqrt(periods_per_year)          # 每期 Sharpe
    var_sr = (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2) / (n_obs - 1)
    if var_sr <= 0:
        return float("nan")
    sd_sr = np.sqrt(var_sr)
    N = max(1, int(n_trials))
    emc = 0.5772156649015329                                 # Euler-Mascheroni
    if N > 1:
        sr0 = sd_sr * ((1 - emc) * norm.ppf(1 - 1.0 / N)
                       + emc * norm.ppf(1 - 1.0 / (N * np.e)))
    else:
        sr0 = 0.0
    return float(norm.cdf((sr - sr0) / sd_sr))


def backtest(
    fac: pd.DataFrame | None = None,
    x_col: str = "fy1_rev_norm",
    hold_weeks: int = 1,
    top_pct: float = 0.2,
    cost_bps: float = 30.0,
    n_drop: int = 3,
    start: str | None = None,
    n_trials: int = 1,
) -> dict:
    """非重叠 hold_weeks 周换仓的多空回测（带股池清洗 + n_drop 降换手）。"""
    if fac is None:
        fac = build_factor_panel()
    fac = fac.copy()
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    if start:
        fac = fac[fac["trade_date"] >= pd.Timestamp(start)]
    fac = _prepare(fac)

    dates = sorted(fac["trade_date"].unique())
    ret_lookup = fac.set_index(["trade_date", "wind_code"])["ret"]
    topk = _topk(fac, top_pct)
    cost = cost_bps / 1e4

    # 每日合格信号（含清洗），供 dropout 选股
    elig = _eligible(fac, x_col)
    sig_by_date = {d: g.set_index("wind_code")[x_col]
                   for d, g in elig.groupby("trade_date")}

    rows, trades, turns_l, turns_s = [], [], [], []
    prev_long, prev_short = [], []

    i = 0
    while i < len(dates) - 1:
        t = dates[i]
        scores = sig_by_date.get(t)
        if scores is None or scores.empty:
            i += 1
            continue
        longs = _dropout(scores, prev_long, topk, n_drop)
        shorts = _dropout(-scores, prev_short, topk, n_drop)
        if not longs or not shorts:
            i += 1
            continue

        turn_l = len(set(longs) - set(prev_long)) / len(longs)
        turn_s = len(set(shorts) - set(prev_short)) / len(shorts)
        turns_l.append(turn_l)
        turns_s.append(turn_s)
        prev_long, prev_short = longs, shorts

        trades.append({"trade_date": t, "side": "LONG", "codes": ",".join(longs)})
        trades.append({"trade_date": t, "side": "SHORT", "codes": ",".join(shorts)})

        for step in range(1, hold_weeks + 1):
            j = i + step
            if j >= len(dates):
                break
            d = dates[j]
            lr = ret_lookup.reindex([(d, c) for c in longs]).mean()
            sr = ret_lookup.reindex([(d, c) for c in shorts]).mean()
            lr = 0.0 if pd.isna(lr) else float(lr)
            sr = 0.0 if pd.isna(sr) else float(sr)
            c_l = cost * turn_l if step == 1 else 0.0
            c_s = cost * turn_s if step == 1 else 0.0
            rows.append({"trade_date": d,
                         "long_ret": lr - c_l,
                         "short_ret": -sr - c_s,
                         "ls_ret": (lr - sr) - (c_l + c_s)})
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
        "多头": _metrics(wk["long_ret"], nav["多头"], bench),
        "空头": _metrics(wk["short_ret"], nav["空头"], bench),
        "多空": _metrics(wk["ls_ret"], nav["多空"], bench),
        "恒生科技": _metrics(wk["bench_ret"], nav["恒生科技"], None),
    }).T

    # 诊断：分层 IC + 年化 ICIR
    ric = _rolling_ic(fac, x_col)
    s1 = ric["series1"]
    ic_summary = {}
    if len(s1) > 1:
        std = s1.std(ddof=1)
        ls_ret = wk["ls_ret"].dropna()
        ic_summary = {
            "ic_mean": float(s1.mean()),
            "ic_ir": float(s1.mean() / std * np.sqrt(WEEKS_PER_YEAR)) if std > 0 else np.nan,
            "ic_pos_rate": float((s1 > 0).mean()),
            "rolling_ic": {int(h): v for h, v in ric["by_h"].items()},
            "turnover_long": float(np.mean(turns_l)) if turns_l else np.nan,
            "turnover_short": float(np.mean(turns_s)) if turns_s else np.nan,
            "subperiod_ic": subperiod_ic(s1),
            "deflated_sharpe": deflated_sharpe(
                float(metrics.loc["多空", "sharpe"]), len(ls_ret), n_trials,
                skew=float(ls_ret.skew()), kurt=float(ls_ret.kurtosis() + 3.0)),
            "n_trials": int(n_trials),
        }

    return {
        "nav": nav, "weekly": wk, "metrics": metrics,
        "trades": pd.DataFrame(trades), "ic": s1.rename_axis("date").reset_index(name="ic"),
        "ic_summary": ic_summary,
        "params": {"x_col": x_col, "hold_weeks": hold_weeks, "top_pct": top_pct,
                   "cost_bps": cost_bps, "n_drop": n_drop, "topk": topk},
    }


def latest_signal(fac: pd.DataFrame | None = None, x_col: str = "fy1_rev_norm",
                  top_pct: float = 0.2) -> pd.DataFrame:
    """最新一周的买入(LONG)/卖出(SHORT)目标清单（已做股池清洗）。"""
    if fac is None:
        fac = build_factor_panel()
    if "name" not in fac.columns:
        fac = attach_industry(fac)
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    fac = _prepare(fac)
    last = fac["trade_date"].max()
    sel = select(fac, x_col, last, top_pct)
    cs = fac[fac["trade_date"] == last].set_index("wind_code")

    out = []
    weight = 1.0 / sel.get("n", 1)
    for side, codes in [("LONG", sel["long"]), ("SHORT", sel["short"])]:
        for code in codes:
            r = cs.loc[code]
            out.append([last.date(), code, r["name"], side, round(weight, 4),
                        round(float(r[x_col]), 8), round(float(r["close_hkd"]), 3)])
    return pd.DataFrame(out, columns=["trade_date", "wind_code", "name", "side",
                                      "target_weight", "signal", "ref_close_hkd"])


def _parse_neutralize(neutralize) -> set:
    """把 neutralize 参数归一成 {'industry','size'} 子集。接受 None / 'industry' /
    'size' / 'industry+size' / 可迭代。无法识别的项忽略。"""
    if not neutralize:
        return set()
    toks = neutralize.replace(",", "+").split("+") if isinstance(neutralize, str) else list(neutralize)
    return {t.strip() for t in toks if t.strip() in ("industry", "size")}


def _neutralize_cross_section(fac: pd.DataFrame, controls: set) -> pd.Series:
    """逐交易日把 composite 对控制变量做 OLS,取残差再截面标准化(剔除净行业/市值暴露)。

    controls ⊆ {'industry','size'}。行业用 industry_l1 哑变量,市值用 log(mkt_cap)。
    缺控制变量或截面样本不足的行/截面原样保留,不参与回归。
    """
    use_ind = "industry" in controls and "industry_l1" in fac.columns and fac["industry_l1"].notna().any()
    use_size = "size" in controls and "mkt_cap" in fac.columns
    if not (use_ind or use_size):
        return fac["composite"]

    resid = fac["composite"].copy()
    for _, sub in fac.groupby("trade_date"):
        m = sub["composite"].notna()
        if use_size:
            m = m & sub["mkt_cap"].notna() & (sub["mkt_cap"] > 0)
        if use_ind:
            m = m & sub["industry_l1"].notna()
        if m.sum() < 5:
            continue
        parts = [np.ones((int(m.sum()), 1))]
        if use_size:
            parts.append(np.log(sub.loc[m, "mkt_cap"].to_numpy(float)).reshape(-1, 1))
        if use_ind:
            dummies = pd.get_dummies(sub.loc[m, "industry_l1"], drop_first=True)
            if dummies.shape[1]:
                parts.append(dummies.to_numpy(float))
        X = np.hstack(parts)
        y = sub.loc[m, "composite"].to_numpy(float)
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid.loc[sub.index[m]] = y - X @ beta

    # 残差再做截面 z,使尺度与未中性化版本可比
    return fac.assign(_resid=resid).groupby("trade_date")["_resid"].transform(_zscore)


def add_composite(fac: pd.DataFrame, weights: dict, neutralize=None):
    """按权重把多个因子合成 composite 列：逐周截面 z 分 → 加权平均（按行非空因子）。

    weights: {因子列名: 权重}。只用权重≠0 且存在于 fac 的因子；缺失因子(未采)自动忽略。
    neutralize: None(默认,不中性化) / 'industry' / 'size' / 'industry+size'。开启后对
        合成总分逐截面做 OLS 残差化,剔除净行业/市值暴露,逼出真 alpha。
    返回 (fac_with_composite, used_cols)。composite 已是"高=看多"，可直接喂 backtest。
    """
    fac = fac.copy()
    cols = [c for c, w in weights.items() if w and c in fac.columns and fac[c].notna().any()]
    if not cols:
        fac["composite"] = np.nan
        return fac, []
    zmat = np.column_stack([
        fac.groupby("trade_date")[c].transform(_zscore).to_numpy(dtype=float) for c in cols
    ])
    w = np.array([float(weights[c]) for c in cols])
    present = ~np.isnan(zmat)
    den = (present * np.abs(w)).sum(axis=1)
    num = np.nansum(zmat * w, axis=1)
    comp = np.full(len(den), np.nan)
    m = den > 0
    comp[m] = num[m] / den[m]
    fac["composite"] = comp

    controls = _parse_neutralize(neutralize)
    if controls:
        fac["composite"] = _neutralize_cross_section(fac, controls)
    return fac, cols


def screen_table(fac: pd.DataFrame, weights: dict, date=None, neutralize=None) -> pd.DataFrame:
    """分析师风格选股表：某日按 composite 排序，含 PE/远期PE/预期增速/各因子值/总分。"""
    if "name" not in fac.columns:
        fac = attach_industry(fac)
    fac, cols = add_composite(fac, weights, neutralize=neutralize)
    fac = _prepare(fac)
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    if date is None:
        date = fac["trade_date"].max()
    date = pd.Timestamp(date)

    cs = fac[(fac["trade_date"] == date) & fac["eligible_base"] & fac["composite"].notna()].copy()
    if cs.empty:
        return pd.DataFrame()
    cs = cs.sort_values("composite", ascending=False).reset_index(drop=True)
    cs["排名"] = cs.index + 1
    cs["远期PE"] = cs["close_hkd"] / cs["fy1_eps"].where(cs["fy1_eps"] > 0)

    base = {"排名": cs["排名"], "名称": cs["name"], "代码": cs["wind_code"],
            "行业": cs.get("industry_l1"), "股价": cs["close_hkd"].round(2),
            "PE": cs["pe_ttm"].round(1), "远期PE": cs["远期PE"].round(1),
            "预期增速%": (cs["eps_growth"] * 100).round(1)}
    out = pd.DataFrame(base)
    for c in cols:                                   # 各入选因子的原始值
        out[FACTORS[c].name] = cs[c].round(4).values
    out["总分"] = cs["composite"].round(3).values
    return out


if __name__ == "__main__":
    res = backtest(x_col="fy1_rev_norm", hold_weeks=1, n_drop=3)
    print("=== 指标 ===")
    print(res["metrics"].round(3).to_string())
    print("\n=== IC / 换手 ===", {k: (round(v, 4) if isinstance(v, float) else v)
                                  for k, v in res["ic_summary"].items()})
    print("\n=== NAV 末值 ===")
    print(res["nav"].iloc[-1].round(3).to_string())
    print("\n=== 本周选股(前6行) ===")
    print(latest_signal(x_col="fy1_rev_norm").head(6).to_string(index=False))

    print("\n=== 多因子 composite 演示（E/P + 低波 + NTM 各 1 权重）===")
    fac = build_factor_panel()
    w = {"ep": 1, "low_vol_12w": 1, "ntm_rev_norm": 1}
    rc = backtest(add_composite(fac, w)[0], x_col="composite", hold_weeks=1, n_drop=3)
    print(rc["metrics"].round(3).to_string())
    print("\n选股表(前6行):")
    print(screen_table(fac, w).head(6).to_string(index=False))
