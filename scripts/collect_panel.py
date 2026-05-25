"""V5 面板采集：xlwings 驱动 template_panel.xlsx，循环周一写 panel!B1，
读 panel!A3:R102（100 股 × 16 字段）入库 panel_data。

设计要点：
- 两种模式：--backfill（回拉历史，一次性挂机）/ --daily（每天增量，接 cron）
- 动态等待：写日期后轮询区域，连续稳定才读，超时跳过（不固定 sleep）
- 断点续采：跳过 panel_data 已有的 trade_date

前置：
    1. Wind 终端已登录
    2. templates/template_panel.xlsx 公式已就绪
    3. uv run python scripts/init_db_v2.py 已建表

用法：
    uv run python scripts/collect_panel.py --backfill --from 2021-01-04
    uv run python scripts/collect_panel.py --daily
"""
import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import xlwings as xw

ROOT = Path(__file__).parent.parent.resolve()
TEMPLATE = ROOT / "templates" / "template_panel.xlsx"
DB_PATH = ROOT / "data" / "wind_history.db"
LOG_DIR = ROOT / "logs"

N_STOCKS = 100
PANEL_FIRST_ROW = 3
PANEL_LAST_ROW = PANEL_FIRST_ROW + N_STOCKS - 1      # 102
PANEL_RANGE = f"A{PANEL_FIRST_ROW}:X{PANEL_LAST_ROW}"  # 代码+日期+22字段(16旧+6估值)
SENTINEL_CELL = f"X{PANEL_LAST_ROW}"                  # 最后一只盈警

MIN_WAIT = 3.0       # 写入后最小等待（让 Wind 启动 fetch，单元格变 "Fetch..."）
POLL = 1.0           # 轮询间隔
STABLE_NEEDED = 2    # 连续 N 次读数一致才算收敛
TIMEOUT = 600        # 单个日期最大等待秒（1800 公式 fetch 可能要 3-5 分钟）

# Wind 异步取数时的占位符 —— 只要区域里还有这些，说明没算完
PLACEHOLDERS = ("fetch", "提取", "请求", "loading", "计算中", "正在")

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"collect_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def mondays(from_date: date, to_date: date):
    """生成 [from, to] 区间内所有周一（含起点所在周的周一）。"""
    d = from_date - timedelta(days=from_date.weekday())  # 对齐到本周一
    out = []
    while d <= to_date:
        out.append(d)
        d += timedelta(days=7)
    return out


def existing_dates(conn) -> set:
    return {r[0] for r in conn.execute("SELECT DISTINCT trade_date FROM panel_data")}


def has_data(grid) -> bool:
    """区域是否大部分非空（避免全空时误判稳定）。"""
    if not grid:
        return False
    non_null = sum(1 for row in grid for v in row if v is not None)
    return non_null > len(grid) * 2  # 至少平均每行 2 个非空


def is_fetching(grid) -> bool:
    """区域里是否还有 Wind 取数占位符（如 "Fetch..."）→ 没算完。"""
    for row in grid:
        for v in row:
            if isinstance(v, str) and any(p in v.lower() for p in PLACEHOLDERS):
                return True
    return False


def wait_calc(panel_sht) -> bool:
    """写入 B1 后调用：触发重算 + 轮询直到 fetch 完成且区域稳定。返回是否收敛。

    关键：只要区域里还有 "Fetch..." 占位符就继续等（不被上一周旧值骗）。
    """
    panel_sht.book.app.calculate()
    time.sleep(MIN_WAIT)
    prev = None
    stable = 0
    start = time.time()
    while time.time() - start < TIMEOUT:
        cur = panel_sht.range(PANEL_RANGE).value
        if is_fetching(cur):          # 还在取数 → 重置，继续等
            stable = 0
            prev = None
            time.sleep(POLL)
            continue
        flat = tuple(tuple("" if v is None else v for v in row) for row in cur)
        if prev is not None and flat == prev and has_data(cur):
            stable += 1
            if stable >= STABLE_NEEDED:
                return True
        else:
            stable = 0
        prev = flat
        time.sleep(POLL)
    return False


# panel A3:X102 列序 → panel_data 字段（去掉 A 代码、B 日期后的 22 个）
FIELDS = [
    "fy1_np_avg", "fy1_eps", "fy1_instnum", "fy1_np_std", "fy1_np_median",
    "fy2_np_avg", "fy2_eps", "fy2_instnum", "fy2_np_std", "fy2_np_median",
    "close_hkd", "volume", "amount", "turn", "pe_ttm", "mkt_cap",
    "pb", "roe_fwd", "div_yield", "ev_ebitda", "nde", "profit_alert",   # S–X 估值因子
]


