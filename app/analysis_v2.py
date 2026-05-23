"""核心分析层 v2：基于 factor 面板（5.4 年 / FY1+FY2），方法学升级版。

相比旧 leadlag_analysis.py：
- 数据源：factors.build_factor_panel()（市值标准化预期修正 + 超额收益）
- 双预期：FY1（当年）/ FY2（下一年）各算一套，对比谁更领先
- 回归：Newey-West (HAC) 标准误，修正周频自相关导致的 t 值虚高
- 显著性：截面 t 检验 + 个股 |r| 显著阈值标记

核心问题：盈利预期修正(ΔF/市值) 与 股价超额收益，谁领先谁？
    lag>0：预期领先股价（预期是领先指标）
    lag<0：股价领先预期（市场先动）
"""
import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm

from factors import build_factor_panel

SIG = 0.05


def cross_correlation(fac, x_col="fy1_rev_norm", y_col="exret", max_lag=8, min_obs=20):
    """逐股算 x 与 y 的交叉相关，再截面平均 + t 检验。

    返回 (agg, raw)：
      agg: 每个 lag 的截面平均 r、股票数、t 检验 p 值
      raw: 每股每 lag 的 r
    """
    rows = []
    for code, g in fac.groupby("wind_code"):
        g = g.sort_values("trade_date")
        x = g[x_col].to_numpy(dtype=float)
        y = g[y_col].to_numpy(dtype=float)
        for lag in range(-max_lag, max_lag + 1):
            if lag > 0:
                xx, yy = x[:-lag], y[lag:]
            elif lag < 0:
                xx, yy = x[-lag:], y[:lag]
            else:
                xx, yy = x, y
            m = ~(np.isnan(xx) | np.isnan(yy))
            if m.sum() < min_obs:
                continue
            xc, yc = xx[m], yy[m]
            if np.std(xc) < 1e-12 or np.std(yc) < 1e-12:
                continue
            r, _ = stats.pearsonr(xc, yc)
            rows.append({"wind_code": code, "lag": lag, "r": r, "n": int(m.sum())})

    raw = pd.DataFrame(rows)
    if raw.empty:
        return pd.DataFrame(), raw

    agg_rows = []
    for lag, grp in raw.groupby("lag"):
        rs = grp["r"].dropna().to_numpy()
        if len(rs) > 3:
            t, p = stats.ttest_1samp(rs, 0)
        else:
            t, p = np.nan, np.nan
        agg_rows.append({
            "lag": int(lag),
            "mean_r": rs.mean(),
            "std_r": rs.std(),
            "n_stocks": grp["wind_code"].nunique(),
            "t_stat": t,
            "p_value": p,
            "sig": "★" if (p is not None and p < SIG) else "",
        })
    agg = pd.DataFrame(agg_rows).sort_values("lag").reset_index(drop=True)
    return agg, raw


def bidir_regression_nw(fac, x_col="fy1_rev_norm", y_col="exret", fwd=(1, 2, 4), nw_lags=4, min_obs=30):
    """双向预测回归 + Newey-West 标准误（pooled，按股票分组对齐再合并）。

    方向 A：预期修正_t → 未来超额收益_{t+k}
    方向 B：超额收益_t → 未来预期修正_{t+k}
    返回 (dirA, dirB)：每个前瞻期 k 的 beta / t / p / R²（HAC）
    """
    def _run(xc, yc, k):
        rows_x, rows_y = [], []
        for code, g in fac.groupby("wind_code"):
            g = g.sort_values("trade_date")
            xv = g[xc].to_numpy(dtype=float)
            yv = g[yc].to_numpy(dtype=float)
            if len(g) <= k:
                continue
            rows_x.append(xv[:-k])
            rows_y.append(yv[k:])
        X = np.concatenate(rows_x) if rows_x else np.array([])
        Y = np.concatenate(rows_y) if rows_y else np.array([])
        m = ~(np.isnan(X) | np.isnan(Y))
        if m.sum() < min_obs or np.std(X[m]) < 1e-12:
            return None
        Xc = sm.add_constant(X[m])
        res = sm.OLS(Y[m], Xc).fit(cov_type="HAC", cov_kwds={"maxlags": nw_lags})
        return {
            "k": k, "beta": res.params[1], "t": res.tvalues[1],
            "p": res.pvalues[1], "r2": res.rsquared, "n": int(m.sum()),
            "sig": "★" if res.pvalues[1] < SIG else "",
        }

    dirA, dirB = [], []
    for k in fwd:
        a = _run(x_col, y_col, k)   # 预期 → 未来收益
        b = _run(y_col, x_col, k)   # 收益 → 未来预期
        if a:
            dirA.append(a)
        if b:
            dirB.append(b)
    return pd.DataFrame(dirA), pd.DataFrame(dirB)


