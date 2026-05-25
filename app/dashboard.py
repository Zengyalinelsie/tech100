"""香港科技 100 · 盈利预期与股价联动看板 (V5)

数据基础：
    panel_data    5.4 年周频面板（100 股 × 281 周 × 16 字段，FY1+FY2 双预期）
    static_info   公司名 / 申万行业 / 上市日期
    benchmark     恒生科技 + 恒生指数

分析层：app/analysis_v2.py（交叉相关 + Newey-West 回归 + 事件研究 + Granger）
因子层：app/factors.py（市值标准化预期修正 + 超额收益 + 分歧度）

启动：
    streamlit run app/dashboard.py
"""
import sqlite3
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from factors import build_factor_panel, attach_industry
from analysis_v2 import cross_correlation, bidir_regression_nw, event_study, granger_var
from strategy import backtest, latest_signal
from theme import PALETTE, PLOTLY_LAYOUT, bar_color, CUSTOM_CSS

ROOT = Path(__file__).parent.parent.resolve()
DB_PATH = ROOT / "data" / "wind_history.db"
SIG_THRESHOLD = 0.05

st.set_page_config(page_title="香港科技 100 · 盈利预期与股价联动看板", layout="wide")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ============== 数据加载（缓存） ==============
@st.cache_data(ttl=3600, show_spinner="加载因子面板…")
def load_data():
    fac = build_factor_panel()
    fac = attach_industry(fac)
    conn = sqlite3.connect(DB_PATH)
    bench = pd.read_sql("SELECT * FROM benchmark", conn)
    static = pd.read_sql("SELECT * FROM static_info", conn)
    conn.close()
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    return fac, bench, static


@st.cache_data(ttl=3600, show_spinner="跑分析（交叉相关 / 回归 / 事件 / Granger）…")
def run_analysis(fac_json: str, x_col: str):
    fac = pd.read_json(StringIO(fac_json), orient="split")
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    agg, raw = cross_correlation(fac, x_col=x_col, y_col="exret")
    dirA, dirB = bidir_regression_nw(fac, x_col=x_col, y_col="exret")
    ev, n_up, n_dn = event_study(fac, x_col=x_col, y_col="exret")
    gc = granger_var(fac, x_col=x_col, y_col="exret")
    return {"agg": agg, "raw": raw, "dirA": dirA, "dirB": dirB,
            "ev": ev, "n_up": n_up, "n_dn": n_dn, "gc": gc}


@st.cache_data(ttl=3600, show_spinner="跑策略回测…")
def run_backtest(fac_json: str, x_col: str, hold: int, top_pct: float, cost_bps: float, n_drop: int):
    fac = pd.read_json(StringIO(fac_json), orient="split")
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    res = backtest(fac, x_col=x_col, hold_weeks=hold, top_pct=top_pct,
                   cost_bps=cost_bps, n_drop=n_drop)
    sig = latest_signal(fac, x_col=x_col, top_pct=top_pct)
    return res, sig


# ============== 加载 ==============
fac, bench, static = load_data()
codes = sorted(fac["wind_code"].unique())
name_map = dict(zip(static["wind_code"], static["name"]))
ind_map = dict(zip(static["wind_code"], static["industry_l1"]))


# ============== 侧边栏 ==============
st.sidebar.header("筛选条件")

latest = fac["trade_date"].max().strftime("%Y-%m-%d")
st.sidebar.caption(f"📅 数据最新至：{latest}")
st.sidebar.caption(f"📊 5.4 年周频 / 100 股 / FY1+FY2 双预期")

# FY1 / FY2 切换
fy_choice = st.sidebar.radio(
    "**预期目标**（核心 X 变量）",
    ["FY1（当年预期）", "FY2（下一年预期）"],
    index=1,
    help="FY2 = 下一年盈利预期，对新信息更敏感；FY1 = 当年预期，年内会机械收敛",
)
x_col = "fy1_rev_norm" if "FY1" in fy_choice else "fy2_rev_norm"
fy_label = "FY1" if "FY1" in fy_choice else "FY2"

st.sidebar.markdown("---")

