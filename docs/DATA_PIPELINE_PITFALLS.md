# 数据管线踩坑录(Wind + xlwings + collect_panel)

> 适用范围:`scripts/collect_panel.py` + `templates/template_panel.xlsx` + Wind 个人版。
>
> **什么时候读这个文档**:
> - 跑 `collect_panel.py` 前(尤其是 backfill)
> - 看到回拉异常慢、单元格卡 "Fetch..."、单周入库 0 行时
> - 准备改模板字段、改"已采"判断口径前
>
> 维护:每踩一个新坑就加一条,带日期。

---

## 一、症状速查表

| 看到什么 | 翻到第几条 |
|---|---|
| `--daily` 本来 30s,突然变 1+ 小时 | §坑 1 已采口径 |
| 整个面板一直停在 "Fetch..." 不动 | §坑 2 visible / §坑 3 多实例 |
| 单元格变了,但数值还是上一周的 | §坑 4 陈旧缓存 |
| 跑了一会儿日志全是 ⚠超时 | §坑 5 MIN_WAIT/POLL |
| 新加字段后单周慢 3-5 倍 | §坑 6 慢函数 |
| 数值列里出现 "无数据" 等字符串 | §坑 7 错误字符串 |
| benchmark 列对不上日期 | §坑 8 多 sheet B1 |
| Mac 半夜断采,Excel 进程被挂起 | §坑 9 睡眠 |
| 新加字段 panel 全 0 / `#NAME?` | §坑 10 个人版授权 |

---

## 二、坑详情

### 坑 1：「已采」判断口径变了 → 全表回拉

**症状**:`--daily` 本应 30s 跑完一周,结果显示"待采 282 周"挂机一晚上。

**原因**:`existing_dates(conn)` 决定哪些日期跳过。一旦给它加新条件(例如"pb 非空才算已采"),旧版采过的所有日期都不再被视为已采,**整个数据库等于从零重跑**。

```python
# V5 老版（宽松）
return {r[0] for r in conn.execute("SELECT DISTINCT trade_date FROM panel_data")}

# V7 新版（严格,补估值因子时引入）
return {r[0] for r in conn.execute(
    "SELECT DISTINCT trade_date FROM panel_data WHERE pb IS NOT NULL")}
```

**修复**:**两种选择,提前想清楚**

- **A 走完整回拉**:接受重跑代价,挂机几小时。INSERT OR REPLACE 会把老 16 列一并重写——值应该一致,但要意识到此事。
- **B 写一次性补列脚本**:`ALTER TABLE` 加列 + 临时模板只跑新列 + `UPDATE` 回填。比 A 快 3-5×,但要写脚本。

**教训**:**改 `existing_dates` 之前先 grep 调用方**,并跑一句体检 SQL 确认对增量的影响:

```sql
SELECT COUNT(DISTINCT trade_date) FROM panel_data WHERE <新条件>;
-- 如果远小于总周数,说明改完会触发全表回拉
```

记录日期:2026-05-25

---

### 坑 2:Excel `visible=False` → 单元格永远 "Fetch..."

**症状**:headless 跑 xlwings,Wind 公式所有格子停在 "Fetch...",永远不刷新。

**原因**:Wind 加载项(`windfunc.xlam`)用 **VBA 异步回调**写回单元格。Excel 在隐藏窗口/关屏幕刷新/不可见状态下,该回调不触发或被吞。

**修复**:

```python
app = xw.App(visible=True)
app.display_alerts = False
# 不要 app.screen_updating = False
# 不要 app.calculation = "manual" 然后期待 calculate() 同步
```

**教训**:**Wind 公式不能 headless**。要省 CPU 就把窗口最小化(用 Windows 任务栏/macOS Dock),不要隐藏 Excel 实例本身。

---

### 坑 3:多个 Excel 实例 → Wind 只认其中一个

**症状**:打开两个 Excel,脚本新建的 `xw.App()` 拿到不带 Wind 的实例 → 公式不解析,返回 `#NAME?` 或永远 "Fetch..."。

**原因**:Wind 终端启动时只往**一个** Excel 实例注入加载项。后续新开的实例没有 Wind。

**修复**:复用已运行的实例,不要新建。

```python
apps = list(xw.apps)
if apps:
    app = apps[0]                       # 复用 Wind 连接的那个
    if len(apps) > 1:
        log.warning("多个实例,Wind 只服务一个,建议关掉其他")
else:
    app = xw.App(visible=True)          # 实在没有才新建
```