def summarize(fac):
    """对 FY1 / FY2 各跑一遍，打印核心结论。"""
    for fy, xcol in [("FY1", "fy1_rev_norm"), ("FY2", "fy2_rev_norm")]:
        print("\n" + "=" * 64)
        print(f"【{fy}】预期修正({xcol}) vs 超额收益(exret)")
        print("=" * 64)
        agg, raw = cross_correlation(fac, x_col=xcol)
        if agg.empty:
            print("  数据不足"); continue
        # 最强 lag
        best = agg.loc[agg["mean_r"].abs().idxmax()]
        print("交叉相关（截面平均，★=p<0.05）：")
        print(agg[["lag", "mean_r", "n_stocks", "t_stat", "p_value", "sig"]].round(4).to_string(index=False))
        bl = int(best["lag"])
        direction = "预期领先股价" if bl > 0 else ("股价领先预期" if bl < 0 else "基本同步")
        print(f"\n  → 最强 lag={bl}（{direction}），mean_r={best['mean_r']:.4f} {best['sig']}")

        dirA, dirB = bidir_regression_nw(fac, x_col=xcol)
        print("\n双向回归（Newey-West HAC）：")
        if not dirA.empty:
            print("  方向A 预期→未来收益:", dirA[["k", "beta", "t", "p", "r2", "sig"]].round(4).to_dict("records"))
        if not dirB.empty:
            print("  方向B 收益→未来预期:", dirB[["k", "beta", "t", "p", "r2", "sig"]].round(4).to_dict("records"))


