"""
每日一键启动脚本：采集 → 校验 → 推送远程 → 提示 Reboot

使用方式（在项目根目录执行）：
    uv run python scripts/run_daily.py

流程：
    1. 运行 Wind 数据采集（update_wind_data_25sheets.py）
    2. 校验数据库行数
    3. 推送到 GitHub（自动 git add → commit → push）
    4. 提示去 Streamlit Cloud 点击 Reboot
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

# 校验阈值
STATIC_THRESHOLD = 95   # static_indicators 最少行数
WEEKLY_THRESHOLD = 8500  # weekly_data 最少行数

STREAMLIT_CLOUD_URL = "https://share.streamlit.io"  # 用户可替换为自己的 App 链接


def run_collection():
    """Step 1: 运行 Wind 数据采集"""
    print("=" * 60)
    print("Step 1: 开始采集 Wind 数据")
    print("=" * 60)
    start = time.time()

    result = subprocess.run(
        [sys.executable, str(COLLECT_SCRIPT)],
        cwd=ROOT,
        capture_output=False,
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
            print(f"    - static_indicators 只有 {static_count} 行")
        if not weekly_ok:
            print(f"    - weekly_data 只有 {weekly_count} 行")
        return False


def push_to_github():
    """Step 3: 推送到 GitHub"""
    print("\n" + "=" * 60)
    print("Step 3: 推送到远程仓库")
    print("=" * 60)

    # 3.1 检查当前分支和远程状态
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    changed_files = result.stdout.strip()

    if not changed_files:
        print("ℹ️  数据库文件无变化，无需推送。")
        print("   （可能今天采集的数据和已有数据日期相同，被 UPSERT 覆盖了）")
        return True

    print(f"检测到 {len(changed_files.splitlines())} 个文件有变更：")
    for line in changed_files.splitlines()[:10]:
        print(f"    {line}")
    if len(changed_files.splitlines()) > 10:
        print(f"    ... 等共 {len(changed_files.splitlines())} 个文件")

    # 3.2 先拉取远程最新（避免冲突）
    print("\n  → 同步远程最新代码...")
    pull_result = subprocess.run(
        ["git", "pull", "--rebase", "origin", "main"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if pull_result.returncode != 0:
        print(f"⚠️  拉取远程失败：{pull_result.stderr.strip()}")
        print("    请手动解决冲突后再推送。")
        return False

    # 3.3 添加变更并提交
    today = datetime.now().strftime("%Y-%m-%d")
    commit_msg = f"data: {today} daily update"

    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", commit_msg], cwd=ROOT, check=True)

    # 3.4 推送
    push_result = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    if push_result.returncode == 0:
        print(f"✅ 推送成功：{commit_msg}")
        return True
    else:
        print(f"❌ 推送失败：{push_result.stderr.strip()}")
        return False


def print_next_steps():
    """Step 4: 提示用户下一步操作"""
    print("\n" + "=" * 60)
    print("Step 4: 去 Streamlit Cloud 重新部署")
    print("=" * 60)
    print("""
🌐 推送已完成，请按下面步骤操作：

   1. 打开你的 Streamlit Cloud App 页面
   2. 点击右下角 "Manage app" → "Reboot"
   3. 等待 1–2 分钟重新部署
   4. 刷新页面即可看到最新数据

💡 快捷方式：
   https://streamlit.io/cloud  → 找到 tech100 → Reboot
""")


def main():
    print(f"\n🚀 香港科技100 每日采集启动器")
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   项目: {ROOT}")

    # Step 1: 采集
    ok, _ = run_collection()
    if not ok:
        sys.exit(1)

    # Step 2: 校验
    if not validate_data():
        print("\n❌ 流程终止：数据校验不通过，未推送到远程。")
        sys.exit(1)

    # Step 3: 推送
    if not push_to_github():
        print("\n⚠️  推送失败，请手动检查后再试。")
        sys.exit(1)

    # Step 4: 提示
    print_next_steps()

    print("\n✅ 全部完成！")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n用户中断")
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Git 命令失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 异常: {e}")
        sys.exit(1)