# 选股
st.sidebar.markdown("**选择股票**")
search = st.sidebar.text_input("选择股票", value="", label_visibility="collapsed", key="stock_search")

ALL_TAG = "📊 全部 100 只"
display_options = [f"{name_map.get(c, c)} ({c})" for c in codes]
if search.strip():
    s = search.strip().lower()
    filtered = [o for o in display_options if s in o.lower()]
    if not filtered:
        st.sidebar.warning(f"未找到「{search}」")
        filtered = display_options
else:
    filtered = display_options

selected_display = st.sidebar.selectbox(
    "股票列表", [ALL_TAG] + filtered, index=0,
    label_visibility="collapsed", key="stock_pick",
)
st.sidebar.caption(f"共 {len(filtered)} 只可选")

if selected_display == ALL_TAG:
    selected_code, selected_name = None, None
else:
    selected_name, _cp = selected_display.rsplit(" (", 1)
    selected_code = _cp.rstrip(")")

# 行业筛选
industries = ["全部"] + sorted(static["industry_l1"].dropna().unique().tolist())
sel_industry = st.sidebar.selectbox("行业筛选", industries, index=0)


# ============== 跑分析 ==============
fac_used = fac if sel_industry == "全部" else fac[fac["industry_l1"] == sel_industry]
fac_json = fac_used.to_json(orient="split", date_format="iso")
result = run_analysis(fac_json, x_col)


# ============== 主标题 + KPI ==============
st.title("香港科技 100 · 盈利预期与股价联动看板")
st.caption(
    f"分析变量：**{fy_label} 预期修正**（市值标准化）vs **超额收益**（相对恒生科技指数）"
    f" | 行业范围：{sel_industry}"
)

gc = result["gc"]
agg = result["agg"]
best_lag, best_r, best_sig = (None, None, None)
if not agg.empty:
    bi = agg["mean_r"].abs().idxmax()
    best_lag = int(agg.loc[bi, "lag"])
    best_r = float(agg.loc[bi, "mean_r"])
    best_sig = agg.loc[bi, "sig"]

mkt_state = "—"
if not bench.empty:
    bench_sorted = bench.sort_values("trade_date")
    bench_sorted["ma20"] = bench_sorted["hstech_close"].rolling(20, min_periods=10).mean()
    last = bench_sorted.iloc[-1]
    if pd.notna(last["ma20"]) and pd.notna(last["hstech_close"]):
        mkt_state = "牛市" if last["hstech_close"] > last["ma20"] else "熊市"

kc1, kc2, kc3, kc4, kc5 = st.columns(5)
kc1.metric("数据日期", latest)
kc2.metric("覆盖股票", f"{fac_used['wind_code'].nunique()} 只")
kc3.metric("市场状态", mkt_state)
if best_lag is not None:
    direction = "预期领先" if best_lag > 0 else ("股价领先" if best_lag < 0 else "同步")
    kc4.metric(f"最强 lag ({fy_label})", f"{best_lag} 周", f"{direction} r={best_r:+.3f}{best_sig}")
else:
    kc4.metric("最强 lag", "—")
if gc and "error" not in gc:
    xy = gc["x_to_y"]; yx = gc["y_to_x"]
    g_label = "预期→股价" if xy["sig"] else ("股价→预期" if yx["sig"] else "无显著")
    kc5.metric("Granger 主导", g_label, f"min p={min(xy['p'], yx['p']):.3f}")
else:
    kc5.metric("Granger 主导", "—")

st.divider()


# ============== Tab 路由 ==============
tab_market, tab_stock, tab_rank, tab_strat = st.tabs([
    "📊 市场整体", "🔍 个股深度", "🏆 100 家排行榜", "📈 策略回测",
])


