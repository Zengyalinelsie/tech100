"""V8 日频 universe 采集:xlwings 驱动 template_universe_daily.xlsx,
循环交易日写 B1,读 A3:G35(33 universe × 5 字段)入库 universe_daily。

设计沿用 collect_panel.py(已踩过的坑):
- 复用已运行的 Excel 实例(Wind 只服务一个)
- 写入后轮询区域,fetch 占位符 + 防陈旧双保险
- 模板 B1 用字符串日期(P0 验证发现:datetime cell 会让公式返 0)
- tqdm 进度,断点续采(读 universe_daily 已采日期跳过)
- caffeinate 在 shell 层加

用法:
    uv run python scripts/collect_universe_daily.py --daily
    uv run python scripts/collect_universe_daily.py --backfill --from 2021-01-04
    uv run python scripts/collect_universe_daily.py --date 2026-05-22
"""
import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import xlwings as xw
from tqdm import tqdm

ROOT = Path(__file__).parent.parent.resolve()
TEMPLATE = ROOT / "templates" / "template_universe_daily.xlsx"
DB_PATH = ROOT / "data" / "wind_history.db"
LOG_DIR = ROOT / "logs"

N_UNIVERSE = 33
FIRST_ROW = 3
LAST_ROW = FIRST_ROW + N_UNIVERSE - 1            # 35
READ_RANGE = f"A{FIRST_ROW}:G{LAST_ROW}"          # 代码 + 日期 + 5 字段

# 字段对齐 universe_daily 表(C-G 列 → DB 列)
FIELDS = ["close", "pe_ttm", "pb", "div_yield", "fy1_eps"]

MIN_WAIT = 5.0
POLL = 1.5
STABLE_NEEDED = 2
TIMEOUT = 600
PLACEHOLDERS = ("fetch", "提取", "请求", "loading", "计算中", "正在")

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"univ_daily_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------- 交易日生成(用 benchmark 表已有的周一日期 + 港股交易日近似)----------

def trading_days(from_date: date, to_date: date):
    """生成 [from, to] 区间内所有交易日(Mon-Fri,粗略,不剔除节假日)。

    V1 简化:周一-周五全算交易日,允许部分日期采集失败(Wind 自然返回 0)。
    V2 可接入 HKEX 日历 / pandas_market_calendars 精确化。
    """
    out = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:           # 0-4 = Mon-Fri
            out.append(d)
        d += timedelta(days=1)
    return out


def existing_dates(conn) -> set:
    """已采日期 = universe_daily 表里所有 trade_date(任一 universe 有数据即算已采)。"""
    return {r[0] for r in conn.execute("SELECT DISTINCT trade_date FROM universe_daily")}


# ---------- Excel 等数据收敛(沿用 collect_panel.py 同款双保险)----------

def has_data(grid) -> bool:
    if not grid:
        return False
    non_null = sum(1 for row in grid for v in row if v is not None)
    return non_null > len(grid) * 2


def is_fetching(grid) -> bool:
    for row in grid:
        for v in row:
            if isinstance(v, str) and any(p in v.lower() for p in PLACEHOLDERS):
                return True
    return False


def _flat(grid):
    return tuple(tuple("" if v is None else v for v in row) for row in grid)


def wait_calc(sht, prev_flat=None) -> bool:
    app = sht.book.app
    app.calculate()
    time.sleep(MIN_WAIT)
    prev = None
    stable = 0
    start = time.time()
    last_calc = time.time()
    while time.time() - start < TIMEOUT:
        cur = sht.range(READ_RANGE).value
        if is_fetching(cur):
            if time.time() - last_calc > 5:
                try:
                    app.calculate()
                except Exception:
                    pass
                last_calc = time.time()
            stable = 0
            prev = None
            time.sleep(POLL)
            continue
        flat = _flat(cur)
        if prev_flat is not None and flat == prev_flat:
            if time.time() - last_calc > 5:
                try:
                    app.calculate()
                except Exception:
                    pass
                last_calc = time.time()
            stable = 0
            prev = None
            time.sleep(POLL)
            continue
        if prev is not None and flat == prev and has_data(cur):
            stable += 1
            if stable >= STABLE_NEEDED:
                return True
        else:
            stable = 0
        prev = flat
        time.sleep(POLL)
    return False


# ---------- 入库 ----------

