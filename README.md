# tech100 — 香港科技100 Wind数据自动采集

香港科技100指数（HKEX Tech 100）成分股的一致预期与股价自动采集 + Streamlit展示。

## 项目结构

```
tech100/
├── pyproject.toml              # 项目配置与依赖（uv管理）
├── uv.lock                     # 依赖锁定文件（精确版本）
├── .python-version             # Python 版本锁定
├── README.md
├── config/
│   └── codes.csv               # 100只股票清单（wind_code, name）
├── data/
│   └── wind_history.db         # SQLite数据库（运行后生成）
├── templates/
│   ├── template.xlsx           # Wind Excel原始模板（公式引用B1）
│   └── template_5sheets.xlsx   # 5-Sheet并行模板
├── scripts/
│   ├── init_db.py              # 初始化数据库
│   ├── update_wind_data.py     # 单Sheet串行采集（20秒/只）
│   └── update_wind_data_5sheets.py  # 5-Sheet并行采集（10秒/5只）
├── app/
│   └── dashboard.py            # Streamlit看板
└── logs/                       # 采集日志
```

## 环境准备

```bash
# 激活 conda 环境
conda activate tech100

# 安装/同步依赖（基于 uv.lock）
uv sync

# 或以 editable 模式安装项目
uv pip install -e .
```

## 使用流程

### 1. 初始化数据库

```bash
uv run python scripts/init_db.py
```

### 2. 采集数据

**5-Sheet 并行（推荐，约5分钟）**

```bash
uv run python scripts/update_wind_data_5sheets.py
```

**单Sheet串行（约33分钟）**

```bash
uv run python scripts/update_wind_data.py
```

> 运行前确保 **Wind终端已登录**，且 `templates/template.xlsx` / `templates/template_5sheets.xlsx` 未被Excel占用。

### 3. 启动 Streamlit

```bash
uv run streamlit run app/dashboard.py
```

浏览器访问 `http://localhost:8501`

- **走势图**：100只股票，一行4个子图，蓝线=预测净利润（百万元），橙线=股价（HKD）
- **静态指标**：一致预期表格（机构家数、净利润预测等）

## 定时自动运行

macOS `crontab`（工作日 17:30）：

```bash
crontab -e
```

添加：

```
30 17 * * 1-5 cd /Users/macbook/Desktop/tech100 && /Users/macbook/miniconda3/envs/tech100/bin/uv run python scripts/update_wind_data_5sheets.py >> logs/cron.log 2>&1
```

## 依赖管理

```bash
# 添加新依赖
uv add 包名

# 添加开发依赖
uv add --dev 包名

# 更新锁定文件
uv lock

# 同步环境到锁定文件
uv sync
```

## 数据表说明

### static_indicators（一致预期静态指标）

| 字段 | 说明 |
|------|------|
| update_date | 采集日期 |
| wind_code | 股票代码 |
| inst_num_2025/2026 | 预测机构家数 |
| netprofit_avg_2025/2026 | 预测净利润平均值（百万元） |
| netprofit_max/min/median/std | 最大/最小/中位数/标准差 |

### weekly_data（每周时间序列）

| 字段 | 说明 |
|------|------|
| update_date | 采集日期（每日快照） |
| trade_date | 交易日期（模板A列每周日期） |
| netprofit_avg | 预测净利润平均值（2026年） |
| close_hkd | 收盘价（HKD） |