# ============================================================
# Tab 1: 市场整体
# ============================================================
with tab_market:
    st.subheader(f"{fy_label} 预期修正与超额收益的交叉相关性")
    st.caption("k > 0：预期领先股价 ｜ k < 0：股价领先预期 ｜ k = 0：同步")
    if agg.empty:
        st.warning("数据不足")
    else:
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=agg["lag"], y=agg["mean_r"],
            marker_color=bar_color(agg["mean_r"]),
            marker_line_width=0,
            text=agg["sig"], textposition="outside",
            hovertemplate="lag=%{x}<br>r=%{y:.4f}<br>p=%{customdata:.4f}<extra></extra>",
            customdata=agg["p_value"],
        ))
        fig.add_hline(y=0, line_color="#BBB", line_width=1)
        fig.update_layout(
            title=dict(text=f"交叉相关 (★=p<{SIG_THRESHOLD})", x=0.5, font=dict(color=PALETTE["title"], size=15)),
            xaxis_title="Lag (周)", yaxis_title="平均 Pearson r",
            height=420, bargap=0.25, **PLOTLY_LAYOUT,
        )
        fig.update_xaxes(showgrid=False, tickmode="linear", dtick=1)
        fig.update_yaxes(showgrid=False)
        st.plotly_chart(fig, width="stretch")

        n_sig = (agg["sig"] == "★").sum()
        st.info(f"★ 显著 lag 数：{n_sig} / {len(agg)} | 最强 lag={best_lag} (r={best_r:+.4f} {best_sig})")
        st.dataframe(agg.round(4), width="stretch", hide_index=True)

    st.divider()
    st.subheader("双向预测回归（Newey-West HAC 标准误）")
    st.caption("方向 A：预期修正 → 未来超额收益 ｜ 方向 B：超额收益 → 未来预期修正")
    ca, cb = st.columns(2)
    with ca:
        st.markdown("**方向 A：预期 → 股价**")
        if not result["dirA"].empty:
            st.dataframe(result["dirA"].round(4), width="stretch", hide_index=True)
        else:
            st.caption("数据不足")
    with cb:
        st.markdown("**方向 B：股价 → 预期**")
        if not result["dirB"].empty:
            st.dataframe(result["dirB"].round(4), width="stretch", hide_index=True)
        else:
            st.caption("数据不足")

    st.divider()
    st.subheader("事件研究：预期大幅修正前后的累计超额收益")
    st.caption(f"事件 = 截面 90%/10% 分位 {fy_label} 预期修正；累计 = sum 超额收益")
    ev = result["ev"]
    if not ev.empty:
        fig_ev = go.Figure()
        fig_ev.add_trace(go.Scatter(
            x=ev["week"], y=ev["up_car"], name=f"大幅上调 (n={result['n_up']})",
            mode="lines+markers",
            line=dict(color=PALETTE["up"], width=2.5),
        ))
        fig_ev.add_trace(go.Scatter(
            x=ev["week"], y=ev["dn_car"], name=f"大幅下调 (n={result['n_dn']})",
            mode="lines+markers",
            line=dict(color=PALETTE["down"], width=2.5),
        ))
        fig_ev.add_vline(x=0, line_dash="dot", line_color="#AAA", annotation_text="事件日")
        fig_ev.update_layout(
            title=dict(text="CAR 曲线（事件窗 ±8 周）", x=0.5, font=dict(color=PALETTE["title"], size=15)),
            xaxis_title="事件窗口（周）", yaxis_title="累计超额收益",
            height=420, hovermode="x unified", **PLOTLY_LAYOUT,
        )
        st.plotly_chart(fig_ev, width="stretch")
        ev0 = ev[ev["week"] == 0]
        if not ev0.empty:
            row = ev0.iloc[0]
            st.info(
                f"**事件当周 t=0**：上调 CAR={row['up_car']:+.4f} {row['up_sig']} ｜ "
                f"下调 CAR={row['dn_car']:+.4f} {row['dn_sig']} ｜ 差距={row['up_car']-row['dn_car']:+.4f}"
            )

    st.divider()
    st.subheader("VAR 模型与 Granger 因果检验")
    st.caption(f"对 {fy_label} 预期修正和超额收益的全市场截面平均序列做 VAR")
    if gc and "error" not in gc:
        g1, g2 = st.columns(2)
        g1.metric(
            "预期修正 → 超额收益", f"F={gc['x_to_y']['stat']:.3f}",
            f"p={gc['x_to_y']['p']:.4f} {'★显著' if gc['x_to_y']['sig'] else '不显著'}",
        )
        g2.metric(
            "超额收益 → 预期修正", f"F={gc['y_to_x']['stat']:.3f}",
            f"p={gc['y_to_x']['p']:.4f} {'★显著' if gc['y_to_x']['sig'] else '不显著'}",
        )
        if not (gc["x_to_y"]["sig"] or gc["y_to_x"]["sig"]):
            st.warning("两方向均不显著 — 全市场截面平均后信号被摊平。个股层面可能仍有信号（见排行榜）。")
    else:
        st.warning("Granger 拟合失败" + (f"：{gc.get('error', '')}" if gc else ""))