**教训**:开跑前 **Cmd+Tab / Alt+Tab 数一遍 Excel 图标**,只留一个。

---

### 坑 4:陈旧缓存 → 读到上一周的数

**症状**:写完 B1 立刻读,数值看起来正常,但其实是上一日期的结果——下次再读才换。

**原因**:Wind 异步取数没回来时,**Excel 单元格里残留的是上次重算的旧值**(不是空,不是 "Fetch...")。简单的"非空就读"会被骗。

**修复 — wait_calc 双保险**:

1. **占位符检测**:扫区域看有没有 `fetch / 提取 / 请求 / loading / 计算中 / 正在` 这些字符串,有就继续等并周期性 nudge 重算。
2. **防陈旧对比**:保留上一日期入库时的快照 `prev_flat`,当前读数必须 ≠ 上一日期 才接受。

```python
if prev_flat is not None and flat == prev_flat:
    # 与上一日期完全一样 = 还没换 → 继续等
    continue
```

**教训**:Wind 异步取数有**双重不确定性**(占位符 + 缓存值),只靠一种判断会漏。

---

### 坑 5:`MIN_WAIT / POLL` 调太小 → 空转 + 误超时

**症状**:把 `MIN_WAIT 5→3s`、`POLL 1.5→1.0s` 想跑快点,反而出现一堆 ⚠超时,或者数据稀疏。

**原因**:Wind 启动 fetch 本身就要 3-5s,提前 poll 看到的是空/陈旧,wait_calc 的稳定计数被重置,空转后才进入真正等待。

**修复 — 保守参数**:

```python
MIN_WAIT = 5.0       # 写入后最小等待
POLL     = 1.5       # 轮询间隔
STABLE_NEEDED = 2    # 连续 N 次一致才算收敛
TIMEOUT  = 600       # 单周最大等 10 分钟
```

**教训**:**Wind 是慢异步系统,不要按本地计算的直觉调参**。要快只能从"少跑日期"(增量、断点续采)着手,不是从"等少点"。

---

### 坑 6:加字段后单周翻倍慢 → 慢 Wind 函数

**症状**:模板从 16 字段加到 22 字段,单周从 ~30s 变 ~80s。

**原因**:不是字段数量,是**字段类别**。下面这些函数本身慢:

| 类别 | 例子 | 为什么慢 |
|---|---|---|
| NTM/Forward 一致预期 | `s_west_avgroe`、`s_west_eps`(FY2) | 加权 FY1+FY2,后端临时算 |
| 财务复合估值 | `s_val_ev2_to_ebitda`、`s_val_evtoebitda` | EV 要叠加债务/现金/少数股东 |
| 报表派生 | `s_fa_debttoequity` | 走 `s_stm*` 链路,需财报披露日 |
| 文本型 | `s_west_profitnotice`(盈警) | 个人版常无授权,长时 fetch 后才返回错误 |

**对比快函数**:`s_dq_close / volume / amount / turn / pe_ttm`、`s_val_pb / s_val_mv`、`s_west_netprofit`(FY1)——都是预聚合好的快照。

**修复**:加字段前**单周计时**:

```fish
# 在 panel sheet 的空白区先贴 6 个新公式,B1 写今天,手动 Cmd+= 触发,看几秒返回
# 如果 >10s,考虑能不能用替代字段或后端派生
```

**教训**:模板加列前**先抽样测速**,不要等回拉跑了 2 小时才发现来不及。

---

### 坑 7:Wind 错误字符串污染数值列

**症状**:`SELECT pe_ttm FROM panel_data` 返回 `'无数据'`、`'#N/A'`,聚合 SQL 报错。

**原因**:Wind 公式失败时返回**中文/字母错误字符串**,直接 `executemany` 进库时 SQLite 把整列降级成 TEXT。

**修复 — `_num()` 滤一道**:

```python
def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None       # 字符串错误一律丢成 NULL
```

**教训**:**任何外部数据源进库前都过一遍类型转换**,不要相信"应该是数字"。

---

### 坑 8:多 sheet 只写一个 B1 → benchmark/static 没跟上

**症状**:panel 数据正确,但 benchmark 表的 hstech/hsi 还停在上周值。

**原因**:模板里 panel / benchmark / static_info **各自有自己的 B1 单元格**,只写一个只触发那一张 sheet 的重算。

**修复**:循环里一起写。

```python
panel.range("B1").value  = m
bench.range("B1").value  = m
# static_info 例外:只在循环外写一次(它不随日期变,见下条)
```

