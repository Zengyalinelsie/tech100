# Wind 数据采购清单 v2.1

> 配套文档：[CHARTER.md](./CHARTER.md) ｜ 实物模板：`templates/template_panel.xlsx`
>
> ## ⭐ v2.1 关键变化（2026-05-22，当前生效版本）
>
> **采集架构改为「面板快照」** —— 取代旧的「逐股时间序列」：
> - **panel sheet = 100 行 × 18 列**（100 只股票 × 单日全字段），靠 `panel!B1` 日期参数驱动
> - 代码列 `=_meta!A2..A101` 引用统一代码清单，**一次重算出 100 家当日全部字段**
> - 每日增量：`B1`=今天，读 1 次；历史回拉：脚本循环交易日改 `B1`，每次读 100 行
> - 回拉耗时从旧的 ~5 小时降到 **~1.8 小时**
>
> **公式校正**（基于用户 Wind 个人版实测）：
> - 滚动 PE：~~`s_dq_pe_ttm`~~ → **`s_val_pe_ttm`** ✅
> - 总市值：从静态表**移到 panel 每日列**（市值 = 股价×股本，是动态数据）→ `s_val_mv($A3,$B3)`
> - 公告日：~~`s_stm_issuingdate($B$1, n)`~~ → **`s_stm_issuingdate($B$1, 报告期)`**，第 2 参数是报告期截止日 `YYYY/MM/DD`（如 2024/12/31=年报、2024/06/30=中报），港股返回业绩公告披露日 ✅
> - 实际净利润：`s_stm07_净利润(...)` 系列（待用户补完整公式），报表类型参数 1=合并报表
>
> ## v2 相比 v1 的核心变化（仍然成立）
> 1. 公式从「固定 FY2026」改为「**滚动 FY1 + FY2**」（用 `YEAR($A11)` / `YEAR($A11)+1` 动态算）
> 2. 频率从「周频」改为「**日频**」
> 3. 第一性原理章节 + 公式逐参数解读章节
>
> **设计前提**：
> 1. Wind 个人版终端（无 WSD / WindAPI 权限）
> 2. 只能用 Excel 公式族：`s_west_*` / `s_dq_*` / `s_val_*` / `s_info_*` / `s_stm*`
> 3. **面板模式**：`panel!B1` = 日期参数、A 列 = 100 代码（引用 `_meta`）、每行字段公式用 `$A{行}` 代码 + `$B{行}` 日期
> 4. 采集脚本循环日期写 `B1` + 等 Wind 重算 + 读 `A3:R102` 入库（取代旧的逐股 B1 替换）
>
> > ⚠ 注：下文「Sheet 1 daily_consensus」等章节是 v2 的**逐股时间序列**设计，已被 panel 架构取代。保留作背景参考；实际以 `template_panel.xlsx` 的 4 sheet（`_meta`/`panel`/`static_info`/`benchmark`）为准。

---

## 〇、Panel 快照架构（v2.1 当前实现）

`templates/template_panel.xlsx` 4 个 sheet：

| Sheet | 内容 | 行数 |
|---|---|---|
| `_meta` | 100 代码 + 名称（单一真相源，从 codes.csv 同步） | 100 |
| `panel` | **核心**：B1 日期参数；A3:A102 代码；C3:R3 起 16 字段公式 | 100 数据行 |
| `static_info` | 真静态：代码 / 公司名 / 行业 / 上市日期（不含市值） | 100 |
| `benchmark` | HSTECH.HI + HSI.HI，B1 日期参数驱动 | 2 |

**panel 的 16 字段（C-R 列）**：

| 列 | 字段 | 公式（第 3 行） |
|---|---|---|
| C | FY1 净利润均值 | `=[1]!s_west_netprofit($A3,YEAR($B3),$B3,180,1000000)` |
| D | FY1 EPS | `=[1]!s_west_eps($A3,YEAR($B3),$B3,180)` |
| E | FY1 机构数 | `=[1]!s_west_instnum_np($A3,YEAR($B3),$B3,180)` |
| F | FY1 标准差 | `=[1]!s_west_stdnetprofit($A3,YEAR($B3),$B3,180,1000000)` |
| G | FY1 中值 | `=[1]!s_west_mediannetprofit($A3,YEAR($B3),$B3,180,1000000)` |
| H | FY2 净利润均值 | `=[1]!s_west_netprofit($A3,YEAR($B3)+1,$B3,180,1000000)` |
| I-L | FY2 EPS/机构数/标准差/中值 | 同 D-G 但 `YEAR($B3)+1` |
| M | 收盘价 | `=[1]!s_dq_close($A3,$B3,3)` |
| N | 成交量 | `=[1]!s_dq_volume($A3,$B3)` |
| O | 成交额 | `=[1]!s_dq_amount($A3,$B3)` |
| P | 换手率 | `=[1]!s_dq_turn($A3,$B3)` |
| Q | 滚动 PE | `=[1]!s_val_pe_ttm($A3,$B3)` |
| R | 总市值（动态） | `=[1]!s_val_mv($A3,$B3)` |