# ============================================================
# Tab 2: 个股深度
# ============================================================
with tab_stock:
    if selected_code is None:
        st.info("请在侧边栏选股，或在搜索框输入名称/代码")
    else:
        sub = fac[fac["wind_code"] == selected_code].sort_values("trade_date")
        st.subheader(f"{selected_name}（{selected_code}） · {ind_map.get(selected_code, '—')}")

        if not sub.empty:
            latest_row = sub.iloc[-1]
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("最新收盘 (HKD)", f"{latest_row['close_hkd']:.2f}" if pd.notna(latest_row['close_hkd']) else "—")
            mc2.metric("市值（亿）", f"{latest_row['mkt_cap']:.0f}" if pd.notna(latest_row['mkt_cap']) else "—")
            mc3.metric("FY1 EPS", f"{latest_row['fy1_eps']:.2f}" if pd.notna(latest_row['fy1_eps']) else "—")
            mc4.metric("覆盖机构 (FY1)", f"{int(latest_row['fy1_instnum'])}" if pd.notna(latest_row['fy1_instnum']) else "—")

            fig = make_subplots(specs=[[{"secondary_y": True}]])
            fig.add_trace(go.Scatter(
                x=sub["trade_date"], y=sub["fy1_np_avg"],
                name="FY1 净利润预期",
                line=dict(color=PALETTE["forecast"], width=2),
            ), secondary_y=False)
            fig.add_trace(go.Scatter(
                x=sub["trade_date"], y=sub["fy2_np_avg"],
                name="FY2 净利润预期",
                line=dict(color=PALETTE["up"], width=2, dash="dot"),
            ), secondary_y=False)
            fig.add_trace(go.Scatter(
                x=sub["trade_date"], y=sub["close_hkd"],
                name="股价 (HKD)",
                line=dict(color=PALETTE["price"], width=2.5, dash="dash"),
            ), secondary_y=True)
            fig.update_layout(
                title=dict(text="预期净利润 vs 股价", x=0.5),
                height=460, hovermode="x unified", **PLOTLY_LAYOUT,
            )
            fig.update_yaxes(title_text="预测净利润（百万）", secondary_y=False)
            fig.update_yaxes(title_text="股价（HKD）", secondary_y=True)
            st.plotly_chart(fig, width="stretch")

            raw = result["raw"]
            sub_raw = raw[raw["wind_code"] == selected_code].sort_values("lag")
            if not sub_raw.empty:
                st.markdown(f"##### 该股 {fy_label} 预期修正 vs 超额收益")
                fig_c = go.Figure()
                fig_c.add_trace(go.Bar(
                    x=sub_raw["lag"], y=sub_raw["r"],
                    marker_color=bar_color(sub_raw["r"]),
                    hovertemplate="lag=%{x}<br>r=%{y:.3f}<extra></extra>",
                ))
                fig_c.add_hline(y=0, line_color="#BBB")
                fig_c.update_layout(
                    title=f"{selected_name} · {fy_label}",
                    xaxis_title="Lag (周)", yaxis_title="r",
                    height=360, bargap=0.25, **PLOTLY_LAYOUT,
                )
                fig_c.update_xaxes(showgrid=False, tickmode="linear", dtick=1)
                fig_c.update_yaxes(showgrid=False)
                st.plotly_chart(fig_c, width="stretch")