**教训**:**模板做单元测试时不要只盯一张 sheet**,改 B1 后翻所有 sheet 看是否同步。

---

### 坑 9:Mac 睡眠 / Excel 被挂起 → 半夜断采

**症状**:晚上 10 点启动 backfill,早上发现卡在某一周不动,Excel 进程"灰色"。

**原因**:macOS 接电源时仍可能进入 App Nap / 显示器睡眠,GUI 应用的事件循环被节流。Wind 异步回调进不来。

**修复**:

```fish
# 命令行临时禁睡眠(整个 backfill 过程挂着)
caffeinate -dimsu &
set CAFFEINATE_PID $last_pid
uv run python scripts/collect_panel.py --backfill --from 2021-01-04
kill $CAFFEINATE_PID
```

或者 GUI:**系统设置 → 电池 → 接电源 → 防止显示器关闭**。

**教训**:**任何 >30 分钟挂机任务**,先 `caffeinate` 再开始。

---

### 坑 10:新字段在个人版无授权 → 长时 fetch 后才返回 `#NAME?`

**症状**:加了 `s_fa_debttoequity` / `s_west_profitnotice`,单周等了 2 分钟最后入库全是 NULL。

**原因**:Wind 个人版授权范围 < 机构版。某些 `s_fa_*` / `s_west_*` 文本类公式不存在或无授权,Wind 不会立刻报错,会先 fetch 几十秒才返回错误字符串。

**修复 — 加字段前的 13 公式抽测(见 `DATA_PROCUREMENT.md §10`)**:

1. 在 panel sheet 空白处单独贴公式
2. B1 = 0700.HK(腾讯,覆盖最高)、$A$1=`=TODAY()`
3. 三种结果:
   - ✅ 数字返回 → 保留
   - 0/`#N/A` → 字段名错或权限无,**砍掉这列**(本次 nde/profit_alert 就是这下场)
   - 字符串文本 → 看 `_num()` 是否兼容

**教训**:**任何"听起来理所当然"的 Wind 函数都先单测**。本次回顾:nde 和 profit_alert 浪费了一轮回拉。

记录日期:2026-05-25,详见 `DATA_PROCUREMENT.md §13.3`

---

## 三、每周更新 SOP

正常情况下每周一次,15 秒命令:

```fish
# 1. 体检(可选,看上周是否已采)
sqlite3 data/wind_history.db "SELECT MAX(trade_date), COUNT(DISTINCT trade_date) FROM panel_data;"

# 2. 防睡眠 + 跑增量
caffeinate -dimsu uv run python scripts/collect_panel.py --daily
```

跑之前 30 秒的预检:

- [ ] Wind 终端右下角连接灯**绿色**
- [ ] **只一个** Excel 实例(Cmd+Tab 数一下)
- [ ] `templates/template_panel.xlsx` 已打开(脚本会复用)
- [ ] Mac 接电源,显示器不睡眠

如果遇到 ⚠超时/0 行入库 → 翻 §一 速查表对症。

---

## 四、改"已采"判断 / 加字段前的体检脚本

放在 `scripts/check_pipeline_health.py`(待补)或临时跑:

```fish
sqlite3 data/wind_history.db <<EOF
.headers on
.mode column
-- 总览
SELECT COUNT(*) AS total_rows,
       COUNT(DISTINCT trade_date) AS total_weeks,
       MIN(trade_date) AS earliest,
       MAX(trade_date) AS latest
FROM panel_data;

-- 每列非空率(改字段前看这个,判断"已采"口径)
SELECT
  ROUND(100.0*COUNT(close_hkd) /COUNT(*),1) AS close_pct,
  ROUND(100.0*COUNT(pe_ttm)    /COUNT(*),1) AS pe_pct,
  ROUND(100.0*COUNT(pb)        /COUNT(*),1) AS pb_pct,
  ROUND(100.0*COUNT(roe_fwd)   /COUNT(*),1) AS roe_pct,
  ROUND(100.0*COUNT(div_yield) /COUNT(*),1) AS div_pct,
  ROUND(100.0*COUNT(ev_ebitda) /COUNT(*),1) AS ev_pct
FROM panel_data;
EOF
```

如果某列非空率 << 100%,说明对它做 `existing_dates` 过滤会触发大量回拉——参考 §坑 1。

---

*版本:v1.0 ｜ 创建:2026-05-25 ｜ 维护:每踩一个新坑追加一条*
