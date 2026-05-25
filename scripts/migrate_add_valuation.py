"""给 panel_data 增加估值因子原始列（非破坏、幂等）。

新增 6 列(对应 template panel sheet 的 S–X 列,见 DATA_PROCUREMENT.md):
    pb, roe_fwd, div_yield, ev_ebitda, nde, profit_alert

已存在的列自动跳过；旧数据不受影响（回拉前新列为 NULL）。

用法：
    uv run python scripts/migrate_add_valuation.py
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
DB_PATH = ROOT / "data" / "wind_history.db"

NEW_COLS = ["pb", "roe_fwd", "div_yield", "ev_ebitda", "nde", "profit_alert"]


def main():
    conn = sqlite3.connect(DB_PATH)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(panel_data)")}
    added = []
    for col in NEW_COLS:
        if col in existing:
            print(f"  跳过(已存在): {col}")
            continue
        conn.execute(f"ALTER TABLE panel_data ADD COLUMN {col} REAL")
        added.append(col)
        print(f"  ✅ 新增列: {col}")
    conn.commit()
    cols_now = [r[1] for r in conn.execute("PRAGMA table_info(panel_data)")]
    nrows = conn.execute("SELECT COUNT(*) FROM panel_data").fetchone()[0]
    conn.close()
    print(f"\n完成：新增 {len(added)} 列 | panel_data 现有 {len(cols_now)} 列 / {nrows} 行(旧数据完好)")


if __name__ == "__main__":
    main()
