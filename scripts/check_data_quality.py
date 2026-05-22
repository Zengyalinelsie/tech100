"""V5 数据质量体检：回拉后跑一次，检查 panel_data / static_info / benchmark 是否健康。

用法：
    uv run python scripts/check_data_quality.py
"""
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "wind_history.db"

FY_FIELDS = [
    "fy1_np_avg", "fy1_eps", "fy1_instnum", "fy1_np_std", "fy1_np_median",
    "fy2_np_avg", "fy2_eps", "fy2_instnum", "fy2_np_std", "fy2_np_median",
]
PX_FIELDS = ["close_hkd", "volume", "amount", "turn", "pe_ttm", "mkt_cap"]


def section(t):
    print("\n" + "=" * 64 + f"\n{t}\n" + "=" * 64)


def main():
    conn = sqlite3.connect(DB_PATH)

    section("1. panel_data 体量与跨度")
    df = pd.read_sql("SELECT * FROM panel_data", conn)
    if df.empty:
        print("⚠ panel_data 为空 —— 还没回拉。先跑 collect_panel.py --backfill")
        conn.close()
        return
    n_stocks = df["wind_code"].nunique()
    n_weeks = df["trade_date"].nunique()
    print(f"总行数: {len(df):,}")
    print(f"股票数: {n_stocks} | 周数: {n_weeks}")
    print(f"日期跨度: {df['trade_date'].min()} → {df['trade_date'].max()}")
    print(f"期望行数 ≈ {n_stocks * n_weeks:,}（实际 {len(df):,}，缺口 {n_stocks*n_weeks-len(df):,}）")

    section("2. 字段非空覆盖率（越高越好；早年 FY2 偏低正常）")
    cov = (df[FY_FIELDS + PX_FIELDS].notna().mean() * 100).round(1)
    for k, v in cov.items():
        flag = "  ⚠ 偏低" if v < 60 else ""
        print(f"  {k:16s}: {v:5.1f}%{flag}")

    section("3. 每股时间序列连续性（缺口最多的 5 只）")
    gaps = []
    for code, g in df.groupby("wind_code"):
        dates = pd.to_datetime(g["trade_date"]).sort_values()
        if len(dates) < 2:
            gaps.append((code, len(dates), 0))
            continue
        diff_weeks = dates.diff().dt.days.dropna() / 7
        max_gap = int(diff_weeks.max()) if len(diff_weeks) else 0
        gaps.append((code, len(dates), max_gap))
    gdf = pd.DataFrame(gaps, columns=["wind_code", "n_weeks", "max_gap_weeks"]).sort_values("max_gap_weeks", ascending=False)
    print(gdf.head(5).to_string(index=False))
    print(f"\n周数中位数: {gdf['n_weeks'].median():.0f} | 最少: {gdf['n_weeks'].min()} | 最大缺口: {gdf['max_gap_weeks'].max()} 周")

    section("4. 市值 / PE 量级合理性")
    for col in ["mkt_cap", "pe_ttm", "close_hkd"]:
        s = df[col].dropna()
        if len(s):
            print(f"  {col:10s}: 中位 {s.median():.2f} | 区间 [{s.min():.2f}, {s.max():.2f}]")

    section("5. benchmark 覆盖")
    b = pd.read_sql("SELECT * FROM benchmark", conn)
    if b.empty:
        print("⚠ benchmark 为空")
    else:
        print(f"行数: {len(b)} | 跨度 {b['trade_date'].min()} → {b['trade_date'].max()}")
        print(f"恒科非空: {b['hstech_close'].notna().mean()*100:.0f}% | 恒生非空: {b['hsi_close'].notna().mean()*100:.0f}%")

    section("6. static_info 覆盖")
    s = pd.read_sql("SELECT * FROM static_info", conn)
    if s.empty:
        print("⚠ static_info 为空")
    else:
        print(f"股票数: {len(s)}")
        print(f"行业(l1)非空: {s['industry_l1'].notna().mean()*100:.0f}% | 上市日非空: {s['list_date'].notna().mean()*100:.0f}%")
        if s["industry_l1"].notna().any():
            print("行业分布:")
            print(s["industry_l1"].value_counts().head(10).to_string())

    conn.close()
    print("\n✅ 体检完成")


if __name__ == "__main__":
    main()
