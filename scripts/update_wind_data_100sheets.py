"""
100-Sheet 一次性采集：一个 Excel 工作簿里放 100 个 Sheet 副本，
一次性写入 100 只股票代码，统一计算，等待 10 分钟，一次性读取入库。

前置条件：
    1. Wind 终端已登录
    2. templates/template_100sheets.xlsx 已生成（100 个子表，每个都是原模板副本，B1 已清空）
    3. 运行前确保文件未被其他 Excel 进程占用

使用方式：
    python scripts/update_wind_data_100sheets.py
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

# ============== 配置 ==============
ROOT = Path(__file__).parent.parent.resolve()
CODES_CSV = ROOT / "config" / "codes.csv"
TEMPLATE = ROOT / "templates" / "template_100sheets.xlsx"
DB_PATH = ROOT / "data" / "wind_history.db"
LOG_DIR = ROOT / "logs"

WAIT_SECONDS = 600  # 10 分钟，100 个 Sheet 同时计算 Wind 公式
SHEETS = [f"Sheet{i}" for i in range(1, 101)]

# ============== 日志 ==============
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"update_100s_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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
    b = static_vals[0] if static_vals[0] else [None] * 6
    c = static_vals[1] if static_vals[1] else [None] * 6
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
    b_vals = weekly_vals[0] if weekly_vals[0] else []
    c_vals = weekly_vals[1] if weekly_vals[1] else []
    rows = []
    for i, d in enumerate(dates):
        if d is None:
            continue
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


# ============== 采集逻辑 ==============
def read_codes():
    df = pd.read_csv(CODES_CSV)
    return list(zip(df["wind_code"].astype(str), df.get("name", "")))


def main():
    logger.info("=" * 60)
    logger.info("100-Sheet 一次性采集启动")
    logger.info(f"Template: {TEMPLATE}")
    logger.info(f"Wait: {WAIT_SECONDS}s (~10min)")
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

        # 1. 把 100 只股票代码分别写入 100 个 Sheet 的 B1
        logger.info("Writing 100 wind_codes to B1...")
        for i, (code, name) in enumerate(codes):
            sheet = wb.sheets[SHEETS[i]]
            sheet.range("B1").value = code
            if (i + 1) % 20 == 0:
                logger.info(f"  {i + 1}/{total} written")
        logger.info("All codes written.")

        # 2. 统一触发重算
        logger.info("Calculating 100 sheets... waiting 10 minutes")
        wb.app.calculate()
        time.sleep(WAIT_SECONDS)

        # 3. 逐个 Sheet 读取数据
        logger.info("Reading data from 100 sheets...")
        for i, (code, name) in enumerate(codes):
            try:
                sheet = wb.sheets[SHEETS[i]]

                static_raw = sheet.range("B3:C8").value
                static_b = [static_raw[j][0] if static_raw and j < len(static_raw) and static_raw[j] else None for j in range(6)]
                static_c = [static_raw[j][1] if static_raw and j < len(static_raw) and static_raw[j] else None for j in range(6)]

                dates = sheet.range("A11:A99").value
                weekly_b = sheet.range("B11:B99").value
                weekly_c = sheet.range("C11:C99").value

                save_static(conn, update_date, code, name, [static_b, static_c])
                save_weekly(conn, update_date, code, name, dates, [weekly_b, weekly_c])

                valid_rows = sum(1 for d in dates if d is not None)
                success += 1
                if (i + 1) % 20 == 0:
                    logger.info(f"  {i + 1}/{total} read OK ({valid_rows} rows)")
            except Exception as e:
                logger.error(f"  FAILED {code}: {e}")
                failed.append(code)

        conn.commit()

    finally:
        if wb:
            try:
                wb.close()
                logger.info("Workbook closed.")
            except Exception as e:
                logger.warning(f"Error closing workbook: {e}")
        if app:
            try:
                app.quit()
                logger.info("Excel app quit.")
            except Exception as e:
                logger.warning(f"Error quitting Excel: {e}")
        conn.close()

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
