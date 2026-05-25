"""因子对比扫描：把因子库里每个因子跑一遍，看哪个真有预测力。

对每个因子复用：
  - analysis_v2.cross_correlation → 最强 lag + 该 lag 的截面平均相关 r
  - strategy.backtest            → IC均值 / ICIR / 多头IR / 多空Sharpe / 平均换手

输出一张按「多头IR」排序的对比表（打印 + 写 data/backtest/factor_scan.csv）。

用法（项目根目录）：
    uv run python scripts/factor_scan.py
    uv run python scripts/factor_scan.py --hold 1 --n-drop 3
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT / "app"))

from factors import build_factor_panel, FACTORS          # noqa: E402
from strategy import backtest                            # noqa: E402
from analysis_v2 import cross_correlation                # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hold", type=int, default=1)
    p.add_argument("--top-pct", type=float, default=0.2)
    p.add_argument("--n-drop", type=int, default=3)
    p.add_argument("--cost-bps", type=float, default=30.0)
    args = p.parse_args()

    print("构建因子面板…")
    fac = build_factor_panel()

    rows = []
    for col, meta in FACTORS.items():
        if col not in fac.columns or fac[col].notna().sum() < 500:
            continue
        try:
            agg, _ = cross_correlation(fac, x_col=col, y_col="exret")
            best_lag, best_r = (np.nan, np.nan)
            if not agg.empty:
                bi = agg["mean_r"].abs().idxmax()
                best_lag, best_r = int(agg.loc[bi, "lag"]), float(agg.loc[bi, "mean_r"])

            res = backtest(fac, x_col=col, hold_weeks=args.hold,
                           top_pct=args.top_pct, cost_bps=args.cost_bps, n_drop=args.n_drop)
            m, s = res["metrics"], res["ic_summary"]
            rows.append({
                "因子": meta.name,
                "列名": col,
                "最强lag": best_lag,
                "lag相关r": round(best_r, 4),
                "IC均值": round(s.get("ic_mean", np.nan), 4),
                "ICIR": round(s.get("ic_ir", np.nan), 2),
                "多头IR": round(m.loc["多头", "ir"], 2),
                "多头累计%": round(m.loc["多头", "total_ret"] * 100, 1),
                "多空Sharpe": round(m.loc["多空", "sharpe"], 2),
                "换手%": round(s.get("turnover_long", np.nan) * 100, 0),
            })
            print(f"  ✓ {meta.name}（{col}）")
        except Exception as e:
            print(f"  ✗ {meta.name}（{col}）跳过：{e}")

    tab = pd.DataFrame(rows).sort_values("多头IR", ascending=False)
    out = ROOT / "data" / "backtest" / "factor_scan.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    tab.to_csv(out, index=False)

    print("\n" + "=" * 92)
    print(f"因子对比（hold={args.hold}w, top={args.top_pct:.0%}, n_drop={args.n_drop}, cost={args.cost_bps}bps，恒生科技同期约 -41%）")
    print("=" * 92)
    print(tab.to_string(index=False))
    print(f"\n已写：{out}")
    print("提示：IC≈0 表示横截面预测力弱；多头IR>0 表示该组合跑赢恒生科技基准。结果仅供研究参考。")


if __name__ == "__main__":
    main()