def _num(v):
    """转 float，无效值（None / 字符串错误）→ None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None  # Wind 错误字符串（如 "无数据"）丢弃


def save_panel(conn, update_date, trade_date, grid):
    rows = []
    for r in grid:
        code = r[0]
        if not code or not isinstance(code, str):
            continue
        vals = [_num(x) for x in r[2:24]]   # C..X = 22 字段
        rows.append((update_date, trade_date, code.strip(), *vals))
    conn.executemany(
        f"""INSERT OR REPLACE INTO panel_data
            (update_date, trade_date, wind_code, {','.join(FIELDS)})
            VALUES (?,?,?,{','.join(['?']*len(FIELDS))})""",
        rows,
    )
    return len(rows)


def save_benchmark(conn, trade_date, bench_sht):
    hstech = _num(bench_sht.range("B3").value)
    hsi = _num(bench_sht.range("B4").value)
    conn.execute(
        "INSERT OR REPLACE INTO benchmark (trade_date, hstech_close, hsi_close) VALUES (?,?,?)",
        (trade_date, hstech, hsi),
    )


def save_static(conn, update_date, static_sht):
    grid = static_sht.range(f"A{PANEL_FIRST_ROW}:E{PANEL_LAST_ROW}").value
    rows = []
    for r in grid:
        code = r[0]
        if not code or not isinstance(code, str):
            continue
        name, ind1, ind2, listd = r[1], r[2], r[3], r[4]
        if isinstance(listd, (datetime, date)):
            listd = listd.strftime("%Y-%m-%d")
        rows.append((code.strip(), name, ind1, ind2, str(listd) if listd else None, update_date))
    conn.executemany(
        """INSERT OR REPLACE INTO static_info
           (wind_code, name, industry_l1, industry_l2, list_date, update_date)
           VALUES (?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="回拉历史")
    ap.add_argument("--daily", action="store_true", help="每天增量（默认）")
    ap.add_argument("--from", dest="from_date", default="2021-01-04", help="回拉起始 YYYY-MM-DD")
    ap.add_argument("--resume", action="store_true", default=True, help="断点续采（默认开）")
    args = ap.parse_args()

    today = date.today()
    if args.backfill:
        start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        target = mondays(start, today)
    else:  # daily：仅本周一
        target = [today - timedelta(days=today.weekday())]

    update_date = today.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)

    done = existing_dates(conn) if args.resume else set()
    todo = [m for m in target if m.strftime("%Y-%m-%d") not in done]
    log.info(f"模式={'backfill' if args.backfill else 'daily'} | 目标 {len(target)} 周 | 已采 {len(done)} | 待采 {len(todo)}")

    if not todo:
        log.info("无待采日期，退出。")
        conn.close()
        return

    log.info(f"打开 {TEMPLATE} ...")
    # visible=True 必需：Wind 加载项(windfunc.xlam)的异步取数靠后台回调写回单元格，
    # 隐藏/关屏幕刷新的自动化 Excel 里该回调不触发，单元格会永远停在 "Fetch..."。
    app = xw.App(visible=True)
    app.display_alerts = False
    try:
        wb = app.books.open(str(TEMPLATE), update_links=False)
        wb.app.calculation = "automatic"
        panel = wb.sheets["panel"]
        bench = wb.sheets["benchmark"]
        static = wb.sheets["static_info"]

        # static_info 一次性
        try:
            panel.range("B1").value = todo[-1]      # 用最新日期触发 static 重算
            bench.range("B1").value = todo[-1]
            static.range("B1").value = todo[-1]
            wait_calc(panel)
            n = save_static(conn, update_date, static)
            conn.commit()
            log.info(f"static_info 已采 {n} 行")
        except Exception as e:
            log.warning(f"static_info 采集失败（可跳过）：{e}")

        ok = fail = 0
        for i, m in enumerate(todo, 1):
            ds = m.strftime("%Y-%m-%d")
            try:
                panel.range("B1").value = m
                bench.range("B1").value = m
                converged = wait_calc(panel)
                grid = panel.range(PANEL_RANGE).value
                nrows = save_panel(conn, update_date, ds, grid)
                save_benchmark(conn, ds, bench)
                ok += 1
                flag = "" if converged else " ⚠超时(仍入库)"
                if i % 10 == 0 or not converged:
                    log.info(f"[{i}/{len(todo)}] {ds} → {nrows} 行{flag}")
                if i % 20 == 0:
                    conn.commit()
            except Exception as e:
                fail += 1
                log.error(f"[{i}/{len(todo)}] {ds} 失败：{e}")
        conn.commit()
        log.info(f"完成：成功 {ok} / 失败 {fail}")
    finally:
        try:
            wb.close()
        except Exception:
            pass
        app.quit()
        conn.close()


if __name__ == "__main__":
    main()
