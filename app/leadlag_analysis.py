"""
Lead-Lag Analysis: 价格与盈利预期的时间差分析
核心问题：股价和盈利预期，谁先动？领先/滞后多久？
"""

import warnings

import numpy as np
import pandas as pd
from scipy import stats


def prepare_stock_series(weekly_df: pd.DataFrame, code: str) -> pd.DataFrame:
    """为单只股票准备干净的时间序列。"""
    sub = (
        weekly_df[weekly_df["wind_code"] == code][
            ["trade_date", "netprofit_avg", "close_hkd"]
        ]
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    # 过滤无效价格（close_hkd <= 0 视为缺失）
    sub.loc[sub["close_hkd"] <= 0, "close_hkd"] = np.nan
    # 计算预期变化 ΔF 和收益率 r
    sub["delta_f"] = sub["netprofit_avg"].diff()
    sub["return_r"] = sub["close_hkd"].pct_change()
    return sub.dropna(subset=["delta_f", "return_r"])


def cross_correlation_series(delta_f: pd.Series, ret_r: pd.Series, max_lag: int = 8):
    """
    计算两个时间序列的交叉相关系数。
    lag > 0: 预期领先股价（预期变化后，股价在未来 lag 期响应）
    lag < 0: 股价领先预期（股价变化后，预期在未来 |lag| 期响应）
    lag = 0: 同步相关
    """
    delta_f = delta_f.reset_index(drop=True)
    ret_r = ret_r.reset_index(drop=True)
    n = len(delta_f)
    if n < max_lag * 2 + 10:
        return {}

    results = {}
    for lag in range(-max_lag, max_lag + 1):
        if lag > 0:
            # 预期领先股价: corr(ΔF_t, r_{t+lag})
            x = delta_f.iloc[:-lag].values
            y = ret_r.iloc[lag:].values
        elif lag < 0:
            # 股价领先预期: corr(ΔF_{t+|lag|}, r_t)
            lag_abs = abs(lag)
            x = delta_f.iloc[lag_abs:].values
            y = ret_r.iloc[:-lag_abs].values
        else:
            x = delta_f.values
            y = ret_r.values

        # 去掉 NaN
        mask = ~(np.isnan(x) | np.isnan(y))
        x_clean, y_clean = x[mask], y[mask]
        if len(x_clean) < 10:
            continue

        # Pearson 和 Spearman
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pr, pp = stats.pearsonr(x_clean, y_clean)
            sr, sp = stats.spearmanr(x_clean, y_clean)

        results[lag] = {
            "pearson_r": pr,
            "pearson_p": pp,
            "spearman_r": sr,
            "spearman_p": sp,
            "n_obs": len(x_clean),
        }
    return results


def compute_all_cross_correlations(weekly_df: pd.DataFrame, max_lag: int = 8):
    """
    对每只股票计算交叉相关，然后截面平均。
    返回：
        agg: 截面平均结果 DataFrame
        stock_best: 每只股票的最优 lag
        raw_list: 每只股票的原始相关结果列表
    """
    codes = sorted(weekly_df["wind_code"].unique())
    raw_list = []
    stock_best = []

    for code in codes:
        sub = prepare_stock_series(weekly_df, code)
        if len(sub) < max_lag * 2 + 10:
            continue

        corr = cross_correlation_series(sub["delta_f"], sub["return_r"], max_lag)
        if not corr:
            continue

        # 记录每只股票的 Pearson 结果
        for lag, vals in corr.items():
            raw_list.append(
                {
                    "wind_code": code,
                    "lag": lag,
                    **vals,
                }
            )

        # 找每只股票的 Pearson 最大绝对值对应的 lag
        lags = sorted(corr.keys())
        pearsons = [corr[l]["pearson_r"] for l in lags]
        best_idx = int(np.argmax(np.abs(pearsons)))
        best_lag = lags[best_idx]
        stock_best.append(
            {
                "wind_code": code,
                "best_lag": best_lag,
                "best_corr": pearsons[best_idx],
                "n_weeks": len(sub),
            }
        )

    if not raw_list:
        return pd.DataFrame(), pd.DataFrame(), []

    df_raw = pd.DataFrame(raw_list)

    # 截面平均：按 lag 分组
    agg = (
        df_raw.groupby("lag")
        .agg(
            mean_pearson=("pearson_r", "mean"),
            std_pearson=("pearson_r", "std"),
            mean_spearman=("spearman_r", "mean"),
            std_spearman=("spearman_r", "std"),
            n_stocks=("wind_code", "nunique"),
        )
        .reset_index()
    )

    # T检验：每个 lag 下，相关系数是否显著不为0
    ttests = []
    for lag, grp in df_raw.groupby("lag"):
        pearsons = grp["pearson_r"].dropna().values
        if len(pearsons) > 3:
            tstat, tpval = stats.ttest_1samp(pearsons, 0)
            ttests.append(
                {
                    "lag": lag,
                    "t_stat": tstat,
                    "t_pval": tpval,
                }
            )
    ttest_df = pd.DataFrame(ttests)
    if not ttest_df.empty:
        agg = agg.merge(ttest_df, on="lag", how="left")

    stock_best_df = pd.DataFrame(stock_best)
    return agg, stock_best_df, df_raw


def bidirectional_prediction_regression(weekly_df: pd.DataFrame, forward_weeks=(1, 2, 4)):
    """
    双向预测回归：
    方向A: 预期变化 ΔF_t 预测未来股价收益 r_{t+k}
    方向B: 股价收益 r_t 预测未来预期变化 ΔF_{t+k}
    """
    results_a = []
    results_b = []
    codes = sorted(weekly_df["wind_code"].unique())

    for code in codes:
        sub = prepare_stock_series(weekly_df, code)
        if len(sub) < max(forward_weeks) + 10:
            continue

        delta_f = sub["delta_f"].values
        ret_r = sub["return_r"].values

        for k in forward_weeks:
            # 方向 A: ΔF_t -> r_{t+k}
            x_a = delta_f[:-k]
            y_a = ret_r[k:]
            mask_a = ~(np.isnan(x_a) | np.isnan(y_a))
            if mask_a.sum() >= 10 and np.std(x_a[mask_a]) > 1e-10:
                slope, intercept, r_value, p_value, std_err = stats.linregress(
                    x_a[mask_a], y_a[mask_a]
                )
                results_a.append(
                    {
                        "wind_code": code,
                        "lag": k,
                        "beta": slope,
                        "alpha": intercept,
                        "r_squared": r_value**2,
                        "p_value": p_value,
                        "n_obs": int(mask_a.sum()),
                    }
                )

            # 方向 B: r_t -> ΔF_{t+k}
            x_b = ret_r[:-k]
            y_b = delta_f[k:]
            mask_b = ~(np.isnan(x_b) | np.isnan(y_b))
            if mask_b.sum() >= 10 and np.std(x_b[mask_b]) > 1e-10:
                slope, intercept, r_value, p_value, std_err = stats.linregress(
                    x_b[mask_b], y_b[mask_b]
                )
                results_b.append(
                    {
                        "wind_code": code,
                        "lag": k,
                        "beta": slope,
                        "alpha": intercept,
                        "r_squared": r_value**2,
                        "p_value": p_value,
                        "n_obs": int(mask_b.sum()),
                    }
                )

    return pd.DataFrame(results_a), pd.DataFrame(results_b)


def build_event_study(
    weekly_df: pd.DataFrame,
    event_col: str = "delta_f",
    window: int = 8,
    top_pct: float = 0.90,
    bottom_pct: float = 0.10,
):
    """
    事件研究：预期大幅上调/下调前后的股价累计超额收益（CAR）。
    超额收益 = 个股收益 - 等权市场平均收益
    """
    events = []
    codes = sorted(weekly_df["wind_code"].unique())

    # 计算每期等权市场平均收益
    # 先算每只股票的周收益率，再按日期截面平均
    _tmp = weekly_df.copy()
    _tmp["_ret"] = _tmp.groupby("wind_code")["close_hkd"].pct_change()
    market_ret = _tmp.groupby("trade_date")["_ret"].mean().reset_index()
    market_ret.columns = ["trade_date", "market_return"]

    for code in codes:
        sub = prepare_stock_series(weekly_df, code)
        if len(sub) < window * 2 + 5:
            continue

        # 合并市场收益，计算超额收益
        sub = sub.merge(market_ret, on="trade_date", how="left")
        sub["excess_return"] = sub["return_r"] - sub["market_return"].fillna(0)
        sub["excess_return"] = sub["excess_return"].replace([np.inf, -np.inf], np.nan)

        q_high = sub[event_col].quantile(top_pct)
        q_low = sub[event_col].quantile(bottom_pct)

        high_mask = sub[event_col] >= q_high
        low_mask = sub[event_col] <= q_low

        for idx in sub[high_mask].index:
            if idx < window or idx >= len(sub) - window:
                continue
            car = [sub.iloc[idx + w]["excess_return"] for w in range(-window, window + 1)]
            events.append({"wind_code": code, "event_type": "大幅上调", "car": car})

        for idx in sub[low_mask].index:
            if idx < window or idx >= len(sub) - window:
                continue
            car = [sub.iloc[idx + w]["excess_return"] for w in range(-window, window + 1)]
            events.append({"wind_code": code, "event_type": "大幅下调", "car": car})

    if not events:
        return pd.DataFrame(), pd.DataFrame()

    events_df = pd.DataFrame(events)
    events_df["car_cumsum"] = events_df["car"].apply(lambda x: np.nancumsum(x).tolist())

    # 过滤长度不一致的
    valid_len = window * 2 + 1
    up_cars = np.array(
        [
            c
            for c in events_df[events_df["event_type"] == "大幅上调"]["car_cumsum"].tolist()
            if len(c) == valid_len
        ]
    )
    down_cars = np.array(
        [
            c
            for c in events_df[events_df["event_type"] == "大幅下调"]["car_cumsum"].tolist()
            if len(c) == valid_len
        ]
    )

    weeks = list(range(-window, window + 1))
    agg = pd.DataFrame(
        {
            "week": weeks,
            "up_mean": np.nanmean(up_cars, axis=0) if len(up_cars) > 0 else np.nan,
            "up_std": np.nanstd(up_cars, axis=0) if len(up_cars) > 0 else np.nan,
            "up_n": len(up_cars),
            "down_mean": np.nanmean(down_cars, axis=0) if len(down_cars) > 0 else np.nan,
            "down_std": np.nanstd(down_cars, axis=0) if len(down_cars) > 0 else np.nan,
            "down_n": len(down_cars),
        }
    )

    # T检验：每周的平均CAR是否显著不为0（过滤NaN）
    ttests = []
    for i, w in enumerate(weeks):
        up_vals = up_cars[:, i] if len(up_cars) > 0 else np.array([])
        down_vals = down_cars[:, i] if len(down_cars) > 0 else np.array([])
        up_vals = up_vals[~np.isnan(up_vals)]
        down_vals = down_vals[~np.isnan(down_vals)]

        if len(up_vals) > 3:
            t_up, p_up = stats.ttest_1samp(up_vals, 0)
        else:
            t_up, p_up = np.nan, np.nan

        if len(down_vals) > 3:
            t_down, p_down = stats.ttest_1samp(down_vals, 0)
        else:
            t_down, p_down = np.nan, np.nan

        ttests.append(
            {"week": w, "up_t": t_up, "up_p": p_up, "down_t": t_down, "down_p": p_down}
        )

    ttest_df = pd.DataFrame(ttests)
    agg = agg.merge(ttest_df, on="week", how="left")

    return events_df, agg


# ==================== Layer 3: 状态依赖 + VAR + IRF ====================

def state_dependent_cross_correlation(
    weekly_df: pd.DataFrame, static_df: pd.DataFrame | None = None, max_lag: int = 8
):
    """
    按市场状态（牛市/熊市）和机构覆盖度分组计算交叉相关。
    """
    # 1. 计算市场状态：等权指数 vs 20周均线
    index_df = weekly_df.groupby("trade_date")["close_hkd"].mean().reset_index()
    index_df = index_df.sort_values("trade_date").reset_index(drop=True)
    index_df["index_ma20"] = index_df["close_hkd"].rolling(window=20, min_periods=10).mean()
    index_df["bull"] = index_df["close_hkd"] > index_df["index_ma20"]
    index_df["bull"] = index_df["bull"].fillna(False)

    weekly_df = weekly_df.merge(index_df[["trade_date", "bull"]], on="trade_date", how="left")

    # 2. 合并机构覆盖度（取最新 static）
    if (
        static_df is not None
        and not static_df.empty
        and "inst_num_2025" in static_df.columns
    ):
        latest_static = (
            static_df.sort_values("update_date").groupby("wind_code").last().reset_index()
        )
        weekly_df = weekly_df.merge(
            latest_static[["wind_code", "inst_num_2025"]],
            on="wind_code",
            how="left",
        )
        weekly_df["inst_num_2025"] = pd.to_numeric(
            weekly_df["inst_num_2025"], errors="coerce"
        )
        median_inst = weekly_df["inst_num_2025"].median()
        if pd.isna(median_inst) or median_inst == 0:
            # 机构数据无效，用代码排序做代理分组
            codes = sorted(weekly_df["wind_code"].unique())
            mid = len(codes) // 2
            high_codes = set(codes[mid:])
            weekly_df["high_coverage"] = weekly_df["wind_code"].isin(high_codes)
        else:
            weekly_df["high_coverage"] = weekly_df["inst_num_2025"] > median_inst
    else:
        codes = sorted(weekly_df["wind_code"].unique())
        mid = len(codes) // 2
        high_codes = set(codes[mid:])
        weekly_df["high_coverage"] = weekly_df["wind_code"].isin(high_codes)

    # 3. 分组计算
    groups = {
        "牛市": weekly_df[weekly_df["bull"] == True],
        "熊市": weekly_df[weekly_df["bull"] == False],
        "高覆盖": weekly_df[weekly_df["high_coverage"] == True],
        "低覆盖": weekly_df[weekly_df["high_coverage"] == False],
    }

    results = {}
    for name, grp in groups.items():
        if len(grp) > 0:
            agg, _, _ = compute_all_cross_correlations(grp, max_lag)
            if not agg.empty:
                results[name] = agg

    return results


def var_granger_irf(weekly_df: pd.DataFrame, max_lag: int = 8, irf_periods: int = 12):
    """
    对截面平均的双变量时间序列拟合 VAR，输出格兰杰因果 + IRF。
    """
    from statsmodels.tsa.api import VAR

    codes = sorted(weekly_df["wind_code"].unique())
    panel = {}

    for code in codes:
        sub = prepare_stock_series(weekly_df, code)
        if len(sub) < 30:  # 至少30周才纳入
            continue
        sub = sub.set_index("trade_date")
        panel[code] = sub

    if not panel:
        return None, None, None

    # 宽格式面板：每周截面平均（不截断，各股票用各自完整序列）
    panel_df = pd.concat(panel, axis=1)
    delta_f_avg = panel_df.xs("delta_f", level=1, axis=1).mean(axis=1, skipna=True)
    return_r_avg = panel_df.xs("return_r", level=1, axis=1).mean(axis=1, skipna=True)

    var_data = pd.DataFrame({
        "delta_f": delta_f_avg,
        "return_r": return_r_avg,
    }).dropna()

    if len(var_data) < max_lag * 2 + 10:
        return None, None, None

    model = VAR(var_data)
    try:
        actual_max_lag = min(max_lag, len(var_data) // 3)
        if actual_max_lag < 1:
            return None, None, None
        results = model.fit(maxlags=actual_max_lag, ic="aic")
    except Exception:
        return None, None, None

    # 格兰杰因果检验
    gc_f_to_r = results.test_causality("return_r", "delta_f", kind="f")
    gc_r_to_f = results.test_causality("delta_f", "return_r", kind="f")

    gc = {
        "f_to_r": {
            "stat": float(gc_f_to_r.test_statistic),
            "pvalue": float(gc_f_to_r.pvalue),
            "sig": float(gc_f_to_r.pvalue) < 0.05,
        },
        "r_to_f": {
            "stat": float(gc_r_to_f.test_statistic),
            "pvalue": float(gc_r_to_f.pvalue),
            "sig": float(gc_r_to_f.pvalue) < 0.05,
        },
    }

    # 脉冲响应函数
    irf = results.irf(irf_periods)
    irf_data = {
        "periods": list(range(irf_periods + 1)),
        # delta_f 冲击 → return_r 响应
        "df_to_r": irf.irfs[:, 1, 0].tolist(),
        "df_to_r_lower": (irf.irfs[:, 1, 0] - 1.96 * irf.stderr()[:, 1, 0]).tolist(),
        "df_to_r_upper": (irf.irfs[:, 1, 0] + 1.96 * irf.stderr()[:, 1, 0]).tolist(),
        # return_r 冲击 → delta_f 响应
        "r_to_df": irf.irfs[:, 0, 1].tolist(),
        "r_to_df_lower": (irf.irfs[:, 0, 1] - 1.96 * irf.stderr()[:, 0, 1]).tolist(),
        "r_to_df_upper": (irf.irfs[:, 0, 1] + 1.96 * irf.stderr()[:, 0, 1]).tolist(),
    }

    return results, gc, irf_data


def build_ranking_df(
    stock_best: pd.DataFrame,
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    events_df: pd.DataFrame,
    codes_names_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    把四个分析模块的个股结果聚合为单股一行的排行榜。
    不做任何新的统计计算 —— 仅 groupby / argmax / 末端取值。

    输出列：
      名称 | 代码 | 最优lag | 相关r | 方向A R² | 方向A 前瞻 |
      方向B R² | 方向B 前瞻 | 上调CAR | 下调CAR | 主导方向
    """
    name_map = dict(zip(codes_names_df["wind_code"], codes_names_df["name"]))

    cols = [
        "名称", "代码", "最优lag", "相关r",
        "方向A R²", "方向A 前瞻", "方向B R²", "方向B 前瞻",
        "上调CAR", "下调CAR", "主导方向",
    ]

    if stock_best is None or stock_best.empty:
        return pd.DataFrame(columns=cols)

    # 1) 相关性：取每只股票的最优 lag / 相关系数
    base = stock_best[["wind_code", "best_lag", "best_corr"]].rename(
        columns={"best_lag": "最优lag", "best_corr": "相关r"}
    )

    # 2) 双向回归：按 wind_code 取 R² 最大的那一行（带 lag）
    def _best_r2(df):
        if df is None or df.empty:
            return pd.DataFrame(columns=["wind_code", "r2", "lag"])
        idx = df.groupby("wind_code")["r_squared"].idxmax()
        return (
            df.loc[idx, ["wind_code", "r_squared", "lag"]]
            .reset_index(drop=True)
        )

    best_a = _best_r2(df_a).rename(columns={"r_squared": "方向A R²", "lag": "方向A 前瞻"})
    best_b = _best_r2(df_b).rename(columns={"r_squared": "方向B R²", "lag": "方向B 前瞻"})

    # 3) 事件研究：取每只股票事件窗口末端 CAR 的平均
    if events_df is not None and not events_df.empty:
        ev = events_df.copy()
        ev["car_end"] = ev["car_cumsum"].apply(
            lambda v: v[-1] if isinstance(v, list) and len(v) > 0 else np.nan
        )
        up_car = (
            ev[ev["event_type"] == "大幅上调"]
            .groupby("wind_code")["car_end"]
            .mean()
            .reset_index()
            .rename(columns={"car_end": "上调CAR"})
        )
        down_car = (
            ev[ev["event_type"] == "大幅下调"]
            .groupby("wind_code")["car_end"]
            .mean()
            .reset_index()
            .rename(columns={"car_end": "下调CAR"})
        )
    else:
        up_car = pd.DataFrame(columns=["wind_code", "上调CAR"])
        down_car = pd.DataFrame(columns=["wind_code", "下调CAR"])

    # 合并
    out = base.merge(best_a, on="wind_code", how="left") \
              .merge(best_b, on="wind_code", how="left") \
              .merge(up_car, on="wind_code", how="left") \
              .merge(down_car, on="wind_code", how="left")

    out["名称"] = out["wind_code"].map(name_map).fillna(out["wind_code"])
    out = out.rename(columns={"wind_code": "代码"})

    # 主导方向
    def _direction(row):
        a = row.get("方向A R²", np.nan)
        b = row.get("方向B R²", np.nan)
        if pd.isna(a) and pd.isna(b):
            return "—"
        if pd.isna(a):
            return "股价→预期"
        if pd.isna(b):
            return "预期→股价"
        if abs(a - b) < 0.01:
            return "近似同步"
        return "预期→股价" if a > b else "股价→预期"

    out["主导方向"] = out.apply(_direction, axis=1)

    return out[cols]