**待用户验证**：①`[1]!` 外部引用是否被 Wind 识别 ②`s_val_mv` 返回值量级（决定是否加单位参数）③`static_info` 行业/上市日期函数名。

---

## 一、第一性原理 — 为什么这样设计

### 1.1 我们真正要测量的变量是什么

不是 F（盈利预期水平），而是 **ΔF**（**预期变化**）。

```
ΔF_t = F_t − F_{t-1}
```

这是因为：**领先-滞后分析关心"信息更新"，不关心"绝对水平"**。例如腾讯 FY2025 净利润预期 = 2300 亿这件事不重要，重要的是"这周从 2270 亿调到了 2300 亿"。

### 1.2 ΔF 要在不同时点之间可比

如果 F_t 和 F_{t-1} 是"对不同对象的预期"，ΔF 就没意义。

举例对比：

```
情景 A（错的）— 固定预测年度 FY2026
  2021-01-15 观测：F = "分析师对 2026 年盈利预期" = ??? (5 年后，覆盖率低)
  2024-01-15 观测：F = "分析师对 2026 年盈利预期" = X 亿 (2 年后)
  2026-01-15 观测：F = "分析师对 2026 年盈利预期" = Y 亿 (当年实际值)

  ΔF 跨这三个时点不可比 — 因为"对 2026 看多远"完全不同
  早年大概率拿不到数据
```

```
情景 B（对的）— 滚动 FY1（当年）
  2021-01-15 观测：F = "对 FY2021 的预期"
  2024-01-15 观测：F = "对 FY2024 的预期"
  2026-01-15 观测：F = "对 FY2026 的预期"

  每个时点的"地平线"一致（都是当年），ΔF 可比
  分析师永远在覆盖当年，数据覆盖率最高
```

### 1.3 为什么同时拉 FY1 + FY2

| 指标 | 优势 | 劣势 |
|------|------|------|
| **FY1（当年）** | 覆盖率最高（每只股每天都有） | 年内会"机械收敛"—— 越接近年末，预期越接近实际值，预期变化信号衰减 |
| **FY2（下一年）** | 不受当期公告"机械修正"影响，是更纯的预期信号 | 覆盖率次之，特别是早年小盘股可能缺失 |

**对照实证**：学术里的 IBES / Frankel-Lee 一类研究里，FY2 预期修正是分析师预期信号的金标准；FY1 预期更适合做"近端跟踪"。两个都拉，分析时各取所长。

### 1.4 频率为什么用日频

一致预期理论上是慢变量（机构报告非每日发布），但选日频是因为：

1. **保留事件精确性**：财报公告日是事实点，日频能保留 t=0 的精度；周频会模糊 ±2 天
2. **与价格同频**：股价就是日频，预期日频后两个变量天然对齐，不需要 resample 假设
3. **下游灵活性**：日频可以聚合成周/月做分析，反之不行
4. **存储成本可控**：5 年日频 100 股 ≈ 13 万行 / 15 MB SQLite，完全可承受

**取舍**：单股 Excel 重算耗时从 60s 上升到 ~180s。首次回拉 100 股需 3-5 小时（一晚跑完），之后每日增量仍是 ~20 分钟。

---

## 二、总览

| Sheet | 优先级 | 行数 | 字段数 | 解决什么问题 |
|---|---|---|---|---|
| 1. `daily_consensus` | 🔴 P0 | ~1300 | 11 | Layer 1/2/3 的 X 变量（盈利预期 FY1 + FY2）+ 股价 |
| 2. `daily_price_vol` | 🔴 P0 | ~1300 | 6 | 量价 / 估值控制变量 |
| 3. `static_info` | 🟡 P1 | 100 | 6 | 行业切片、市值加权 |
| 4. `announce_events` | 🟡 P1 | ~2000 | 5 | V6 PEAD 升级（财报公告日为真实事件） |
| 5. `benchmark` | 🟡 P1 | ~1300 | 3 | 超额收益正确基准 |