def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def save_grid(conn, trade_date, grid, update_ts):
    """grid = A3:G35,每行 [code, date_cell, close, pe, pb, div, fy1_eps]。"""
    rows = []
    for r in grid:
        code = r[0]
        if not code or not isinstance(code, str):
            continue
        vals = [_num(x) for x in r[2:7]]          # C..G = 5 字段
        rows.append((trade_date, code.strip(), *vals, update_ts))
    if not rows:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO universe_daily
           (trade_date, universe_code, close, pe_ttm, pb, div_yield, fy1_eps, update_ts)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true", help="只采今天(工作日)")
    ap.add_argument("--backfill", action="store_true", help="回拉历史")
    ap.add_argument("--from", dest="from_date", default="2021-01-04", help="回拉起始 YYYY-MM-DD")
    ap.add_argument("--to", dest="to_date", default=None, help="回拉结束(默认今天)")
    ap.add_argument("--date", dest="single_date", default=None, help="只采指定一天 YYYY-MM-DD")
    ap.add_argument("--resume", action="store_true", default=True, help="断点续采(默认开)")
    args = ap.parse_args()

    today = date.today()
    if args.single_date:
        target = [datetime.strptime(args.single_date, "%Y-%m-%d").date()]
    elif args.backfill:
        start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        end = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else today
        target = trading_days(start, end)
    else:  # daily
        if today.weekday() >= 5:
            log.warning(f"今天 {today}(weekday={today.weekday()})不是工作日,跳过")
            return
        target = [today]

    conn = sqlite3.connect(DB_PATH)
    done = existing_dates(conn) if args.resume else set()
    todo = [d for d in target if d.strftime("%Y-%m-%d") not in done]
    log.info(f"目标 {len(target)} 天 | 已采 {len(done)} | 待采 {len(todo)}")
    if not todo:
        log.info("无待采日期,退出。")
        conn.close()
        return

    log.info(f"打开 {TEMPLATE}")
    apps = list(xw.apps)
    created_app = False
    if apps:
        app = apps[0]
        log.info(f"复用已打开的 Excel 实例(共 {len(apps)} 个)")
        if len(apps) > 1:
            log.warning("多实例;Wind 只服务一个,建议关掉其他")
    else:
        app = xw.App(visible=True)
        created_app = True
        log.info("未发现 Excel,新建实例")
    app.display_alerts = False

    wb = None
    for b in list(app.books):
        try:
            if Path(b.fullname).name == TEMPLATE.name:
                wb = b
                log.info("使用已打开的 template_universe_daily.xlsx")
                break
        except Exception:
            pass
    opened_wb = False
    if wb is None:
        wb = app.books.open(str(TEMPLATE), update_links=False)
        opened_wb = True

    update_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        wb.app.calculation = "automatic"
        sht = wb.sheets["universe"]

        # 预热一次,得 last_flat 作为防陈旧基线
        ds0 = todo[-1].strftime("%Y-%m-%d")
        sht.range("B1").value = ds0
        wait_calc(sht)
        last_flat = _flat(sht.range(READ_RANGE).value)

        ok = fail = 0
        pbar = tqdm(todo, desc="采集", unit="天", dynamic_ncols=True)
        for i, d in enumerate(pbar, 1):
            ds = d.strftime("%Y-%m-%d")
            try:
                sht.range("B1").value = ds                  # 字符串日期(P0 决策 D5)
                converged = wait_calc(sht, prev_flat=last_flat)
                grid = sht.range(READ_RANGE).value
                last_flat = _flat(grid)
                nrows = save_grid(conn, ds, grid, update_ts)
                ok += 1
                pbar.set_postfix_str(f"{ds} rows={nrows} ok={ok} fail={fail}{'' if converged else ' ⚠超时'}")
                if not converged:
                    log.warning(f"[{i}/{len(todo)}] {ds} 超时(仍入库)")
                if i % 20 == 0:
                    conn.commit()
            except Exception as e:
                fail += 1
                pbar.set_postfix_str(f"{ds} FAIL ok={ok} fail={fail}")
                log.error(f"[{i}/{len(todo)}] {ds} 失败:{e}")
        pbar.close()
        conn.commit()
        log.info(f"完成:成功 {ok} / 失败 {fail}")
    finally:
        if opened_wb:
            try:
                wb.save()
                wb.close()
            except Exception:
                pass
        if created_app:
            try:
                app.quit()
            except Exception:
                pass
        conn.close()


if __name__ == "__main__":
    main()
