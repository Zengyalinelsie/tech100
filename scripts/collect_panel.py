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
from tqdm import tqdm

ROOT = Path(__file__).parent.parent.resolve()
TEMPLATE = ROOT / "templates" / "template_panel.xlsx"
DB_PATH = ROOT / "data" / "wind_history.db"
LOG_DIR = ROOT / "logs"

N_STOCKS = 100
PANEL_FIRST_ROW = 3
PANEL_LAST_ROW = PANEL_FIRST_ROW + N_STOCKS - 1      # 102
PANEL_RANGE = f"A{PANEL_FIRST_ROW}:X{PANEL_LAST_ROW}"  # 代码+日期+22字段(16旧+6估值)
SENTINEL_CELL = f"X{PANEL_LAST_ROW}"                  # 最后一只盈警

MIN_WAIT = 5.0       # 写入后最小等待（让 Wind 启动 fetch，单元格变 "Fetch..."）
POLL = 1.5           # 轮询间隔
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
    # 只把"新估值列已填(pb 非空)"的日期算作已采 —— 使本次估值因子回拉能补全旧日期的新列，
    # 同时仍可断点续采(已补全 pb 的日期会跳过)。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(panel_data)")}
    if "pb" in cols:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM panel_data WHERE pb IS NOT NULL")}
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


def _flat(grid):
    return tuple(tuple("" if v is None else v for v in row) for row in grid)


def wait_calc(panel_sht, prev_flat=None) -> bool:
    """写入 B1 后调用：触发重算 + 轮询直到 fetch 完成且区域稳定。返回是否收敛。

    关键：
    - 只要区域里还有 "Fetch..." 占位符就继续等（不被上一周旧值骗），并每隔几秒再 nudge
      一次重算 —— Wind 异步取数有时需要反复触发才回填。
    - 防陈旧：复用已打开的模板时,换日期后旧缓存值仍在,会被误判为"已稳定"。故要求当前
      读数必须与"上一个日期的结果(prev_flat)"不同(收盘价等周周不同)才接受。
    """
    app = panel_sht.book.app
    app.calculate()
    time.sleep(MIN_WAIT)
    prev = None
    stable = 0
    start = time.time()
    last_calc = time.time()
    while time.time() - start < TIMEOUT:
        cur = panel_sht.range(PANEL_RANGE).value
        if is_fetching(cur):          # 还在取数 → 每 5s 再触发一次重算,继续等
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
        if prev_flat is not None and flat == prev_flat:   # 与上一日期相同 → 还是旧缓存,继续等
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
    # ★ 复用已打开的 Excel 实例（Wind 只对某一个实例连接并自动取数；新开实例会永远停在
    #   "Fetch..."）。优先用已在运行的 Excel，并尽量用其中已打开的模板；都没有才新建。
    apps = list(xw.apps)
    created_app = False
    if apps:
        app = apps[0]
        log.info(f"复用已打开的 Excel 实例(Wind 连接的那个) — 共 {len(apps)} 个实例")
        if len(apps) > 1:
            log.warning("检测到多个 Excel 实例；Wind 只服务其中一个,建议只留一个再跑。")
    else:
        app = xw.App(visible=True)
        created_app = True
        log.info("未发现运行中的 Excel,新建一个实例")
    app.display_alerts = False

    # 找已打开的模板;没有才打开
    wb = None
    for b in list(app.books):
        try:
            if Path(b.fullname).name == TEMPLATE.name:
                wb = b
                log.info("使用已打开的 template_panel.xlsx")
                break
        except Exception:
            pass
    opened_wb = False
    if wb is None:
        wb = app.books.open(str(TEMPLATE), update_links=False)
        opened_wb = True
    try:
        wb.app.calculation = "automatic"
        panel = wb.sheets["panel"]
        bench = wb.sheets["benchmark"]
        static = wb.sheets["static_info"]

        # static_info 一次性
        last_flat = None
        try:
            panel.range("B1").value = todo[-1]      # 用最新日期触发 static 重算
            bench.range("B1").value = todo[-1]
            static.range("B1").value = todo[-1]
            wait_calc(panel)
            last_flat = _flat(panel.range(PANEL_RANGE).value)   # 基线,供首个日期防陈旧
            n = save_static(conn, update_date, static)
            conn.commit()
            log.info(f"static_info 已采 {n} 行")
        except Exception as e:
            log.warning(f"static_info 采集失败（可跳过）：{e}")

        ok = fail = 0
        pbar = tqdm(todo, desc="采集", unit="周", dynamic_ncols=True)
        for i, m in enumerate(pbar, 1):
            ds = m.strftime("%Y-%m-%d")
            try:
                panel.range("B1").value = m
                bench.range("B1").value = m
                converged = wait_calc(panel, prev_flat=last_flat)
                grid = panel.range(PANEL_RANGE).value
                last_flat = _flat(grid)
                nrows = save_panel(conn, update_date, ds, grid)
                save_benchmark(conn, ds, bench)
                ok += 1
                pbar.set_postfix_str(f"{ds} rows={nrows} ok={ok} fail={fail}{'' if converged else ' ⚠超时'}")
                if not converged:
                    log.warning(f"[{i}/{len(todo)}] {ds} 超时(仍入库)")
                if i % 20 == 0:
                    conn.commit()
            except Exception as e:
                fail += 1
                pbar.set_postfix_str(f"{ds} FAIL ok={ok} fail={fail}")
                log.error(f"[{i}/{len(todo)}] {ds} 失败：{e}")
        pbar.close()
        conn.commit()
        log.info(f"完成：成功 {ok} / 失败 {fail}")
    finally:
        # 只清理自己新建/打开的;复用用户的实例与模板则保持不动
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