**`_meta` sheet**（不进数据库）：在 template.xlsx 加一个隐藏 sheet 列出 100 个代码 + 名称，作为人类阅读参考；`update_wind_data.py` 仍从 `config/codes.csv` 读，保持现有机制。

---

## 三、🔴 P0 必拿 — V5 范围内一定要拿

### Sheet 1：`daily_consensus` — 一致预期日序列（11 列）

**布局**：A 列日频日期（A11:A1311，约 1300 行 ≈ 5 年），B-L 列 11 个字段。

| 列 | 字段中文 | Wind 公式 |
|---|---|---|
| A | 观测日期 | A11 = `=TODAY()-IF(WEEKDAY(TODAY(),2)=6,1,IF(WEEKDAY(TODAY(),2)=7,2,0))`<br>A12:A1311 = `=A11-1`（日频，逐日往前） |
| B | **FY1** 净利润均值（百万） | `=[1]!s_west_netprofit($B$1,YEAR($A11),$A11,180,1000000)` |
| C | **FY1** EPS | `=[1]!s_west_eps($B$1,YEAR($A11),$A11,180)` |
| D | **FY1** 覆盖机构数 | `=[1]!s_west_instnum_np($B$1,YEAR($A11),$A11,180)` |
| E | **FY1** 净利润标准差（百万） | `=[1]!s_west_stdnetprofit($B$1,YEAR($A11),$A11,180,1000000)` |
| F | **FY1** 净利润中值（百万） | `=[1]!s_west_mediannetprofit($B$1,YEAR($A11),$A11,180,1000000)` |
| G | **FY2** 净利润均值（百万） | `=[1]!s_west_netprofit($B$1,YEAR($A11)+1,$A11,180,1000000)` |
| H | **FY2** EPS | `=[1]!s_west_eps($B$1,YEAR($A11)+1,$A11,180)` |
| I | **FY2** 覆盖机构数 | `=[1]!s_west_instnum_np($B$1,YEAR($A11)+1,$A11,180)` |
| J | **FY2** 净利润标准差（百万） | `=[1]!s_west_stdnetprofit($B$1,YEAR($A11)+1,$A11,180,1000000)` |
| K | **FY2** 净利润中值（百万） | `=[1]!s_west_mediannetprofit($B$1,YEAR($A11)+1,$A11,180,1000000)` |
| L | 日收盘价（HKD） | `=[1]!s_dq_close($B$1,$A11,3)` |

### Sheet 1 公式逐参数解读

以 B 列公式为例：

```
=[1]!s_west_netprofit( $B$1 , YEAR($A11) , $A11 , 180 , 1000000 )
                        ↑        ↑          ↑      ↑       ↑
                        ①        ②          ③      ④       ⑤
```

| # | 参数 | 含义 | 当前取值 |
|---|------|------|---------|
| ① | `$B$1` | **股票代码**。绝对引用，update 脚本逐股替换 | 0700.HK / 3690.HK / ... |
| ② | `YEAR($A11)` | **预测年度**（关键 — v2 改造点）。Excel 的 YEAR 函数从 A11 日期里提取年份 | A11=2024 年某日 → 2024；A11=2026 年某日 → 2026 |
| ③ | `$A11` | **观测日期**。所有公式查的是"在这一天，分析师对【②的年度】的预期是多少" | A11=2024-01-15 + ②=2024 → "2024-01-15 那天对 2024 年的预期" |
| ④ | `180` | **统计窗口（天）**。取观测日往前 180 天内所有分析师报告的均值 | 180 = 6 个月，标准选择（30 天太短噪声大、360 天太长含老预测） |
| ⑤ | `1000000` | **单位换算**。Wind 原始单位是"元"，除以 1000000 换算成"百万元" | 仅 netprofit / std / median / max / min 有此参数；EPS / instnum 无 |

**FY2 公式只改 ②**：`YEAR($A11)+1` —— 拉"对下一年的预期"。

**为什么不用 NTM（next twelve months）**：Wind 个人版的 `s_west_*` 函数固定要求"年度"参数，没有现成的 NTM 函数。NTM 需要后端 Python 加权 FY1 + FY2（按到年末剩余天数加权）算出来，这是 V6 阶段事情。

### Sheet 2：`daily_price_vol` — 量价日序列（6 列）

**为什么必要**：当前 dashboard 只有 `close`，没有量、换手、估值。这些是 V7 因子化的控制变量，也用于检验"信号是否被高换手稀释"。

