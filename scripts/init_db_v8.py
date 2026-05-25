"""初始化 V8 日频研究看板新表(与 v7 周频表完全独立并存)。

新表(本脚本建):
    universe            universe 元数据(指数/行业代码 + 中文名 + 市场)
    universe_daily      日频面板:指数/行业本身的 PE/PB/EPS/股息率/收盘
    universe_metrics    后端算的滚动分位 + 变化(PE 5y 分位 / 4w EPS 修正 / YTD)
    macro_data          宏观 Top-down(GDP/CPI/PPI/PMI/零售/工业利润)
    panel_stock_daily   港股科技 100 日频价格估值(基本面/预期仍用 v7 周频 panel_data)

v7 已有表(不动一行):
    panel_data / benchmark / static_info / announce_events
    weekly_data / static_indicators (V1-4 遗留)

用法:
    uv run python scripts/init_db_v8.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "wind_history.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

SQL = """
-- 1. Universe 元数据
CREATE TABLE IF NOT EXISTS universe (
    universe_code  TEXT PRIMARY KEY,     -- 000300.SH / HSTECH.HI / 801080.SI
    name_cn        TEXT,
    market         TEXT,                  -- A / HK
    universe_type  TEXT,                  -- index / industry
    notes          TEXT
);

-- 2. 日频 universe 面板(指数/行业本身的数据,直接 Wind 拉)
CREATE TABLE IF NOT EXISTS universe_daily (
    trade_date     TEXT NOT NULL,
    universe_code  TEXT NOT NULL,
    close          REAL,                  -- 指数点位
    pe_ttm         REAL,
    pb             REAL,
    fy1_eps        REAL,                  -- 一致预期 FY1 EPS
    fy2_eps        REAL,
    div_yield      REAL,
    mkt_cap        REAL,                  -- 仅指数适用
    update_ts      TEXT,
    PRIMARY KEY (trade_date, universe_code)
);
CREATE INDEX IF NOT EXISTS idx_univ_daily_code ON universe_daily(universe_code);
CREATE INDEX IF NOT EXISTS idx_univ_daily_date ON universe_daily(trade_date);

-- 3. 滚动分位 / 变化(后端算)
CREATE TABLE IF NOT EXISTS universe_metrics (
    trade_date         TEXT NOT NULL,
    universe_code      TEXT NOT NULL,
    pe_percentile_5y   REAL,              -- PE 在 5 年的分位 0-100
    pb_percentile_5y   REAL,
    eps_chg_4w         REAL,              -- FY1 EPS 一致预期 4 周变化 %
    eps_chg_12w        REAL,
    return_4w          REAL,              -- 收盘 4 周收益 %
    return_ytd         REAL,
    PRIMARY KEY (trade_date, universe_code)
);

-- 4. 宏观数据(Top-down)
CREATE TABLE IF NOT EXISTS macro_data (
    period          TEXT NOT NULL,        -- 2026-04(月度) / 2026Q1(季度)
    indicator_code  TEXT NOT NULL,        -- M0000612 / M0017126 等 Wind EDB code
    indicator_name  TEXT,
    value           REAL,
    release_date    TEXT,                  -- 公布日(用于"今日新数据"提醒)
    PRIMARY KEY (period, indicator_code)
);

-- 5. 港股科技 100 日频价格估值(基本面/预期仍用 v7 panel_data 周频)
CREATE TABLE IF NOT EXISTS panel_stock_daily (
    trade_date  TEXT NOT NULL,
    wind_code   TEXT NOT NULL,
    close_hkd   REAL,
    volume      REAL,
    amount      REAL,
    turn        REAL,
    pe_ttm      REAL,
    pb          REAL,
    mkt_cap     REAL,
    div_yield   REAL,
    PRIMARY KEY (trade_date, wind_code)
);
CREATE INDEX IF NOT EXISTS idx_stk_daily_code ON panel_stock_daily(wind_code);
CREATE INDEX IF NOT EXISTS idx_stk_daily_date ON panel_stock_daily(trade_date);
"""

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SQL)
    conn.commit()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    conn.close()
    print(f"Database initialized: {DB_PATH}")
    print(f"All tables: {tables}")
    new = {"universe", "universe_daily", "universe_metrics", "macro_data", "panel_stock_daily"}
    print(f"V8 new tables present: {sorted(new & set(tables))}")
