"""
清理当天数据脚本：删除今日 update_date 的所有记录

使用场景：
    - 早上没收盘时跑了数据，现在收盘了要重跑
    - 今天采集数据出错，需要清空重采

使用方式：
    uv run python scripts/cleanup_today.py
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "wind_history.db"


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"准备删除 {today} 的数据...")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 先查有多少条
    weekly_before = cursor.execute(
        "SELECT COUNT(*) FROM weekly_data WHERE update_date = ?", (today,)
    ).fetchone()[0]
    static_before = cursor.execute(
        "SELECT COUNT(*) FROM static_indicators WHERE update_date = ?", (today,)
    ).fetchone()[0]

    if weekly_before == 0 and static_before == 0:
        print(f"ℹ️  {today} 没有数据，无需清理。")
        conn.close()
        return

    print(f"  weekly_data:       {weekly_before} 行")
    print(f"  static_indicators: {static_before} 行")
    print("  确认删除？(y/n)", end=" ")

    confirm = input().strip().lower()
    if confirm not in ("y", "yes"):
        print("已取消")
        conn.close()
        return

    cursor.execute("DELETE FROM weekly_data WHERE update_date = ?", (today,))
    cursor.execute("DELETE FROM static_indicators WHERE update_date = ?", (today,))
    conn.commit()
    conn.close()

    print(f"✅ 已删除 {today} 的所有数据")
    print("现在可以重新运行采集脚本：uv run python scripts/update_wind_data_25sheets.py")


if __name__ == "__main__":
    main()