# ============================================================
# Tab 3: 100 家排行榜
# ============================================================
with tab_rank:
    st.subheader("100 家公司领先-滞后排行")
    st.caption(f"基于 {fy_label} 预期修正与超额收益。★ = 单股 |r| > 0.115（n≈260 时 5% 显著阈值）")

    raw = result["raw"]
    if raw.empty:
        st.warning("数据不足")
    else:
        idx = raw.groupby("wind_code")["r"].apply(lambda s: s.abs().idxmax())
        best = raw.loc[idx.values, ["wind_code", "lag", "r", "n"]].reset_index(drop=True)
        best["名称"] = best["wind_code"].map(name_map)
        best["行业"] = best["wind_code"].map(ind_map)
        best["显著"] = best["r"].abs().apply(lambda x: "★" if x > 0.115 else "")
        best["方向"] = best["lag"].apply(lambda l: "预期领先" if l > 0 else ("股价领先" if l < 0 else "同步"))
        best = best.rename(columns={"wind_code": "代码", "lag": "最优lag(周)", "r": "相关r", "n": "样本数"})
        best = best[["名称", "代码", "行业", "最优lag(周)", "相关r", "方向", "显著", "样本数"]]

        c1, c2, c3, c4 = st.columns(4)
        top_r = best.loc[best["相关r"].abs().idxmax()]
        c1.metric("最强相关", top_r["名称"], f"r={top_r['相关r']:+.3f} {top_r['显著']}")
        n_lead_price = (best["最优lag(周)"] < 0).sum()
        n_lead_exp = (best["最优lag(周)"] > 0).sum()
        c2.metric("股价领先股票数", f"{n_lead_price} 只", f"{n_lead_price/len(best)*100:.0f}%")
        c3.metric("预期领先股票数", f"{n_lead_exp} 只", f"{n_lead_exp/len(best)*100:.0f}%")
        n_sig_stock = (best["显著"] == "★").sum()
        c4.metric("显著样本数", f"{n_sig_stock} 只", f"{n_sig_stock/len(best)*100:.0f}%")

        csv = best.to_csv(index=False).encode("utf-8-sig")
        st.download_button("📥 下载 CSV", csv, file_name=f"ranking_{fy_label}_{latest}.csv", mime="text/csv")

        sorted_best = best.assign(_abs=best["相关r"].abs()).sort_values(
            "_abs", ascending=False).drop(columns=["_abs"])

        st.dataframe(
            sorted_best, width="stretch", hide_index=True,
            column_config={
                "相关r": st.column_config.NumberColumn(format="%+.3f"),
                "最优lag(周)": st.column_config.NumberColumn(format="%d"),
                "样本数": st.column_config.NumberColumn(format="%d"),
            },
        )


