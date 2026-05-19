"""初始化 SQLite 数据库。"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "wind_history.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

SQL = """
CREATE TABLE IF NOT EXISTS static_indicators (
    update_date TEXT NOT NULL,
    wind_code TEXT NOT NULL,
    name TEXT,
    inst_num_2025 REAL,
    netprofit_avg_2025 REAL,
    netprofit_max_2025 REAL,
    netprofit_min_2025 REAL,
    netprofit_median_2025 REAL,
    netprofit_std_2025 REAL,
    inst_num_2026 REAL,
    netprofit_avg_2026 REAL,
    netprofit_max_2026 REAL,
    netprofit_min_2026 REAL,
    netprofit_median_2026 REAL,
    netprofit_std_2026 REAL,
    PRIMARY KEY (update_date, wind_code)
);

CREATE TABLE IF NOT EXISTS weekly_data (
    update_date TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    wind_code TEXT NOT NULL,
    name TEXT,
    netprofit_avg REAL,
    close_hkd REAL,
    PRIMARY KEY (update_date, trade_date, wind_code)
);

CREATE INDEX IF NOT EXISTS idx_wd_update ON weekly_data(update_date);
CREATE INDEX IF NOT EXISTS idx_wd_code ON weekly_data(wind_code);
CREATE INDEX IF NOT EXISTS idx_wd_date ON weekly_data(trade_date);
"""

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SQL)
    conn.commit()
    conn.close()
    print(f"Database initialized: {DB_PATH}")
