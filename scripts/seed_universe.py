"""填 universe 元数据(24 个指数/行业)。

V1 范围:
- A 股 4 指数 + 12 A 股一级行业(申万)
- 港股 5 指数 + 8 港股行业指数(恒生综合分类)
- 共 29 行(若 P0 验证失败可裁减)

用法:
    uv run python scripts/seed_universe.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "wind_history.db")

# (code, name_cn, market, universe_type, notes)
UNIVERSE = [
    # === A 股指数(4 个)===
    ("000300.SH", "沪深 300",   "A",  "index", "大盘核心宽基"),
    ("000905.SH", "中证 500",   "A",  "index", "中盘"),
    ("000852.SH", "中证 1000",  "A",  "index", "小盘"),
    ("000016.SH", "上证 50",    "A",  "index", "超大盘 / 蓝筹"),

    # === A 股申万一级行业(V1 取她 xlsx 列出的 12 个,V2 补齐其余 19 个)===
    ("801010.SI", "申万 农林牧渔", "A", "industry", ""),
    ("801030.SI", "申万 基础化工", "A", "industry", ""),
    ("801040.SI", "申万 钢铁",     "A", "industry", ""),
    ("801050.SI", "申万 有色金属", "A", "industry", ""),
    ("801080.SI", "申万 电子",     "A", "industry", ""),
    ("801110.SI", "申万 家用电器", "A", "industry", ""),
    ("801150.SI", "申万 医药生物", "A", "industry", ""),
    ("801160.SI", "申万 公用事业", "A", "industry", ""),
    ("801170.SI", "申万 交通运输", "A", "industry", ""),
    ("801180.SI", "申万 房地产",   "A", "industry", ""),
    ("801200.SI", "申万 商贸零售", "A", "industry", ""),
    ("801780.SI", "申万 银行",     "A", "industry", ""),

    # === 港股指数(5 个)===
    ("HSI.HI",      "恒生指数",         "HK", "index", "港股大盘"),
    ("HSTECH.HI",   "恒生科技",         "HK", "index", "港股科技 30(tech100 自家基准)"),
    ("HSCEI.HI",    "恒生中国企业",     "HK", "index", "国企指数"),
    ("HSCI.HI",     "恒生综合",         "HK", "index", "全港股综合"),
    ("HSCNCI.HI",   "恒生消费",         "HK", "index", "消费板块"),

    # === 港股恒生综合行业(8 个,P0 验证后可能调整代码)===
    ("HSCIIN10.HI", "恒生综合 能源业",    "HK", "industry", "代码待 P0 验证"),
    ("HSCIIN15.HI", "恒生综合 原材料业",  "HK", "industry", "代码待 P0 验证"),
    ("HSCIIN20.HI", "恒生综合 工业",      "HK", "industry", "代码待 P0 验证"),
    ("HSCIIN25.HI", "恒生综合 非必需消费","HK", "industry", "代码待 P0 验证"),
    ("HSCIIN30.HI", "恒生综合 必需消费",  "HK", "industry", "代码待 P0 验证"),
    ("HSCIIN35.HI", "恒生综合 医疗保健",  "HK", "industry", "代码待 P0 验证"),
    ("HSCIIN40.HI", "恒生综合 金融业",    "HK", "industry", "代码待 P0 验证"),
    ("HSCIIN45.HI", "恒生综合 资讯科技业","HK", "industry", "代码待 P0 验证"),
]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "INSERT OR REPLACE INTO universe (universe_code, name_cn, market, universe_type, notes) VALUES (?,?,?,?,?)",
        UNIVERSE,
    )
    conn.commit()
    n_a_idx = sum(1 for u in UNIVERSE if u[2] == "A" and u[3] == "index")
    n_a_ind = sum(1 for u in UNIVERSE if u[2] == "A" and u[3] == "industry")
    n_hk_idx = sum(1 for u in UNIVERSE if u[2] == "HK" and u[3] == "index")
    n_hk_ind = sum(1 for u in UNIVERSE if u[2] == "HK" and u[3] == "industry")
    print(f"已插入 universe {len(UNIVERSE)} 行")
    print(f"  A 股: 指数 {n_a_idx} + 行业 {n_a_ind}")
    print(f"  港股: 指数 {n_hk_idx} + 行业 {n_hk_ind}")
    conn.close()


if __name__ == "__main__":
    main()
