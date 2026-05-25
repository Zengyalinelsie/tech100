"""股池状态表：用 INF 港股数据给 100 只标的打"上市/退市/HSC"标记。

借 INF(闻昭回测系统)的港股生命周期数据,只读它的 CSV(不引入 backtesting 包):
    backtesting/data/listing_delisting/stock_lifecycle.csv   上市/退市日 + is_active
    backtesting/data/high_shareholding_concentration/*.hsc.csv  高度集中持股名单(快照)

输出 config/universe_status.csv(供 strategy.py 做退市保险栓):
    wind_code, code5, name, list_date, delist_date, is_active, hsc_flag

用法(项目根目录):
    uv run python scripts/build_universe.py
"""
import glob
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent.resolve()
INF = ROOT / "backtesting" / "data"
LIFECYCLE = INF / "listing_delisting" / "stock_lifecycle.csv"
HSC_GLOB = str(INF / "high_shareholding_concentration" / "*.hsc.csv")
CODES = ROOT / "config" / "codes.csv"
OUT = ROOT / "config" / "universe_status.csv"


def to_code5(wind_code: str) -> str:
    """0700.HK -> 00700"""
    return wind_code.replace(".HK", "").strip().zfill(5)


def main():
    codes = pd.read_csv(CODES)
    if "wind_code" not in codes.columns:
        codes.columns = ["wind_code", "name"][: len(codes.columns)]
    codes["code5"] = codes["wind_code"].map(to_code5)

    if not LIFECYCLE.exists():
        print(f"❌ 找不到 INF 数据:{LIFECYCLE}\n   确认 backtesting/ 已在项目下。")
        sys.exit(1)

    life = pd.read_csv(LIFECYCLE, dtype={"stock_code": str})
    life["stock_code"] = life["stock_code"].str.zfill(5)
    life = life.rename(columns={
        "stock_code": "code5", "listing_date": "list_date",
        "delisting_date": "delist_date",
    })

    out = codes.merge(life[["code5", "list_date", "delist_date", "is_active"]],
                      on="code5", how="left")

    # HSC 快照(取最新一份)
    hsc_files = sorted(glob.glob(HSC_GLOB))
    hsc_set = set()
    hsc_src = "—"
    if hsc_files:
        hsc_src = Path(hsc_files[-1]).name
        hsc = pd.read_csv(hsc_files[-1], dtype=str)
        code_col = next((c for c in hsc.columns if "stockcode" in c.lower()
                         or c.lower() == "code"), hsc.columns[0])
        hsc_set = set(hsc[code_col].str.zfill(5))
    out["hsc_flag"] = out["code5"].isin(hsc_set)

    out = out[["wind_code", "code5", "name", "list_date", "delist_date",
               "is_active", "hsc_flag"]]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False)

    hit = out["is_active"].notna().sum()
    delisted = (out["is_active"].astype(str) == "False").sum()
    hsc_n = int(out["hsc_flag"].sum())
    print(f"✅ 已写 {OUT}")
    print(f"   INF lifecycle 命中:{hit}/{len(out)}")
    print(f"   已退市(is_active=False):{delisted}")
    print(f"   HSC 高集中度命中(快照 {hsc_src}):{hsc_n}")
    if delisted == 0 and hsc_n == 0:
        print("   → 当前池子在退市/HSC 维度本就干净(零删除);保险栓将在未来退市时自动接住。")


if __name__ == "__main__":
    main()
