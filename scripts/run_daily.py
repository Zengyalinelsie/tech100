"""
每日一键启动脚本：采集 → 校验 → 启动看板

使用方式：
    conda activate tech100
    python scripts/run_daily.py
"""
import subprocess
import sys
import sqlite3
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent.resolve()
COLLECT_SCRIPT = ROOT / "scripts" / "update_wind_data_25sheets.py"
DB_PATH = ROOT / "data" / "wind_history.db"
DASHBOARD = ROOT / "app" / "dashboard.py"

# 校验阈值
STATIC_THRESHOLD = 95   # static_indicators 最少行数
WEEKLY_THRESHOLD = 8500  # weekly_data 最少行数


def run_collection():
    """Step 1: 运行采集脚本"""
    print("=" * 60)
    print("Step 1: 开始采集数据")
    print("=" * 60)
    start = time.time()

    result = subprocess.run(
        [sys.executable, str(COLLECT_SCRIPT)],
        cwd=ROOT,
        capture_output=False,  # 让日志实时输出到终端
    )

    elapsed = time.time() - start
    print(f"\n采集耗时: {elapsed:.0f} 秒")

    if result.returncode != 0:
        print("❌ 采集失败！请检查上面的错误日志。")
        return False, elapsed

    print("✅ 采集脚本正常结束")
    return True, elapsed


def validate_data():
    """Step 2: 校验数据库行数"""
    print("\n" + "=" * 60)
    print("Step 2: 数据校验")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    static_count = conn.execute("SELECT COUNT(*) FROM static_indicators").fetchone()[0]
    weekly_count = conn.execute("SELECT COUNT(*) FROM weekly_data").fetchone()[0]
    conn.close()

    print(f"  static_indicators: {static_count} 行 (阈值: {STATIC_THRESHOLD})")
    print(f"  weekly_data:       {weekly_count} 行 (阈值: {WEEKLY_THRESHOLD})")

    static_ok = static_count >= STATIC_THRESHOLD
    weekly_ok = weekly_count >= WEEKLY_THRESHOLD

    if static_ok and weekly_ok:
        print("✅ 数据校验通过")
        return True
    else:
        print("⚠️  数据校验不通过：")
        if not static_ok:
            print(f"    - static_indicators 只有 {static_count} 行，低于阈值 {STATIC_THRESHOLD}")
        if not weekly_ok:
            print(f"    - weekly_data 只有 {weekly_count} 行，低于阈值 {WEEKLY_THRESHOLD}")
        print("    建议：检查日志中的失败股票，或手动补刷后重试。")
        return False


def start_dashboard():
    """Step 3: 启动 Streamlit 看板"""
    print("\n" + "=" * 60)
    print("Step 3: 启动 Streamlit 看板")
    print("=" * 60)
    print("正在启动，浏览器将自动打开...")
    print(f"看板地址: http://localhost:8501")
    print("=" * 60)

    subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(DASHBOARD)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    print(f"\n🚀 香港科技100 每日采集启动器")
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   项目: {ROOT}")

    # Step 1
    ok, elapsed = run_collection()
    if not ok:
        sys.exit(1)

    # Step 2
    if not validate_data():
        print("\n❌ 流程终止：数据校验不通过，看板未启动。")
        sys.exit(1)

    # Step 3
    start_dashboard()

    print("\n✅ 全部完成！请打开浏览器查看看板。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 异常: {e}")
        sys.exit(1)