| 列 | 字段中文 | Wind 公式 |
|---|---|---|
| A | 观测日期 | 同 Sheet 1 |
| B | 日收盘 (HKD) | `=[1]!s_dq_close($B$1,$A11,3)` （与 Sheet1 L 列同，可共用） |
| C | 日成交量 | `=[1]!s_dq_volume($B$1,$A11)` |
| D | 日成交额 | `=[1]!s_dq_amount($B$1,$A11)` |
| E | 日换手率 (%) | `=[1]!s_dq_turn($B$1,$A11)` |
| F | 滚动 PE | `=[1]!s_dq_pe_ttm($B$1,$A11)` 或 `s_val_pe_ttm` |

**⚠ 待验证**：`s_dq_volume` / `s_dq_amount` / `s_dq_turn` / `s_dq_pe_ttm` 这 4 个函数名在 Wind 个人版的实际可用性 — 见本文档第七章「验证清单」第 8-11 条。

---

## 四、🟡 P1 应拿 — V6 范围（方法学升级用）

### Sheet 3：`static_info` — 静态信息（每股一行）

**做什么用**：行业切片分析 + 市值加权 + 描述性信息。每只股票采集一次，**不需要时间序列**。

| 字段中文 | Wind 公式 | 说明 |
|---|---|---|
| 公司名称 | `=[1]!S_INFO_NAME($B$1)` | ✅ 已有 |
| 申万一级行业 | `=[1]!s_info_industry($B$1,"申万一级")` | 100 股分到 5-8 个行业 |
| 申万二级行业 | `=[1]!s_info_industry($B$1,"申万二级")` | 更细 |
| GICS 行业 | `=[1]!s_info_gicssector($B$1)` | 国际标准（港股更常用） |
| 总市值 (亿港币) | `=[1]!s_val_mv($B$1,$D$1,100000000)` | 按今天市值（D$1 = TODAY()） |
| 上市日期 | `=[1]!s_info_listdate($B$1)` | 过滤上市 < 2 年的样本 |

> **风险提示**：港股的"申万行业"在 Wind 个人版可能不全（申万本来是 A 股分类）。优先用 `s_info_gicssector` 或 Wind 自己的行业分类 `s_info_indcode`。如果全部都拿不到，**fallback**：手动在 `config/codes.csv` 加一列 industry（100 只 1 小时搞定）。

### Sheet 4：`announce_events` — 财报公告事件

**做什么用**：V6 把事件研究从"预期变化 90/10 分位"（噪声大）升级为以**财报实际公告日为 t=0** 的 PEAD（公告后盈利漂移）研究。**这是全球量化最经典的研究范式之一**。

**数据结构**：每只股票每个财报季 1 行。100 股 × 4 季 × 5 年 ≈ 2000 行。

| 字段中文 | Wind 公式 | 说明 |
|---|---|---|
| 公告日期 | `=[1]!s_stm_issuingdate($B$1, n)` | n = 向前推第 n 次公告 |
| 报告期 | `=[1]!s_fa_periodenddate($B$1, n)` | 2023Q1 / 2023Q4 等 |
| 实际净利润 (百万) | `=[1]!s_fa_profit($B$1, n, 1000000)` | 同上 n |
| 实际营收 (百万) | `=[1]!s_fa_revenue($B$1, n, 1000000)` | 同上 |
| 实际 EPS | `=[1]!s_fa_eps($B$1, n)` | 同上 |

**盈利惊喜 (Earnings Surprise) 由后端脚本算**：
```
surprise = (实际净利润 − 公告前 1 天 FY1 预期均值) / |公告前 1 天 FY1 预期均值|
```

> **风险**：港股年报 / 中报 2 次为主，季报覆盖不全。如果 `s_stm_issuingdate` 在个人版不可用，**fallback**：用 Sheet 1 的 B 列（FY1 均值）在日频上找"日变化绝对值 > 5%"的位置，反推为公告日。不如直接公告日精确但能用。

### Sheet 5：`benchmark` — 市场基准（3 列）

**做什么用**：
- 计算超额收益时用**恒生科技指数**替代"等权 100 股平均"（去 size bias）
- VAR 模型加入"市场收益"作为控制变量
- 牛熊状态判断用真实指数

**数据**：3 个指数 × 5 年日频 ≈ 4000 行（单次采集）。

