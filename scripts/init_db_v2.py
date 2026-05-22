"""初始化 V5 面板数据库（新表，与旧 weekly_data / static_indicators 并存）。

新表：
    panel_data       周频面板：FY1+FY2 一致预期 + 量价 + 市值
    static_info      静态信息：行业 / 上市日期
    benchmark        市场基准：恒生科技 / 恒生指数
    announce_events  财报公告事件（V6 用，本期建空表）

用法：
    uv run python scripts/init_db_v2.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "wind_history.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

SQL = """
CREATE TABLE IF NOT EXISTS panel_data (
    update_date TEXT NOT NULL,      -- 采集日期
    trade_date  TEXT NOT NULL,      -- 观测日（周一）
    wind_code   TEXT NOT NULL,
    -- FY1（当年）一致预期
    fy1_np_avg    REAL,             -- 净利润均值（百万）
    fy1_eps       REAL,
    fy1_instnum   REAL,             -- 覆盖机构数
    fy1_np_std    REAL,             -- 净利润标准差（百万）
    fy1_np_median REAL,             -- 净利润中值（百万）
    -- FY2（下一年）一致预期
    fy2_np_avg    REAL,
    fy2_eps       REAL,
    fy2_instnum   REAL,
    fy2_np_std    REAL,
    fy2_np_median REAL,
    -- 量价 + 市值
    close_hkd REAL,
    volume    REAL,
    amount    REAL,
    turn      REAL,
    pe_ttm    REAL,
    mkt_cap   REAL,                 -- 总市值（亿港币）
    PRIMARY KEY (update_date, trade_date, wind_code)
);

CREATE TABLE IF NOT EXISTS static_info (
    wind_code   TEXT PRIMARY KEY,
    name        TEXT,
    industry_l1 TEXT,
    industry_l2 TEXT,
    list_date   TEXT,
    update_date TEXT
);

CREATE TABLE IF NOT EXISTS benchmark (
    trade_date   TEXT PRIMARY KEY,
    hstech_close REAL,
    hsi_close    REAL
);

CREATE TABLE IF NOT EXISTS announce_events (
    wind_code      TEXT NOT NULL,
    report_period  TEXT NOT NULL,
    ann_date       TEXT,
    np_actual      REAL,
    revenue_actual REAL,
    eps_actual     REAL,
    surprise       REAL,
    PRIMARY KEY (wind_code, report_period)
);

CREATE INDEX IF NOT EXISTS idx_panel_code ON panel_data(wind_code);
CREATE INDEX IF NOT EXISTS idx_panel_date ON panel_data(trade_date);
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
    print(f"Tables: {tables}")