# ============================================================
# Tab 4: 策略回测
# ============================================================
with tab_strat:
    st.subheader(f"多空组合回测 · 按 {fy_label} 预期修正选股")
    st.caption(
        "每周排序：信号最强 top% 买入（多头）、最弱 top% 卖出（空头）。"
        "三条线 = 多头 / 空头簿损益 / 多空价差，基准 = 恒生科技。"
        "**点位正确**：t 时点信号 → t+1 起结算，无前视。"
        "已做股池清洗（可交易+流动性，退市保险栓来自 INF）；n_drop = 每期最多换几只（借 Qlib，降换手降成本）。"
    )

    sc1, sc2, sc3, sc4 = st.columns(4)
    hold = sc1.slider("持有/换仓周数", 1, 8, 1)
    top_pct = sc2.slider("买卖各占池比例", 0.05, 0.40, 0.20, step=0.05)
    cost_bps = sc3.slider("单边交易成本 (bps)", 0, 100, 30, step=5)
    topk_est = max(1, int(round(top_pct * 100)))
    n_drop = sc4.slider("n_drop（每期换仓上限）", 1, topk_est, min(3, topk_est))

    res, sig = run_backtest(fac_json, x_col, hold, top_pct, float(cost_bps), n_drop)
    nav, metrics, ic = res["nav"], res["metrics"], res["ic_summary"]

    if nav.empty:
        st.warning("数据不足，无法回测")
    else:
        line_color = {"多头": PALETTE["up"], "空头": PALETTE["down"],
                      "多空": PALETTE["forecast"], "恒生科技": PALETTE["neutral"]}
        dash = {"多头": None, "空头": None, "多空": None, "恒生科技": "dash"}
        fig_nav = go.Figure()
        for col in ["多头", "空头", "多空", "恒生科技"]:
            fig_nav.add_trace(go.Scatter(
                x=nav.index, y=nav[col], name=col, mode="lines",
                line=dict(color=line_color[col], width=2.5, dash=dash[col]),
            ))
        fig_nav.add_hline(y=1.0, line_color="#BBB", line_width=1)
        fig_nav.update_layout(
            title=dict(text=f"组合净值曲线（起点=1.0，持有{hold}周）", x=0.5,
                       font=dict(color=PALETTE["title"], size=15)),
            xaxis_title="日期", yaxis_title="净值 (NAV)",
            height=440, hovermode="x unified", **PLOTLY_LAYOUT,
        )
        st.plotly_chart(fig_nav, width="stretch")

        ic_mean = ic.get("ic_mean", float("nan"))
        ic_pos = ic.get("ic_pos_rate", float("nan"))
        ic_ir = ic.get("ic_ir", float("nan"))
        turn_l = ic.get("turnover_long", float("nan"))
        ls = metrics.loc["多空"]
        lng = metrics.loc["多头"]
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("多头累计", f"{lng['total_ret']*100:+.1f}%", f"IR {lng['ir']:+.2f}")
        m2.metric("多空累计", f"{ls['total_ret']*100:+.1f}%")
        m3.metric("多空 Sharpe", f"{ls['sharpe']:+.2f}")
        m4.metric("最大回撤(多空)", f"{ls['max_dd']*100:.1f}%")
        m5.metric("IC 均值", f"{ic_mean:+.4f}", f"ICIR {ic_ir:+.2f}")
        m6.metric("换手/期", f"{turn_l*100:.0f}%", f"正率 {ic_pos*100:.0f}%")

        if not (ic_mean > 0.02):
            st.warning(
                "⚠️ 横截面信号 IC≈0，**多空 alpha 尚未证明**（与 CHARTER「可交易因子尚未就绪」一致）。"
                f"但清洗股池 + 低换手(n_drop={n_drop}) 后，多头 sleeve 相对恒生科技 IR={lng['ir']:+.2f}。"
                "下一步建议：加分歧度过滤 / 用最优 lag / 行业切片，再决定是否投入真金。"
            )

        rolling = ic.get("rolling_ic", {})
        if rolling:
            st.caption("分层 IC（信号 vs 前向收益）： "
                       + " ｜ ".join(f"+{h}周 {v:+.4f}" for h, v in sorted(rolling.items())))

        st.markdown("##### 各组合指标")
        st.dataframe(
            metrics.rename(columns={
                "total_ret": "累计收益", "cagr": "年化", "ann_vol": "年化波动",
                "sharpe": "Sharpe", "max_dd": "最大回撤", "win_rate": "周胜率",
                "ir": "信息比率IR",
            }).round(4),
            width="stretch",
        )

    st.divider()
    st.subheader(f"本周交易清单（{latest}）· 可下载对接 INF 回测系统")
    st.caption("以最新收盘价为参考买卖价。LONG=买入 / SHORT=卖出。")
    if sig.empty:
        st.warning("本周无有效信号")
    else:
        sig_csv = sig.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 下载本周信号 CSV（INF 可读）", sig_csv,
            file_name=f"signal_{latest}.csv", mime="text/csv",
        )
        bcol, scol = st.columns(2)
        with bcol:
            st.markdown("**🟢 买入（多头）**")
            st.dataframe(
                sig[sig["side"] == "LONG"][["name", "wind_code", "signal", "ref_close_hkd"]]
                .rename(columns={"name": "名称", "wind_code": "代码",
                                 "signal": "信号", "ref_close_hkd": "参考价"}),
                width="stretch", hide_index=True,
            )
        with scol:
            st.markdown("**🔴 卖出（空头）**")
            st.dataframe(
                sig[sig["side"] == "SHORT"][["name", "wind_code", "signal", "ref_close_hkd"]]
                .rename(columns={"name": "名称", "wind_code": "代码",
                                 "signal": "信号", "ref_close_hkd": "参考价"}),
                width="stretch", hide_index=True,
            )