| 列 | 字段中文 | Wind 公式 |
|---|---|---|
| A | 观测日期 | 同上 |
| B | 恒生科技指数 close | `=[1]!s_dq_close("HSTECH.HI",$A11,3)` |
| C | 恒生指数 close | `=[1]!s_dq_close("HSI.HI",$A11,3)` |
| D | HIBOR 1 月 (%) | 待验证 Wind 个人版代码 |

**采集方式**：不需要循环 100 股。可以单独放一个 template_benchmark.xlsx 或写在 update 脚本的特殊分支。

---

## 五、🟢 P2 可选 — V7 阶段考虑（先不拉）

仅作记录，**V5/V6 阶段不需要**：

| 字段 | 用途 | Wind 函数候选 |
|------|------|--------------|
| 分析师评级均值 | 评级因子 | `s_west_rating` |
| 目标价均值 | 目标价/股价 比率 | `s_west_targetprice` |
| 股东数（季频） | 筹码结构 | `s_holder_num` |
| 北向持股比例 | 外资动向 | `s_hk_sb_share` |
| 财务三大表明细 | 基本面深度 | `s_fa_*` 全套 |

**原则**：每加一个新字段都要回答"它直接喂给哪个分析？产出什么结论？"。

---

## 六、`_meta` sheet（template.xlsx 内）

在 template.xlsx 加一个 `_meta` sheet（不参与公式，纯参考）：

```
_meta sheet 布局：
  A 列：wind_code  B 列：name        C 列：备注
  0013.HK         和黄医药          
  0020.HK         商汤              
  ...
  9988.HK         阿里巴巴-W        
  9999.HK         网易              
```

**作用**：
- 人类阅读 template 时知道这个模板服务于哪 100 只
- 测试时方便在 B1 切换代码做单股验证
- 如果未来加新股票，先在这里登记，然后同步到 `config/codes.csv`

**与 update_wind_data.py 的关系**：脚本仍从 `config/codes.csv` 读，**不读 _meta sheet**。两者由用户/Claude 保持手动同步（codes.csv 是单一真相源）。

---

## 七、字段优先级与依赖关系

```
分析框架                            数据依赖
────────────────────────────────────────────────────────
Layer 1 交叉相关 ──────────────────► Sheet 1 (B,L 列 = FY1 + close)
Layer 2A 双向回归 ─────────────────► Sheet 1 (B,L) 或 (G,L) = FY2 + close
Layer 2B 事件研究 (当前阈值法) ────► Sheet 1 (B 或 G)
Layer 3A 状态依赖 ─────────────────► Sheet 1 + Sheet 5 (基准)
Layer 3B VAR + Granger ────────────► Sheet 1 + Sheet 5
Layer 3C IRF ──────────────────────► 同 Layer 3B
──────────────────────────────────────────────────────── 
V6 升级
─────────────────────────────────────────────────────── 
Newey-West 回归 ───────────────────► 无需新数据，只改算法
PEAD 事件研究 ─────────────────────► ★ Sheet 4 (公告日) 关键
行业切片 Granger ──────────────────► ★ Sheet 3 (行业)
分歧度因子事件 ────────────────────► Sheet 1 (E,J 列 = FY1/FY2 std)
FY1 vs FY2 信号对比 ───────────────► Sheet 1 (B vs G)
滚动 lag 稳定性 ───────────────────► 已有数据，跨度够长即可
────────────────────────────────────────────────────────
V7 因子化
────────────────────────────────────────────────────────
因子合成 ─────────────────────────► Sheet 1+2+3 全部
回测 ────────────────────────────► + Sheet 5 (基准)
风险归因 ─────────────────────────► + Sheet 2 (量价控制) + 行业
```

---

## 八、跨度与体量估算

### 跨度建议

| 数据 | 最低 | 推荐 | 上限 |
|------|------|------|------|
| Sheet 1/2 日序列 | 3 年 (~780 行) | **5 年 (~1300 行)** | 7 年 (~1820 行，受 Wind 港股一致预期深度限制) |
| Sheet 3 静态 | 仅当前快照 | 同 | 同 |
| Sheet 4 公告日 | 5 年 ~ 20 次/股 | 同 | 同 |
| Sheet 5 基准 | 同日序列 | 同 | 同 |

### 体量估算（5 年方案）

