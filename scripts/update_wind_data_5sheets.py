"""
5-Sheet 并行采集：一个 Excel 工作簿里放 5 份模板副本，
每批同时刷新 5 只股票，等待 10 秒。

前置条件：
    1. Wind 终端已登录
    2. excel/template_5sheets.xlsx 已生成（5 个子表，每个都是原模板副本）
    3. 运行前确保文件未被其他 Excel 进程占用

使用方式：
    python scripts/update_wind_data_5sheets.py
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
TEMPLATE = ROOT / "templates" / "template_5sheets.xlsx"
DB_PATH = ROOT / "data" / "wind_history.db"
LOG_DIR = ROOT / "logs"

BATCH_SIZE = 5
WAIT_SECONDS = 20
SHEETS = ["Sheet1", "Sheet2", "Sheet3", "Sheet4", "Sheet5"]

# ============== 日志 ==============
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"update_5s_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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
    """static_vals: [[B3..B8], [C3..C8]]"""
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


def fetch_batch(wb, batch, update_date, conn):
    """batch: list of (wind_code, name), len <= BATCH_SIZE"""
    n = len(batch)

    # 1. 把每只股票写入对应 Sheet 的 B1
    for i, (code, name) in enumerate(batch):
        sheet = wb.sheets[SHEETS[i]]
        sheet.range("B1").value = code
        logger.info(f"  [{i+1}/{n}] Written {code} to {SHEETS[i]}.B1")

    # 2. 统一触发重算
    wb.app.calculate()
    logger.info(f"  Calculating {n} sheets... waiting {WAIT_SECONDS}s")
    time.sleep(WAIT_SECONDS)

    # 3. 逐个 Sheet 读取数据
    for i, (code, name) in enumerate(batch):
        try:
            sheet = wb.sheets[SHEETS[i]]

            # 静态指标 B3:C8
            static_raw = sheet.range("B3:C8").value
            static_b = [static_raw[j][0] if static_raw and j < len(static_raw) and static_raw[j] else None for j in range(6)]
            static_c = [static_raw[j][1] if static_raw and j < len(static_raw) and static_raw[j] else None for j in range(6)]

            # 时间序列 A11:C99
            dates = sheet.range("A11:A99").value
            weekly_b = sheet.range("B11:B99").value
            weekly_c = sheet.range("C11:C99").value

            save_static(conn, update_date, code, name, [static_b, static_c])
            save_weekly(conn, update_date, code, name, dates, [weekly_b, weekly_c])
            conn.commit()

            valid_rows = sum(1 for d in dates if d is not None)
            logger.info(f"  [{i+1}/{n}] {code} OK — {valid_rows} weekly rows")
        except Exception as e:
            logger.error(f"  [{i+1}/{n}] {code} FAILED: {e}")


def main():
    logger.info("=" * 60)
    logger.info("5-Sheet 并行采集启动")
    logger.info(f"Template: {TEMPLATE}")
    logger.info(f"Batch size: {BATCH_SIZE}, Wait: {WAIT_SECONDS}s")
    logger.info("=" * 60)

    codes = read_codes()
    total = len(codes)
    logger.info(f"Total stocks: {total}")

    # 分批次
    batches = [codes[i : i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    logger.info(f"Total batches: {len(batches)}")

    update_date = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()

    app = None
    wb = None
    try:
        logger.info("Opening Excel...")
        app = xw.App(visible=False)
        app.display_alerts = False
        wb = app.books.open(TEMPLATE)
        logger.info("Excel opened.")

        for idx, batch in enumerate(batches, 1):
            codes_in_batch = ", ".join([c for c, _ in batch])
            logger.info(f"Batch {idx}/{len(batches)}: {codes_in_batch}")
            fetch_batch(wb, batch, update_date, conn)

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

    logger.info("=" * 60)
    logger.info("采集完成")
    logger.info(f"预估耗时: ~{len(batches) * WAIT_SECONDS} 秒（每批5只×{WAIT_SECONDS}秒）")
    logger.info(f"日志: {log_file}")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"任务异常终止: {e}")
        sys.exit(1)
