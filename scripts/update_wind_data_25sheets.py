"""
25-Sheet 两轮采集：每批 25 只，等待 60 秒，第二轮只重试 Fetch 失败的股票。

使用方式：
    python scripts/update_wind_data_25sheets.py
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
TEMPLATE = ROOT / "templates" / "template_25sheets.xlsx"
DB_PATH = ROOT / "data" / "wind_history.db"
LOG_DIR = ROOT / "logs"

BATCH_SIZE = 25
WAIT_SECONDS = 60
SHEETS = [f"Sheet{i}" for i in range(1, 26)]
FETCH_THRESHOLD = 20  # B11:B99 中 Fetch.../空值 超过此数量视为失败

# ============== 日志 ==============
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"update_25s_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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


# ============== Fetch 检测 ==============
def has_fetch(sheet):
    """检查 Sheet 的 weekly 数据是否有大量 Fetch... 或空值"""
    values = sheet.range("B11:B99").value
    if not values:
        return True
    bad_count = sum(1 for v in values if v in ("Fetch...", None, "", "#N/A"))
    return bad_count > FETCH_THRESHOLD


# ============== 采集逻辑 ==============
def read_codes():
    df = pd.read_csv(CODES_CSV)
    return list(zip(df["wind_code"].astype(str), df.get("name", "")))


def read_one_sheet(sheet, code, name, update_date, conn):
    """读取单个 Sheet 的数据并入库，返回是否成功"""
    try:
        static_raw = sheet.range("B3:C8").value
        static_b = [static_raw[j][0] if static_raw and j < len(static_raw) and static_raw[j] else None for j in range(6)]
        static_c = [static_raw[j][1] if static_raw and j < len(static_raw) and static_raw[j] else None for j in range(6)]

        dates = sheet.range("A11:A99").value
        weekly_b = sheet.range("B11:B99").value
        weekly_c = sheet.range("C11:C99").value

        save_static(conn, update_date, code, name, [static_b, static_c])
        save_weekly(conn, update_date, code, name, dates, [weekly_b, weekly_c])

        valid_rows = sum(1 for d in dates if d is not None)
        return True, valid_rows
    except Exception as e:
        logger.error(f"  Read error {code}: {e}")
        return False, 0


def run_batch(wb, batch, update_date, conn):
    """执行一批：写入 B1 → 计算 → 等待 → 读取 → 检测 Fetch"""
    n = len(batch)
    failed = []
    success = []

    # 1. 写入 B1
    for i, (code, name) in enumerate(batch):
        sheet = wb.sheets[SHEETS[i]]
        sheet.range("B1").value = code
    logger.info(f"  Written {n} codes to B1")

    # 2. 计算 + 等待
    wb.app.calculate()
    logger.info(f"  Calculating... waiting {WAIT_SECONDS}s")
    time.sleep(WAIT_SECONDS)

    # 3. 读取 + 检测 Fetch
    for i, (code, name) in enumerate(batch):
        sheet = wb.sheets[SHEETS[i]]
        if has_fetch(sheet):
            logger.warning(f"  [FETCH] {code} has Fetch.../empty values, will retry")
            failed.append((code, name))
        else:
            ok, rows = read_one_sheet(sheet, code, name, update_date, conn)
            if ok:
                success.append(code)
                logger.info(f"  [OK] {code} — {rows} rows")
            else:
                failed.append((code, name))

    return failed, success


def main():
    logger.info("=" * 60)
    logger.info("25-Sheet 两轮采集启动")
    logger.info(f"Template: {TEMPLATE}")
    logger.info(f"Batch size: {BATCH_SIZE}, Wait: {WAIT_SECONDS}s")
    logger.info("=" * 60)

    codes = read_codes()
    total = len(codes)
    logger.info(f"Total stocks: {total}")

    update_date = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()

    # ========== 第一轮 ==========
    logger.info("\n========== ROUND 1 ==========")
    batches = [codes[i : i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    all_failed = []
    all_success = []

    app = None
    wb = None
    try:
        logger.info("Opening Excel...")
        app = xw.App(visible=False)
        app.display_alerts = False
        wb = app.books.open(TEMPLATE)
        logger.info("Excel opened.")

        for idx, batch in enumerate(batches, 1):
            codes_str = ", ".join([c for c, _ in batch])
            logger.info(f"\nBatch {idx}/{len(batches)}: {codes_str}")
            failed, success = run_batch(wb, batch, update_date, conn)
            all_failed.extend(failed)
            all_success.extend(success)
            conn.commit()

        # ========== 第二轮：重试 Fetch 失败 ==========
        if all_failed:
            logger.info(f"\n========== ROUND 2 (Retry {len(all_failed)} failed) ==========")
            retry_batches = [all_failed[i : i + BATCH_SIZE] for i in range(0, len(all_failed), BATCH_SIZE)]

            for idx, batch in enumerate(retry_batches, 1):
                codes_str = ", ".join([c for c, _ in batch])
                logger.info(f"\nRetry Batch {idx}/{len(retry_batches)}: {codes_str}")
                failed, success = run_batch(wb, batch, update_date, conn)
                # 第二轮成功的从失败列表移除
                for code, _ in batch:
                    if code in success and code in [c for c, _ in all_failed]:
                        all_failed = [(c, n) for c, n in all_failed if c != code]
                conn.commit()
        else:
            logger.info("\n========== ROUND 2: No failures, skipping ==========")

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

    # ========== 汇总 ==========
    logger.info("\n" + "=" * 60)
    logger.info("采集完成")
    logger.info(f"第一轮成功: {len(all_success)}")
    if all_failed:
        logger.info(f"第二轮后仍失败: {len(all_failed)} — {[c for c, _ in all_failed]}")
    else:
        logger.info("第二轮后全部成功")
    logger.info(f"日志: {log_file}")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"任务异常终止: {e}")
        sys.exit(1)