def event_study(fac, x_col="fy1_rev_norm", y_col="exret", top_pct=0.9, bottom_pct=0.1, window=8):
    """阈值法事件研究：每周截面取 x 的 top/bottom 分位为事件，看事件前后 ±window 周 y 的累计。

    返回 (agg, n_up, n_dn)：
      agg: 各 lag 的上调/下调事件平均 CAR + t 检验
    """
    fac = fac.sort_values(["wind_code", "trade_date"]).copy()
    # 截面分位标记事件
    fac["q_hi"] = fac.groupby("trade_date")[x_col].transform(lambda s: s.quantile(top_pct))
    fac["q_lo"] = fac.groupby("trade_date")[x_col].transform(lambda s: s.quantile(bottom_pct))
    fac["is_up"] = fac[x_col] >= fac["q_hi"]
    fac["is_dn"] = fac[x_col] <= fac["q_lo"]

    up_cars, dn_cars = [], []
    for code, g in fac.groupby("wind_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        ev_up = g.index[g["is_up"]].tolist()
        ev_dn = g.index[g["is_dn"]].tolist()
        for ev_list, bucket in [(ev_up, up_cars), (ev_dn, dn_cars)]:
            for idx in ev_list:
                if idx < window or idx >= len(g) - window:
                    continue
                car = g[y_col].iloc[idx - window: idx + window + 1].to_numpy()
                if len(car) == 2 * window + 1 and not np.any(np.isnan(car)):
                    bucket.append(car)

    weeks = list(range(-window, window + 1))
    rows = []
    up_arr = np.array(up_cars) if up_cars else np.empty((0, len(weeks)))
    dn_arr = np.array(dn_cars) if dn_cars else np.empty((0, len(weeks)))
    up_cum = np.cumsum(up_arr, axis=1) if len(up_arr) else up_arr
    dn_cum = np.cumsum(dn_arr, axis=1) if len(dn_arr) else dn_arr
    for i, w in enumerate(weeks):
        up_t, up_p = (stats.ttest_1samp(up_cum[:, i], 0) if len(up_cum) > 3 else (np.nan, np.nan))
        dn_t, dn_p = (stats.ttest_1samp(dn_cum[:, i], 0) if len(dn_cum) > 3 else (np.nan, np.nan))
        rows.append({
            "week": w,
            "up_car": up_cum[:, i].mean() if len(up_cum) else np.nan,
            "up_t": up_t, "up_p": up_p, "up_sig": "★" if up_p < SIG else "" if not np.isnan(up_p) else "",
            "dn_car": dn_cum[:, i].mean() if len(dn_cum) else np.nan,
            "dn_t": dn_t, "dn_p": dn_p, "dn_sig": "★" if dn_p < SIG else "" if not np.isnan(dn_p) else "",
        })
    return pd.DataFrame(rows), len(up_cars), len(dn_cars)


def granger_var(fac, x_col="fy1_rev_norm", y_col="exret", max_lag=8):
    """截面平均时间序列做 VAR + Granger 因果检验。"""
    from statsmodels.tsa.api import VAR
    panel = (fac.groupby("trade_date")[[x_col, y_col]].mean()
                .dropna().sort_index())
    if len(panel) < max_lag * 2 + 10:
        return None
    try:
        actual_lag = min(max_lag, len(panel) // 3)
        if actual_lag < 1:
            return None
        res = VAR(panel).fit(maxlags=actual_lag, ic="aic")
        if res.k_ar == 0:                              # AIC 选 0 阶 → 回退到 1 阶
            res = VAR(panel).fit(1)
        gc_xy = res.test_causality(y_col, x_col, kind="f")  # x→y
        gc_yx = res.test_causality(x_col, y_col, kind="f")  # y→x
        return {
            "x_to_y": {"stat": float(gc_xy.test_statistic), "p": float(gc_xy.pvalue), "sig": gc_xy.pvalue < SIG},
            "y_to_x": {"stat": float(gc_yx.test_statistic), "p": float(gc_yx.pvalue), "sig": gc_yx.pvalue < SIG},
            "n_obs": len(panel), "lag_order": res.k_ar,
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    print("加载因子面板……")
    fac = build_factor_panel()
    print(f"面板：{len(fac):,} 行 / {fac['wind_code'].nunique()} 股 / {fac['trade_date'].nunique()} 周")
    summarize(fac)

    for fy, xcol in [("FY1", "fy1_rev_norm"), ("FY2", "fy2_rev_norm")]:
        print("\n" + "=" * 64)
        print(f"【{fy}】事件研究（截面 90/10 分位）+ Granger")
        print("=" * 64)
        ev, n_up, n_dn = event_study(fac, x_col=xcol)
        print(f"事件数：上调 {n_up} / 下调 {n_dn}")
        print(ev[["week", "up_car", "up_p", "up_sig", "dn_car", "dn_p", "dn_sig"]].round(4).to_string(index=False))

        gc = granger_var(fac, x_col=xcol)
        if gc and "error" not in gc:
            print(f"\nGranger（VAR lag={gc['lag_order']}, n={gc['n_obs']}）：")
            xy = gc["x_to_y"]; yx = gc["y_to_x"]
            print(f"  预期修正 → 超额收益: F={xy['stat']:.3f} p={xy['p']:.4f} {'★显著' if xy['sig'] else '不显著'}")
            print(f"  超额收益 → 预期修正: F={yx['stat']:.3f} p={yx['p']:.4f} {'★显著' if yx['sig'] else '不显著'}")
