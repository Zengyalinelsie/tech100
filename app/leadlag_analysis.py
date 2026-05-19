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


def build_event_study(
    weekly_df: pd.DataFrame,
    event_col: str = "delta_f",
    window: int = 8,
    top_pct: float = 0.90,
    bottom_pct: float = 0.10,
):
    """
    事件研究：预期大幅上调/下调前后的股价累计收益。
    """
    events = []
    codes = sorted(weekly_df["wind_code"].unique())

    for code in codes:
        sub = prepare_stock_series(weekly_df, code)
        if len(sub) < window * 2 + 5:
            continue

        # 计算事件的阈值
        q_high = sub[event_col].quantile(top_pct)
        q_low = sub[event_col].quantile(bottom_pct)

        # 找事件位置
        high_events = sub[sub[event_col] >= q_high].index.tolist()
        low_events = sub[sub[event_col] <= q_low].index.tolist()

        for idx in high_events:
            if idx < window or idx >= len(sub) - window:
                continue
            car = []
            for w in range(-window, window + 1):
                car.append(sub.iloc[idx + w]["return_r"])
            events.append({"wind_code": code, "event_type": "大幅上调", "car": car})

        for idx in low_events:
            if idx < window or idx >= len(sub) - window:
                continue
            car = []
            for w in range(-window, window + 1):
                car.append(sub.iloc[idx + w]["return_r"])
            events.append({"wind_code": code, "event_type": "大幅下调", "car": car})

    if not events:
        return pd.DataFrame(), pd.DataFrame()

    events_df = pd.DataFrame(events)
    events_df["car_cumsum"] = events_df["car"].apply(lambda x: np.cumsum(x).tolist())

    # 按事件类型聚合
    up_cars = np.array(events_df[events_df["event_type"] == "大幅上调"]["car_cumsum"].tolist())
    down_cars = np.array(events_df[events_df["event_type"] == "大幅下调"]["car_cumsum"].tolist())

    agg = pd.DataFrame(
        {
            "week": list(range(-window, window + 1)),
            "up_mean": up_cars.mean(axis=0) if len(up_cars) > 0 else np.nan,
            "up_std": up_cars.std(axis=0) if len(up_cars) > 0 else np.nan,
            "down_mean": down_cars.mean(axis=0) if len(down_cars) > 0 else np.nan,
            "down_std": down_cars.std(axis=0) if len(down_cars) > 0 else np.nan,
        }
    )
    return events_df, agg