| 数据 | 行数 | 写入 SQLite 体积 |
|------|------|-----------------|
| Sheet 1 日序列 | 100 股 × ~1300 = ~130,000 行 | ~13 MB |
| Sheet 2 量价 | 同上 | ~6 MB |
| Sheet 3 静态 | 100 行 | 几 KB |
| Sheet 4 公告事件 | ~2,000 行 | <1 MB |
| Sheet 5 基准 | 3 × 1300 = ~4,000 行 | <1 MB |
| **总计** | **~140,000 行** | **~20 MB** |

完全在 SQLite 舒适区。

### 采集耗时估算

| 操作 | 单次耗时 | 100 股全量耗时 |
|------|---------|----------------|
| Sheet 1+2 单股（17 列 × 1300 行 公式重算） | ~120-180s | **3-5 小时** |
| Sheet 3 单股静态 | ~5s | **8-10 分钟** |
| Sheet 4 单股公告事件（20 行） | ~10s | **15-20 分钟** |
| Sheet 5 基准（一次性，不循环） | ~3-5min | **3-5 分钟** |
| **首次全量回拉总计** | | **约 4-6 小时（一晚上）** |

**之后每日增量更新**：只采当天新增 1 行 × 100 股 ≈ **20-30 分钟**（不影响每日 17:30 cron）。

---

## 九、对应的数据库 schema 设计（quant-v5 分支待实施）

```sql
-- 1. 日序列（一致预期 + 量价合并）
CREATE TABLE daily_data_v2 (
    update_date TEXT NOT NULL,      -- 采集日期
    trade_date TEXT NOT NULL,       -- 观测日期（日频）
    wind_code TEXT NOT NULL,
    -- FY1 一致预期
    fy1_netprofit_avg REAL,         -- B
    fy1_eps REAL,                   -- C
    fy1_instnum INTEGER,            -- D
    fy1_netprofit_std REAL,         -- E
    fy1_netprofit_median REAL,      -- F
    -- FY2 一致预期
    fy2_netprofit_avg REAL,         -- G
    fy2_eps REAL,                   -- H
    fy2_instnum INTEGER,            -- I
    fy2_netprofit_std REAL,         -- J
    fy2_netprofit_median REAL,      -- K
    -- 量价
    close_hkd REAL,                 -- L (Sheet 1) / B (Sheet 2)
    volume REAL,                    -- Sheet 2 C
    amount REAL,                    -- Sheet 2 D
    turn REAL,                      -- Sheet 2 E
    pe_ttm REAL,                    -- Sheet 2 F
    PRIMARY KEY (update_date, trade_date, wind_code)
);

-- 2. 静态信息（每股一行，仅最新）
CREATE TABLE static_info (
    wind_code TEXT PRIMARY KEY,
    name TEXT,
    industry_sw_l1 TEXT,
    industry_sw_l2 TEXT,
    industry_gics TEXT,
    mkt_cap_100m REAL,
    list_date TEXT,
    update_date TEXT
);

-- 3. 公告事件
CREATE TABLE announce_events (
    wind_code TEXT NOT NULL,
    ann_date TEXT NOT NULL,
    report_period TEXT NOT NULL,
    profit_actual REAL,
    revenue_actual REAL,
    eps_actual REAL,
    surprise REAL,                  -- 后端计算
    PRIMARY KEY (wind_code, report_period)
);

-- 4. 基准
CREATE TABLE benchmark (
    trade_date TEXT PRIMARY KEY,
    hstech_close REAL,
    hsi_close REAL,
    hibor_1m REAL
);
```

> 老表 `weekly_data` / `static_indicators` 保留不动（main 分支生产在用）。新表在 v5 分支并行存在，验证后再决定迁移策略。

---

## 十、验证清单（用户实施前的最小测试集）

在 template.xlsx 空白处（比如新 sheet）试以下 13 个公式：

**前置**：
- `B1` 临时填 `0700.HK`（腾讯，覆盖率最高，最容易拿到数据）
- `A1` 填 `=TODAY()` （= 今天 2026 年 5 月）
- `A2` 填 `2024-01-15`（中等历史日期）
- `A3` 填 `2021-01-04`（早期历史日期）

