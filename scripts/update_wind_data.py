"""
自动驱动 Wind Excel 插件，逐只刷新 100 只股票数据并写入 SQLite。

前置条件：
    1. Wind 终端已登录
    2. template.xlsx 中公式已配置好（引用 B1 作为 wind_code）
    3. 运行前确保 template.xlsx 未被其他 Excel 进程占用

使用方式：
    python scripts/update_wind_data.py
"""
import os
import sys
import time
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import xlwings as xw

# ============== 路径配置 ==============
ROOT = Path(__file__).parent.parent.resolve()
CODES_CSV = ROOT / "config" / "codes.csv"
TEMPLATE = ROOT / "templates" / "template.xlsx"
DB_PATH = ROOT / "data" / "wind_history.db"
LOG_DIR = ROOT / "logs"

WAIT_SECONDS = 20          # Wind 公式计算等待时间
SHEET_NAME = "Sheet1"      # 模板工作表名称

# ============== 日志 ==============
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ============== 数据库 ==============

def get_conn():
    return sqlite3.connect(DB_PATH)


def save_static(conn, update_date, wind_code, name, static_vals):
    """static_vals: [[B3,B4,B5,B6,B7,B8], [C3,C4,C5,C6,C7,C8]]"""
    b = static_vals[0] if static_vals[0] else [None]*6
    c = static_vals[1] if static_vals[1] else [None]*6
    conn.execute(
        """INSERT OR REPLACE INTO static_indicators
        (update_date, wind_code, name,
         inst_num_2025, netprofit_avg_2025, netprofit_max_2025,
         netprofit_min_2025, netprofit_median_2025, netprofit_std_2025,
         inst_num_2026, netprofit_avg_2026, netprofit_max_2026,
         netprofit_min_2026, netprofit_median_2026, netprofit_std_2026)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (update_date, wind_code, name,
         b[0], b[1], b[2], b[3], b[4], b[5],
         c[0], c[1], c[2], c[3], c[4], c[5]),
    )


def save_weekly(conn, update_date, wind_code, name, dates, weekly_vals):
    """dates: A11:A99, weekly_vals: [[B11,B12,...], [C11,C12,...]]"""
    b_vals = weekly_vals[0] if weekly_vals[0] else []
    c_vals = weekly_vals[1] if weekly_vals[1] else []
    rows = []
    for i, d in enumerate(dates):
        if d is None:
            continue
        # 统一日期格式
        if isinstance(d, datetime):
            d = d.strftime("%Y-%m-%d")
        elif hasattr(d, "strftime"):
            d = d.strftime("%Y-%m-%d")
        else:
            d = str(d)[:10]
        rows.append((update_date, d, wind_code, name,
                     b_vals[i] if i < len(b_vals) else None,
                     c_vals[i] if i < len(c_vals) else None))
    if rows:
        conn.executemany(
            """INSERT OR REPLACE INTO weekly_data
            (update_date, trade_date, wind_code, name, netprofit_avg, close_hkd)
            VALUES (?,?,?,?,?,?)""",
            rows,
        )


# ============== 主采集逻辑 ==============

def read_codes():
    df = pd.read_csv(CODES_CSV)
    return dict(zip(df["wind_code"].astype(str), df.get("name", "")))


def fetch_one_stock(wb, wind_code, name, update_date, conn):
    sheet = wb.sheets[SHEET_NAME]

    # 1. 写入 wind_code 到 B1（覆盖原有公式/值）
    sheet.range("B1").value = wind_code
    logger.info(f"  Written {wind_code} to B1")

    # 2. 触发重算
    wb.app.calculate()
    logger.info(f"  Calculating... waiting {WAIT_SECONDS}s")
    time.sleep(WAIT_SECONDS)

    # 3. 读取静态指标 B3:C8（2列×6行）
    static_raw = sheet.range("B3:C8").value
    static_vals = [static_raw[i] if static_raw else [None]*6 for i in range(6)]
    # 转置成 [[B3..B8], [C3..C8]]
    static_b = [static_raw[i][0] if static_raw and i < len(static_raw) and static_raw[i] else None for i in range(6)]
    static_c = [static_raw[i][1] if static_raw and i < len(static_raw) and static_raw[i] else None for i in range(6)]
    static_vals = [static_b, static_c]

    # 4. 读取时间序列 A11:C99
    # 先定位实际数据范围：A列从A11往下找最后一个非空日期
    dates = sheet.range("A11:A99").value
    weekly_b = sheet.range("B11:B99").value
    weekly_c = sheet.range("C11:C99").value

    # 保存到数据库
    save_static(conn, update_date, wind_code, name, static_vals)
    save_weekly(conn, update_date, wind_code, name, dates, [weekly_b, weekly_c])
    conn.commit()

    # 统计有效数据行数
    valid_rows = sum(1 for d in dates if d is not None)
    logger.info(f"  Saved: {valid_rows} weekly rows + static indicators")


def main():
    logger.info("=" * 60)
    logger.info("Wind Excel 自动采集启动")
    logger.info(f"Template: {TEMPLATE}")
    logger.info(f"Codes:    {CODES_CSV}")
    logger.info(f"Database: {DB_PATH}")
    logger.info("=" * 60)

    codes = read_codes()
    total = len(codes)
    logger.info(f"Total stocks: {total}")

    update_date = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    success = 0
    failed = []

    app = None
    wb = None
    try:
        logger.info("Opening Excel...")
        app = xw.App(visible=False)
        app.display_alerts = False
        wb = app.books.open(TEMPLATE)
        logger.info("Excel opened.")

        for idx, (wind_code, name) in enumerate(codes.items(), 1):
            try:
                logger.info(f"[{idx}/{total}] Processing {wind_code} ({name})")
                fetch_one_stock(wb, wind_code, name, update_date, conn)
                success += 1
            except Exception as e:
                logger.error(f"  FAILED {wind_code}: {e}")
                failed.append(wind_code)

    finally:
        if wb:
            try:
                wb.close()
                logger.info("Workbook closed (not saved).")
            except Exception as e:
                logger.warning(f"Error closing workbook: {e}")
        if app:
            try:
                app.quit()
                logger.info("Excel app quit.")
            except Exception as e:
                logger.warning(f"Error quitting Excel: {e}")
        conn.close()

    # 汇总
    logger.info("=" * 60)
    logger.info("采集完成")
    logger.info(f"成功: {success} / {total}")
    if failed:
        logger.info(f"失败: {failed}")
    logger.info(f"日志: {log_file}")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"任务异常终止: {e}")
        sys.exit(1)