| # | 测试目的 | 公式（M 列起） | 期望 |
|---|---|---|---|
| 1 | 今天 FY1（=2026）净利润均值 | `=[1]!s_west_netprofit($B$1,YEAR($A$1),$A$1,180,1000000)` | 几千亿 |
| 2 | 今天 FY2（=2027）净利润均值 | `=[1]!s_west_netprofit($B$1,YEAR($A$1)+1,$A$1,180,1000000)` | 几千亿（可能略低） |
| 3 | 今天 FY1 EPS | `=[1]!s_west_eps($B$1,YEAR($A$1),$A$1,180)` | 几十 |
| 4 | **关键** 2024 年初看 FY2024 | `=[1]!s_west_netprofit($B$1,YEAR($A$2),$A$2,180,1000000)` | 有数 |
| 5 | **关键** 2024 年初看 FY2025 | `=[1]!s_west_netprofit($B$1,YEAR($A$2)+1,$A$2,180,1000000)` | 有数 |
| 6 | **核心关键** 2021 年初看 FY2021 | `=[1]!s_west_netprofit($B$1,YEAR($A$3),$A$3,180,1000000)` | 有数 → FY1 5 年可拉 |
| 7 | **核心关键** 2021 年初看 FY2022 | `=[1]!s_west_netprofit($B$1,YEAR($A$3)+1,$A$3,180,1000000)` | 有数 → FY2 5 年可拉 |
| 8 | s_dq_volume 是否可用 | `=[1]!s_dq_volume($B$1,$A$1)` | 非零 |
| 9 | s_dq_amount 是否可用 | `=[1]!s_dq_amount($B$1,$A$1)` | 非零 |
| 10 | s_dq_turn 是否可用 | `=[1]!s_dq_turn($B$1,$A$1)` | 非零 |
| 11 | s_dq_pe_ttm 是否可用 | `=[1]!s_dq_pe_ttm($B$1,$A$1)` | 非零（10-50 区间） |
| 12 | 恒生科技指数取数 | `=[1]!s_dq_close("HSTECH.HI",$A$1,3)` | 4000-8000 点 |
| 13 | 公告日函数是否可用（V6 准备） | `=[1]!s_stm_issuingdate($B$1,1)` | 最近一次财报日 |

**每个公式 4 种结果**：
- ✅ 非零数字 → 字段可用
- 0 → 字段不返回值（可能权限或数据深度问题）
- `#N/A` / `#VALUE!` → 字段名错或权限无
- `无法读取数据` → 个人版无授权

**最关键的两个**：#6 和 #7。如果 2021 年初能拉到 FY2021 + FY2022，**整个 5 年 × FY1+FY2 计划成立**。如果 #6 有数 #7 没数，FY2 计划要降级。如果 #6 #7 都没数，整体要回退到 3 年跨度。

---

## 十一、关键风险与降级方案

| 风险 | 概率 | 影响 | 降级方案 |
|------|------|------|---------|
| 测试 #7 失败：2021 年初拿不到 FY2 | 中 | FY2 历史缩到 2-3 年 | 早期只拉 FY1，2023 年起补 FY2；分析时分段处理 |
| 测试 #11 失败：`s_dq_pe_ttm` 不可用 | 中 | V7 估值因子缺一块 | 用 `s_fa_eps` × 4（年化）+ close 自算 PE |
| 测试 #13 失败：`s_stm_issuingdate` 不可用 | 中-高 | V6 PEAD 卡住 | 用 FY1 日变化 >5% 反推公告日 |
| 测试 #9-10 失败：成交量/换手率不可用 | 低 | Sheet 2 缩水 | 仅保留 close + pe，量价控制变量降级为常数 |
| 日频 1300 行 Excel 重算 > 5 分钟 | 中 | 单股采集超时 | 分两段重算（先 1-650 行、再 651-1300 行）；或降到周频 |
| Wind 港股一致预期早期覆盖 < 50% | 低 | 数据稀疏 | 接受现状，分析时用 dropna() 处理 |

---

## 十二、给用户的优先行动

```
本周内（30 分钟）：
  ① 在 template.xlsx 找一片空白区，铺好 B1 / A1 / A2 / A3 三个测试日期
  ② 跑 13 个验证公式
  ③ 截图给 Claude 看哪些 ✅ / ❌

Claude 拿到结果后（同一天）：
  ④ 根据 ✅/❌ 调整本清单（删掉不可用字段、确定 fallback）
  ⑤ 开 quant-v5 分支
  ⑥ 实施改造（另开 plan）：
     - 改 template.xlsx：A 列扩到 A1311，B-L 列新公式，加 _meta sheet
     - 改 SQLite schema：新建 daily_data_v2 / static_info / announce_events / benchmark 表
     - 改 update_wind_data.py：日频 + 11 列读取
     - 写 backfill_history.py：一次性回拉 5 年
     - 改 leadlag_analysis.py：max_lag 单位「周 → 天」（或聚合为周后再分析）
     - 改 dashboard.py：暴露 FY1/FY2 双 X 变量
  ⑦ 用户跑一次回拉（一晚上 4-6 小时）
  ⑧ 验证 dashboard 新字段
  ⑨ 合并 v5 → main → push → Streamlit Cloud 部署
```

---

*版本：v2.0 ｜ 最后更新：2026-05-22 ｜ 上一版：v1.0（基于固定 FY 的错误设计，已废弃）*

---

## 十三、估值因子扩展（v2.2，2026-05-25，V7 选股看板）

> 为「多因子加权选股看板」补齐分析师估值表所需的估值因子。已逐列核对其 `Index Future` / `quarterly earnings` 两张估值表，单股因子清单见 plan。

### 13.1 panel sheet 新增 S–X 列（已写入 `templates/template_panel.xlsx`，公式待 Wind 个人版验证）

| 列 | DB 字段 | 中文 | 公式（第3行，已填至 102 行） | 方向 |
|---|---|---|---|---|
| S | `pb` | 市净率 PB | `=[1]!s_val_pb($A3,$B3)` | 低=便宜 |
| T | `roe_fwd` | 预期 ROE | `=[1]!s_west_avgroe($A3,YEAR($B3),$B3,180)` | 高=好 |
| U | `div_yield` | 股息率 % | `=[1]!s_val_dividendyield2($A3,$B3)` | 高=好 |
| V | `ev_ebitda` | EV/EBITDA | `=[1]!s_val_ev2_to_ebitda($A3,$B3)` | 低=便宜 |
| W | `nde` | 净负债率 ND/E | `=[1]!s_fa_debttoequity($A3,$B3)` | 低=稳健 |
| X | `profit_alert` | 盈警 Profit Alert | `=[1]!s_west_profitnotice($A3,$B3)` | 负面=看空 |

- **Beta 不在此采**：后端 `factors.py` 用「个股收益 vs 恒生科技」滚动 52 周自算（`beta` 因子）。
- EPS增长 / 远期PE / 远期E/P：用已有 fy1/fy2_eps 派生，无需新采。
- DB schema：`scripts/migrate_add_valuation.py` 已用 `ALTER TABLE ADD COLUMN` 给 `panel_data` 加上述 6 列（非破坏、幂等）。
- 采集脚本：`collect_panel.py` 的 `PANEL_RANGE` 已 `A3:R102`→`A3:X102`、`FIELDS` 已 +6、切片 `r[2:24]`。

### 13.2 验证清单（用户在 Wind 个人版先测，再回拉）

`B1`=`0700.HK`，`A1`=`=TODAY()`、`A2`=`2024-01-15`、`A3`=`2021-01-04`，在空白处测：

| # | 字段 | 测试公式 | 期望 | 拿不到的备选/降级 |
|---|---|---|---|---|
| 1 | PB | `=[1]!s_val_pb($B$1,$A$1)` | 1–10 | 备 `s_val_pb_lf`；再不行用 1/(PE×派息率) 近似 |
| 2 | ROE | `=[1]!s_west_avgroe($B$1,YEAR($A$1),$A$1,180)` | 5–30% | 备 `s_val_roe`/`s_fa_roe`（滚动实际） |
| 3 | 股息率 | `=[1]!s_val_dividendyield2($B$1,$A$1)` | 0–8% | 备 `s_val_dividendyield` |
| 4 | EV/EBITDA | `=[1]!s_val_ev2_to_ebitda($B$1,$A$1)` | 5–40 | 备 `s_val_evtoebitda`；再不行跳过 |
| 5 | 净负债率 | `=[1]!s_fa_debttoequity($B$1,$A$1)` | 数值/可能 #NAME? | **风险高**，拿不到就跳过(不影响其余) |
| 6 | 盈警 | `=[1]!s_west_profitnotice($B$1,$A$1)` | 文本/数值/可能 #NAME? | **风险高**，拿不到就跳过 |

> #1–4 是核心估值因子,务必拿到;#5–6 风险高,拿不到不阻塞。每个公式 4 种结果同 §10（✅数字 / 0 / #N/A / 无授权）。
>
> 验证 OK 后回拉:`uv run python scripts/collect_panel.py --backfill --from 2021-01-04`（新列;耗时与现状相当）。回拉完，新估值因子自动出现在看板「🧮 多因子选股」权重面板。

*v2.2 ｜ 2026-05-25 ｜ 估值因子扩展(PB/ROE/股息率/EV-EBITDA/净负债率/盈警 + Beta 自算)*
