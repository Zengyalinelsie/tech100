"""
香港科技 100 · 盈利预期与股价联动看板

启动方式：
    streamlit run app/dashboard.py
"""
import io
import sqlite3
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from leadlag_analysis import (
    compute_all_cross_correlations,
    bidirectional_prediction_regression,
    build_event_study,
    state_dependent_cross_correlation,
    var_granger_irf,
    build_ranking_df,
    detect_recent_events,
    prepare_stock_series,
    cross_correlation_series,
)
from theme import PALETTE, PLOTLY_LAYOUT, bar_color, lag_color, CUSTOM_CSS

# Streamlit Cloud 上中文字体在 packages.txt 阶段后安装，需强制重扫
try:
    fm._load_fontmanager(try_read_cache=False)
except Exception:
    pass

plt.rcParams["font.sans-serif"] = [
    "Heiti TC", "Arial Unicode MS", "Noto Sans CJK SC",
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).parent.parent.resolve()
DB_PATH = ROOT / "data" / "wind_history.db"

st.set_page_config(
    page_title="香港科技 100 · 盈利预期与股价联动看板",
    layout="wide",
)
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ============== 数据库查询 ==============
def get_conn():
    return sqlite3.connect(DB_PATH)


@st.cache_data(ttl=60)
def get_available_dates():
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT DISTINCT update_date FROM weekly_data ORDER BY update_date DESC", conn
    )
    conn.close()
    return df["update_date"].tolist()


@st.cache_data(ttl=60)
def get_codes_names(update_date):
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT DISTINCT wind_code, name FROM weekly_data
           WHERE update_date = ? ORDER BY wind_code""",
        conn, params=(update_date,),
    )
    conn.close()
    return df


@st.cache_data(ttl=60)
def get_weekly_data(update_date):
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT trade_date, wind_code, name, netprofit_avg, close_hkd
           FROM weekly_data WHERE update_date = ? ORDER BY wind_code, trade_date""",
        conn, params=(update_date,),
    )
    conn.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["netprofit_avg"] = pd.to_numeric(df["netprofit_avg"], errors="coerce")
    df["close_hkd"] = pd.to_numeric(df["close_hkd"], errors="coerce")
    return df


@st.cache_data(ttl=3600)
def get_all_historical_weekly():
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT trade_date, wind_code, name, netprofit_avg, close_hkd
           FROM weekly_data ORDER BY wind_code, trade_date""",
        conn,
    )
    conn.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["netprofit_avg"] = pd.to_numeric(df["netprofit_avg"], errors="coerce")
    df["close_hkd"] = pd.to_numeric(df["close_hkd"], errors="coerce")
    return df


@st.cache_data(ttl=60)
def get_static_data(update_date):
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM static_indicators WHERE update_date = ? ORDER BY wind_code",
        conn, params=(update_date,),
    )
    conn.close()
    return df


# ============== 分析计算（一次跑全，三 Tab 共用） ==============
@st.cache_data(ttl=3600, show_spinner=False)
def run_full_analysis(hist_json: str, static_json: str | None):
    hist_df = pd.read_json(StringIO(hist_json), orient="split")
    hist_df["trade_date"] = pd.to_datetime(hist_df["trade_date"])

    if static_json:
        static_df = pd.read_json(StringIO(static_json), orient="split")
    else:
        static_df = None

    agg, stock_best, _ = compute_all_cross_correlations(hist_df, max_lag=8)
    df_a, df_b = bidirectional_prediction_regression(hist_df, forward_weeks=(1, 2, 4))
    events_df, event_agg = build_event_study(hist_df, window=8)
    state_results = state_dependent_cross_correlation(hist_df, static_df, max_lag=8)
    _, gc, irf_data = var_granger_irf(hist_df, max_lag=8, irf_periods=12)

    return {
        "agg": agg, "stock_best": stock_best,
        "df_a": df_a, "df_b": df_b,
        "events_df": events_df, "event_agg": event_agg,
        "state_results": state_results,
        "gc": gc, "irf_data": irf_data,
    }


# ============== 侧边栏 ==============
st.sidebar.header("筛选条件")

available_dates = get_available_dates()
if not available_dates:
    st.error("数据库为空，请先运行采集脚本。")
    st.stop()

available_dates_dt = [datetime.strptime(d, "%Y-%m-%d").date() for d in available_dates]
max_d, min_d = max(available_dates_dt), min(available_dates_dt)

picked = st.sidebar.date_input(
    "数据日期",
    value=max_d,
    min_value=min_d,
    max_value=max_d,
    format="YYYY-MM-DD",
)

# 最近邻：取 ≤ 用户所选日期的最近一个有效数据日
candidates = [d for d in available_dates_dt if d <= picked]
actual_d = max(candidates) if candidates else max_d
selected_date = actual_d.strftime("%Y-%m-%d")

if actual_d != picked:
    st.sidebar.caption(f"📅 最近有效数据日期：{selected_date}")

weekly_df = get_weekly_data(selected_date)
static_df = get_static_data(selected_date)
codes_names_df = get_codes_names(selected_date)

if weekly_df.empty:
    st.warning(f"{selected_date} 没有 weekly 数据。")
    st.stop()

# 统一选股控件：text_input + 自实现 substring 过滤 + 候选清单
all_codes = codes_names_df["wind_code"].tolist()
all_names = codes_names_df["name"].tolist()
code_to_name = dict(zip(codes_names_df["wind_code"], codes_names_df["name"]))

ALL_TAG = "📊 全部 100 只"
display_options = [f"{name} ({code})" for code, name in zip(all_codes, all_names)]

st.sidebar.markdown("**选择股票**")
search = st.sidebar.text_input(
    "选择股票",
    value="",
    label_visibility="collapsed",
    key="stock_search",
)

if search.strip():
    s = search.strip().lower()
    filtered = [o for o in display_options if s in o.lower()]
    if not filtered:
        st.sidebar.warning(f"未找到匹配「{search}」的股票")
        filtered = display_options
else:
    filtered = display_options

options_full = [ALL_TAG] + filtered

selected_display = st.sidebar.selectbox(
    "股票列表",
    options_full,
    index=0,
    label_visibility="collapsed",
    key="stock_pick",
)
st.sidebar.caption(f"共 {len(filtered)} 只可选")

if selected_display == ALL_TAG:
    selected_code, selected_name = None, None
else:
    selected_name, _code_part = selected_display.rsplit(" (", 1)
    selected_code = _code_part.rstrip(")")


# ============== 页面标题 ==============
st.title("香港科技 100 · 盈利预期与股价联动看板")
st.caption(f"数据日期：{selected_date}")


# ============== 预热：加载历史 + 跑完所有分析 ==============
hist_df = get_all_historical_weekly()
analysis = None
if not hist_df.empty:
    with st.spinner("首次加载需 15–30 秒（之后命中缓存）……"):
        hist_json = hist_df.to_json(orient="split", date_format="iso")
        static_json = static_df.to_json(orient="split") if not static_df.empty else None
        analysis = run_full_analysis(hist_json, static_json)

# 最近 1 周 / 4 周事件（用于 KPI 状态栏 + 本周事件被动表）
recent_events_1w = detect_recent_events(hist_df, lookback_weeks=1) if not hist_df.empty else pd.DataFrame()
recent_events_4w = detect_recent_events(hist_df, lookback_weeks=4) if not hist_df.empty else pd.DataFrame()


# ============== 顶部 KPI 状态栏 ==============
def _market_state(weekly_df: pd.DataFrame) -> str:
    if weekly_df.empty:
        return "—"
    latest_date = weekly_df["trade_date"].max()
    snap = weekly_df[weekly_df["trade_date"] == latest_date]
    if snap.empty:
        return "—"
    ma20_by_code = (
        weekly_df.sort_values("trade_date")
        .groupby("wind_code")["close_hkd"]
        .apply(lambda s: s.rolling(20, min_periods=10).mean().iloc[-1])
    )
    last_by_code = snap.set_index("wind_code")["close_hkd"]
    aligned = pd.concat([last_by_code, ma20_by_code], axis=1, join="inner")
    aligned.columns = ["last", "ma20"]
    above = (aligned["last"] > aligned["ma20"]).mean()
    return f"牛市 {above*100:.0f}%" if above >= 0.5 else f"熊市 {(1-above)*100:.0f}%"


def _granger_dir(gc) -> tuple[str, str]:
    if not gc:
        return "—", "—"
    if gc["f_to_r"]["sig"] and not gc["r_to_f"]["sig"]:
        return "预期 → 股价", f"p = {gc['f_to_r']['pvalue']:.3f}"
    if gc["r_to_f"]["sig"] and not gc["f_to_r"]["sig"]:
        return "股价 → 预期", f"p = {gc['r_to_f']['pvalue']:.3f}"
    if gc["f_to_r"]["sig"] and gc["r_to_f"]["sig"]:
        return "双向因果", "均显著"
    return "未通过", f"min p = {min(gc['f_to_r']['pvalue'], gc['r_to_f']['pvalue']):.3f}"


kc1, kc2, kc3, kc4, kc5 = st.columns(5)
kc1.metric("数据日期", selected_date)
kc2.metric("覆盖股票数", f"{len(codes_names_df)} 只")
kc3.metric("市场状态", _market_state(weekly_df))
g_dir, g_sub = _granger_dir(analysis["gc"] if analysis else None)
kc4.metric("Granger 主导方向", g_dir, g_sub)
kc5.metric("近 1 周显著事件", f"{len(recent_events_1w)} 条")

st.divider()


# ============== Tab 路由 ==============
tab_market, tab_stock, tab_rank = st.tabs([
    "📊 市场整体",
    "🔍 个股深度",
    "🏆 100 家排行榜",
])


PER_PAGE = 12  # 每页 12 只（3 行 × 4 列）


# ============== 渲染：成分股全景 Plotly 交互（分页） ==============
def render_overview_plotly(weekly_df: pd.DataFrame, page: int, per_page: int):
    grouped = weekly_df.groupby("wind_code")
    all_codes = sorted(weekly_df["wind_code"].unique())

    start = page * per_page
    end = min(start + per_page, len(all_codes))
    codes = all_codes[start:end]

    n_cols = 4
    n_rows = (len(codes) + n_cols - 1) // n_cols if codes else 1

    specs = [[{"secondary_y": True}] * n_cols for _ in range(n_rows)]
    titles = []
    for c in codes:
        sub = grouped.get_group(c)
        name = sub["name"].iloc[0] if not sub["name"].isna().all() else c
        titles.append(f"{name} · {c}")

    fig = make_subplots(
        rows=n_rows, cols=n_cols, specs=specs, subplot_titles=titles,
        vertical_spacing=0.14, horizontal_spacing=0.07,
    )

    for idx, code in enumerate(codes):
        r = idx // n_cols + 1
        c = idx % n_cols + 1
        sub = grouped.get_group(code).sort_values("trade_date")

        fig.add_trace(
            go.Scatter(
                x=sub["trade_date"], y=sub["netprofit_avg"],
                mode="lines",
                line=dict(color=PALETTE["forecast"], width=2),
                name="预测净利润",
                legendgroup="forecast",
                showlegend=(idx == 0),
                hovertemplate="%{x|%Y-%m-%d}<br>净利润 %{y:,.0f} 百万<extra></extra>",
            ),
            row=r, col=c, secondary_y=False,
        )
        fig.add_trace(
            go.Scatter(
                x=sub["trade_date"], y=sub["close_hkd"],
                mode="lines",
                line=dict(color=PALETTE["price"], width=2, dash="dash"),
                name="股价",
                legendgroup="price",
                showlegend=(idx == 0),
                hovertemplate="%{x|%Y-%m-%d}<br>股价 %{y:.2f} HKD<extra></extra>",
            ),
            row=r, col=c, secondary_y=True,
        )

    fig.update_xaxes(
        tickfont=dict(color=PALETTE["axis"], size=11),
        gridcolor="#E5E5E5", showgrid=True,
    )
    fig.update_yaxes(
        tickfont=dict(color=PALETTE["axis"], size=11),
        gridcolor="#E5E5E5", showgrid=True,
        secondary_y=False,
    )
    fig.update_yaxes(
        tickfont=dict(color=PALETTE["axis"], size=11),
        showgrid=False,
        secondary_y=True,
    )
    fig.update_annotations(font=dict(size=13, color=PALETTE["title"]))
    fig.update_layout(
        height=380 * n_rows,
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="PingFang SC, Helvetica, Arial", size=12, color=PALETTE["axis"]),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5,
            font=dict(color=PALETTE["axis"], size=12),
        ),
        margin=dict(l=40, r=20, t=80, b=40),
    )
    return fig


# ============================================================
# Tab 1：市场整体
# ============================================================
with tab_market:
    # ── 本周事件被动表 ──
    with st.expander(
        f"📌 最近 4 周触发显著调整的股票（{len(recent_events_4w)} 条）",
        expanded=not recent_events_4w.empty,
    ):
        if recent_events_4w.empty:
            st.caption("最近 4 周内无股票触发各自 90% / 10% 分位的 ΔF 调整。")
        else:
            ev = recent_events_4w.copy()
            ev["名称"] = ev["代码"].map(code_to_name).fillna(ev["代码"])
            ev["日期"] = pd.to_datetime(ev["日期"]).dt.strftime("%Y-%m-%d")
            ev = ev[["日期", "代码", "名称", "事件", "ΔF", "当周收益"]]

            def _color_event(val):
                if val == "大幅上调":
                    return f"background-color: {PALETTE['up']}; color: #2C3E50;"
                if val == "大幅下调":
                    return f"background-color: {PALETTE['down']}; color: white;"
                return ""

            styled = ev.style.map(_color_event, subset=["事件"]) \
                             .format({"ΔF": "{:+.1f}", "当周收益": "{:+.2%}"})
            st.dataframe(styled, width="stretch", hide_index=True)

    st.divider()

    # ── 成分股全景（分页） ──
    st.subheader("成分股全景")
    st.caption("预测净利润（橙）vs 股价（蓝）双 Y 轴 · 每页 12 只")

    codes = sorted(weekly_df["wind_code"].unique())
    if not codes:
        st.warning("暂无数据。")
    else:
        n_pages = (len(codes) + PER_PAGE - 1) // PER_PAGE
        pc1, pc2, _ = st.columns([2, 4, 4])
        page = pc1.number_input(
            f"页（共 {n_pages} 页，每页 {PER_PAGE} 只）",
            min_value=1, max_value=n_pages, value=1, step=1,
            key="overview_page",
        )
        start = (int(page) - 1) * PER_PAGE
        end = min(start + PER_PAGE, len(codes))
        pc2.markdown(f"<div style='padding-top:30px;color:#666'>显示第 {start + 1} – {end} 只</div>", unsafe_allow_html=True)

        fig_overview = render_overview_plotly(weekly_df, int(page) - 1, PER_PAGE)
        st.plotly_chart(fig_overview, width="stretch")

    if analysis is None:
        st.warning("暂无历史数据可供时序分析。")
    else:
        agg = analysis["agg"]
        stock_best = analysis["stock_best"]
        df_a, df_b = analysis["df_a"], analysis["df_b"]
        events_df, event_agg = analysis["events_df"], analysis["event_agg"]
        state_results = analysis["state_results"]
        gc, irf_data = analysis["gc"], analysis["irf_data"]

        # ── 交叉相关 ──
        st.divider()
        st.subheader("盈利预期与股价的交叉相关性")
        st.caption("k > 0：预期领先股价 ｜ k < 0：股价领先预期 ｜ k = 0：基本同步")

        if agg.empty:
            st.warning("数据不足以计算交叉相关。")
        else:
            best_idx = agg["mean_pearson"].abs().idxmax()
            best_row = agg.loc[best_idx]
            best_lag = int(best_row["lag"])
            best_corr = float(best_row["mean_pearson"])

            if best_lag > 0:
                lead_text = f"预期平均领先股价 **{best_lag} 周**"
            elif best_lag < 0:
                lead_text = f"股价平均领先预期 **{abs(best_lag)} 周**"
            else:
                lead_text = "预期与股价基本同步"

            st.info(f"结论：{lead_text}（平均相关系数 = {best_corr:.3f}）")

            fig1, ax1 = plt.subplots(figsize=(10, 5), dpi=150)
            ax1.bar(agg["lag"], agg["mean_pearson"], color=bar_color(agg["mean_pearson"]), alpha=0.85)
            ax1.axhline(y=0, color=PALETTE["axis"], linewidth=0.8)
            ax1.set_xlabel("Lag (周)", fontsize=12, color=PALETTE["axis"])
            ax1.set_ylabel("平均 Pearson 相关系数", fontsize=12, color=PALETTE["axis"])
            ax1.set_title("交叉相关图（截面平均）", fontsize=14, fontweight="bold", color=PALETTE["title"])
            ax1.tick_params(axis="both", labelcolor=PALETTE["axis"], labelsize=11)
            ax1.set_xticks(agg["lag"])
            ax1.grid(True, alpha=0.3)
            st.pyplot(fig1)

            tbl = agg.copy()
            tbl.columns = ["Lag(周)", "Pearson均值", "Pearson标准差", "Spearman均值", "Spearman标准差", "股票数", "T统计量", "P值"]
            st.dataframe(tbl.round(3), width="stretch", hide_index=True)

            if not stock_best.empty:
                st.markdown("**个股层面的领先-滞后分布**")
                fig2, ax2 = plt.subplots(figsize=(10, 4), dpi=150)
                lag_counts = stock_best["best_lag"].value_counts().sort_index()
                ax2.bar(
                    lag_counts.index.astype(str), lag_counts.values,
                    color=lag_color(lag_counts.index.tolist()), alpha=0.85,
                )
                ax2.set_xlabel("最优 Lag (周)", fontsize=12, color=PALETTE["axis"])
                ax2.set_ylabel("股票数量", fontsize=12, color=PALETTE["axis"])
                ax2.set_title("每只股票的最优领先/滞后周数分布", fontsize=14, fontweight="bold", color=PALETTE["title"])
                ax2.tick_params(axis="both", labelcolor=PALETTE["axis"], labelsize=11)
                ax2.grid(True, alpha=0.3)
                st.pyplot(fig2)

                n_price_lead = int((stock_best["best_lag"] < 0).sum())
                n_expect_lead = int((stock_best["best_lag"] > 0).sum())
                n_sync = int((stock_best["best_lag"] == 0).sum())
                total = len(stock_best)

                c1, c2, c3 = st.columns(3)
                c1.metric("股价领先预期", f"{n_price_lead} 只", f"{n_price_lead / total * 100:.1f}%")
                c2.metric("预期领先股价", f"{n_expect_lead} 只", f"{n_expect_lead / total * 100:.1f}%")
                c3.metric("基本同步", f"{n_sync} 只", f"{n_sync / total * 100:.1f}%")

        # ── 预测力检验与事件影响 ──
        st.divider()
        st.subheader("预测力检验与事件影响")

        # 双向回归
        st.markdown("##### 双向预测回归")
        st.caption("方向 A：预期变化 → 未来股价收益 ｜ 方向 B：股价收益 → 未来预期变化")

        col_a, col_b = st.columns(2)

        def _agg_dir(df):
            if df.empty:
                return pd.DataFrame()
            return (
                df.groupby("lag").agg(
                    mean_beta=("beta", "mean"),
                    std_beta=("beta", "std"),
                    mean_r2=("r_squared", "mean"),
                    std_r2=("r_squared", "std"),
                    sig_pct=("p_value", lambda x: (x < 0.05).mean() * 100),
                    n=("wind_code", "nunique"),
                ).reset_index()
            )

        agg_a = _agg_dir(df_a)
        agg_b = _agg_dir(df_b)

        with col_a:
            st.markdown("**方向 A：预期 → 股价**")
            if not agg_a.empty:
                disp = agg_a.copy()
                disp.columns = ["前瞻周数", "β均值", "β标准差", "R²均值", "R²标准差", "β显著占比(%)", "股票数"]
                st.dataframe(disp.round(4), width="stretch", hide_index=True)
            else:
                st.warning("方向 A 数据不足。")

        with col_b:
            st.markdown("**方向 B：股价 → 预期**")
            if not agg_b.empty:
                disp = agg_b.copy()
                disp.columns = ["前瞻周数", "β均值", "β标准差", "R²均值", "R²标准差", "β显著占比(%)", "股票数"]
                st.dataframe(disp.round(4), width="stretch", hide_index=True)
            else:
                st.warning("方向 B 数据不足。")

        if not agg_a.empty and not agg_b.empty:
            best_a = agg_a.loc[agg_a["mean_r2"].idxmax()]
            best_b = agg_b.loc[agg_b["mean_r2"].idxmax()]
            if best_a["mean_r2"] > best_b["mean_r2"]:
                conclusion = (
                    f"方向 A 更强：预期变化对未来 **{int(best_a['lag'])} 周** 股价的预测力更高"
                    f"（平均 R² = {best_a['mean_r2']:.4f}），说明 **预期是领先指标**。"
                )
            else:
                conclusion = (
                    f"方向 B 更强：股价变化对未来 **{int(best_b['lag'])} 周** 预期变化的预测力更高"
                    f"（平均 R² = {best_b['mean_r2']:.4f}），说明 **股价是领先指标**。"
                )
            st.info(conclusion)

        # 事件研究
        st.markdown("##### 事件研究：预期大幅调整的市场反应")
        st.caption("超额收益 = 个股收益 − 等权市场平均收益 ｜ 事件定义：ΔF 处于历史 90%/10% 分位")

        if event_agg.empty:
            st.warning("事件研究数据不足。")
        else:
            fig_car = go.Figure()
            fig_car.add_trace(go.Scatter(
                x=event_agg["week"], y=event_agg["up_mean"],
                name="大幅上调", mode="lines+markers",
                line=dict(color=PALETTE["up"], width=2.5),
                marker=dict(size=8),
            ))
            fig_car.add_trace(go.Scatter(
                x=event_agg["week"], y=event_agg["down_mean"],
                name="大幅下调", mode="lines+markers",
                line=dict(color=PALETTE["down"], width=2.5),
                marker=dict(size=8),
            ))
            fig_car.add_hline(y=0, line_dash="dash", line_color="#888")
            fig_car.add_vline(x=0, line_dash="dot", line_color="#AAA", annotation_text="事件日")
            fig_car.update_layout(
                title="累计超额收益（CAR）：预期大幅调整前后",
                xaxis_title="事件窗口（周）", yaxis_title="累计超额收益",
                height=450, hovermode="x unified",
                **PLOTLY_LAYOUT,
            )
            st.plotly_chart(fig_car, width="stretch")

            tbl_ev = event_agg[["week", "up_mean", "up_t", "up_p", "up_n", "down_mean", "down_t", "down_p", "down_n"]].copy()
            tbl_ev.columns = ["周", "上调CAR均值", "上调T", "上调P", "上调N", "下调CAR均值", "下调T", "下调P", "下调N"]
            st.dataframe(tbl_ev.round(3), width="stretch", hide_index=True)

            ev0 = event_agg[event_agg["week"] == 0]
            if not ev0.empty:
                up_car = ev0.iloc[0]["up_mean"]; up_p = ev0.iloc[0]["up_p"]
                down_car = ev0.iloc[0]["down_mean"]; down_p = ev0.iloc[0]["down_p"]
                sig_up = "显著" if up_p < 0.05 else "不显著"
                sig_down = "显著" if down_p < 0.05 else "不显著"
                st.markdown(
                    f"**事件当周**：上调事件 CAR = {up_car:.3f}（{sig_up}，p={up_p:.3f}）｜ "
                    f"下调事件 CAR = {down_car:.3f}（{sig_down}，p={down_p:.3f}）"
                )

        # ── 条件分析与动态因果框架 ──
        st.divider()
        st.subheader("条件分析与动态因果框架")

        # 状态依赖
        st.markdown("##### 状态依赖：分市场状态的领先-滞后")
        st.caption("左列＝市场状态（牛/熊）｜ 右列＝机构覆盖度（高/低）")

        if state_results:
            sc1, sc2 = st.columns(2)
            state_names = list(state_results.keys())
            for i, name in enumerate(state_names):
                agg_state = state_results[name]
                if agg_state.empty:
                    continue
                fig_s, ax_s = plt.subplots(figsize=(8, 4), dpi=150)
                ax_s.bar(
                    agg_state["lag"], agg_state["mean_pearson"],
                    color=bar_color(agg_state["mean_pearson"]), alpha=0.85,
                )
                ax_s.axhline(y=0, color=PALETTE["axis"], linewidth=0.8)
                ax_s.set_xlabel("Lag (周)", fontsize=11, color=PALETTE["axis"])
                ax_s.set_ylabel("平均 Pearson r", fontsize=11, color=PALETTE["axis"])
                ax_s.set_title(name, fontsize=13, fontweight="bold", color=PALETTE["title"])
                ax_s.tick_params(axis="both", labelcolor=PALETTE["axis"], labelsize=10)
                ax_s.set_xticks(agg_state["lag"])
                ax_s.grid(True, alpha=0.3)
                (sc1 if i % 2 == 0 else sc2).pyplot(fig_s)

            if "牛市" in state_results and "熊市" in state_results:
                bull_best = state_results["牛市"].loc[state_results["牛市"]["mean_pearson"].abs().idxmax()]
                bear_best = state_results["熊市"].loc[state_results["熊市"]["mean_pearson"].abs().idxmax()]
                st.info(
                    f"牛市最优：lag={int(bull_best['lag'])}, r={bull_best['mean_pearson']:.3f} ｜ "
                    f"熊市最优：lag={int(bear_best['lag'])}, r={bear_best['mean_pearson']:.3f}"
                )
        else:
            st.warning("状态依赖分析数据不足。")

        # VAR + Granger
        st.markdown("##### VAR 模型与格兰杰因果检验")
        if gc:
            gc1, gc2 = st.columns(2)
            gc1.metric(
                "预期 → 股价",
                f"F = {gc['f_to_r']['stat']:.3f}",
                f"p = {gc['f_to_r']['pvalue']:.3f}  {'显著' if gc['f_to_r']['sig'] else '不显著'}",
            )
            gc2.metric(
                "股价 → 预期",
                f"F = {gc['r_to_f']['stat']:.3f}",
                f"p = {gc['r_to_f']['pvalue']:.3f}  {'显著' if gc['r_to_f']['sig'] else '不显著'}",
            )

            if gc["f_to_r"]["sig"] or gc["r_to_f"]["sig"]:
                pieces = []
                if gc["f_to_r"]["sig"]:
                    pieces.append("预期变化**格兰杰引起**股价变化")
                if gc["r_to_f"]["sig"]:
                    pieces.append("股价变化**格兰杰引起**预期变化")
                st.success(" ｜ ".join(pieces))
            else:
                st.warning("两个方向均未通过格兰杰因果检验：价格和预期之间不存在统计上可识别的领先-滞后因果关系。")
        else:
            st.warning("VAR 模型拟合失败（样本量不足）。")

        # IRF
        st.markdown("##### 脉冲响应函数")
        st.caption("一个标准差冲击后另一变量的动态反应路径（带 95% 置信区间）")

        if irf_data:
            ic1, ic2 = st.columns(2)
            periods = irf_data["periods"]

            with ic1:
                fig_irf1 = go.Figure()
                fig_irf1.add_trace(go.Scatter(
                    x=periods, y=irf_data["df_to_r"],
                    name="点估计", mode="lines+markers",
                    line=dict(color=PALETTE["price"], width=2.5),
                ))
                fig_irf1.add_trace(go.Scatter(
                    x=periods + periods[::-1],
                    y=irf_data["df_to_r_upper"] + irf_data["df_to_r_lower"][::-1],
                    fill="toself", fillcolor="rgba(154,201,219,0.40)",
                    line=dict(color="rgba(255,255,255,0)"),
                    name="95% CI", showlegend=True,
                ))
                fig_irf1.add_hline(y=0, line_dash="dash", line_color="#888")
                fig_irf1.update_layout(
                    title="预期冲击 → 股价响应",
                    xaxis_title="期数（周）", yaxis_title="响应幅度",
                    height=350, hovermode="x unified",
                    **PLOTLY_LAYOUT,
                )
                st.plotly_chart(fig_irf1, width="stretch")

            with ic2:
                fig_irf2 = go.Figure()
                fig_irf2.add_trace(go.Scatter(
                    x=periods, y=irf_data["r_to_df"],
                    name="点估计", mode="lines+markers",
                    line=dict(color=PALETTE["forecast"], width=2.5),
                ))
                fig_irf2.add_trace(go.Scatter(
                    x=periods + periods[::-1],
                    y=irf_data["r_to_df_upper"] + irf_data["r_to_df_lower"][::-1],
                    fill="toself", fillcolor="rgba(154,201,219,0.40)",
                    line=dict(color="rgba(255,255,255,0)"),
                    name="95% CI", showlegend=True,
                ))
                fig_irf2.add_hline(y=0, line_dash="dash", line_color="#888")
                fig_irf2.update_layout(
                    title="股价冲击 → 预期响应",
                    xaxis_title="期数（周）", yaxis_title="响应幅度",
                    height=350, hovermode="x unified",
                    **PLOTLY_LAYOUT,
                )
                st.plotly_chart(fig_irf2, width="stretch")
        else:
            st.warning("IRF 数据不可用。")


# ============================================================
# Tab 2：个股深度
# ============================================================
with tab_stock:
    if selected_code is None:
        st.info("请在左侧「选择股票」下拉框中选定一只股票（支持输入名称或代码模糊搜索）。")
    else:
        st.subheader(f"{selected_name}（{selected_code}）")

        # --- 顶部指标卡 ---
        stock_df = weekly_df[weekly_df["wind_code"] == selected_code].sort_values("trade_date")
        if not stock_df.empty:
            latest = stock_df.iloc[-1]
            first = stock_df.iloc[0]
            price_chg = (latest["close_hkd"] / first["close_hkd"] - 1) * 100 if first["close_hkd"] else 0
            np_chg = (latest["netprofit_avg"] / first["netprofit_avg"] - 1) * 100 if first["netprofit_avg"] else 0

            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("最新股价（HKD）", f"{latest['close_hkd']:.2f}", f"{price_chg:+.1f}% (期间)")
            mc2.metric("最新预测净利润（百万）", f"{latest['netprofit_avg']:,.0f}", f"{np_chg:+.1f}% (期间)")
            mc3.metric("样本数（周）", f"{len(stock_df)}")

        # --- K 线 / 净利润双 Y 轴 ---
        if stock_df.empty:
            st.warning(f"{selected_code} 在 {selected_date} 没有数据。")
        else:
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            fig.add_trace(
                go.Scatter(
                    x=stock_df["trade_date"], y=stock_df["netprofit_avg"],
                    name="预测净利润（百万）", mode="lines+markers",
                    line=dict(color=PALETTE["forecast"], width=2.5), marker=dict(size=4),
                    hovertemplate="%{x}<br>预测净利润: %{y:,.0f} 百万<extra></extra>",
                ),
                secondary_y=False,
            )
            fig.add_trace(
                go.Scatter(
                    x=stock_df["trade_date"], y=stock_df["close_hkd"],
                    name="股价（HKD）", mode="lines+markers",
                    line=dict(color=PALETTE["price"], width=2.5, dash="dash"), marker=dict(size=4),
                    hovertemplate="%{x}<br>股价: %{y:,.2f} HKD<extra></extra>",
                ),
                secondary_y=True,
            )
            fig.update_layout(
                title=dict(text="预测净利润 vs 股价", x=0.5),
                height=460, hovermode="x unified",
                **PLOTLY_LAYOUT,
            )
            fig.update_yaxes(title_text="预测净利润（百万）", secondary_y=False)
            fig.update_yaxes(title_text="股价（HKD）", secondary_y=True)
            fig.update_xaxes(title_text="日期")
            st.plotly_chart(fig, width="stretch")

        # --- 该股交叉相关 ---
        if analysis is not None and not hist_df.empty:
            st.divider()
            st.subheader("该股票的交叉相关性")

            sub_hist = prepare_stock_series(hist_df, selected_code)
            single_corr = cross_correlation_series(sub_hist["delta_f"], sub_hist["return_r"], max_lag=8)
            if single_corr:
                lags_s = sorted(single_corr.keys())
                pearsons_s = [single_corr[k]["pearson_r"] for k in lags_s]
                spearmans_s = [single_corr[k]["spearman_r"] for k in lags_s]

                fig_c = go.Figure()
                fig_c.add_trace(go.Bar(
                    x=lags_s, y=pearsons_s, name="Pearson",
                    marker_color=bar_color(pearsons_s), opacity=0.85,
                ))
                fig_c.add_trace(go.Scatter(
                    x=lags_s, y=spearmans_s, name="Spearman",
                    mode="lines+markers",
                    line=dict(color=PALETTE["band"], width=2.5),
                    marker=dict(size=8),
                ))
                fig_c.add_hline(y=0, line_dash="dash", line_color="#888")
                fig_c.update_layout(
                    title=f"{selected_name} 的交叉相关图",
                    xaxis_title="Lag (周)", yaxis_title="相关系数",
                    height=380, hovermode="x unified",
                    **PLOTLY_LAYOUT,
                )
                st.plotly_chart(fig_c, width="stretch")

                stock_best = analysis["stock_best"]
                sb = stock_best[stock_best["wind_code"] == selected_code]
                if not sb.empty:
                    best_lag_s = int(sb.iloc[0]["best_lag"])
                    best_corr_s = float(sb.iloc[0]["best_corr"])
                    st.info(
                        f"最优 lag = **{best_lag_s} 周**，对应相关系数 = **{best_corr_s:.3f}**"
                    )
            else:
                st.warning("该股票样本数不足以计算交叉相关。")

            # --- 该股双向回归 ---
            st.subheader("该股票的双向预测回归")
            df_a = analysis["df_a"]; df_b = analysis["df_b"]
            sub_a = df_a[df_a["wind_code"] == selected_code] if not df_a.empty else pd.DataFrame()
            sub_b = df_b[df_b["wind_code"] == selected_code] if not df_b.empty else pd.DataFrame()

            rc1, rc2 = st.columns(2)
            with rc1:
                st.markdown("**方向 A：预期 → 股价**")
                if not sub_a.empty:
                    d = sub_a[["lag", "beta", "r_squared", "p_value", "n_obs"]].copy()
                    d.columns = ["前瞻周数", "β", "R²", "p 值", "样本数"]
                    st.dataframe(d.round(4), width="stretch", hide_index=True)
                else:
                    st.caption("数据不足。")
            with rc2:
                st.markdown("**方向 B：股价 → 预期**")
                if not sub_b.empty:
                    d = sub_b[["lag", "beta", "r_squared", "p_value", "n_obs"]].copy()
                    d.columns = ["前瞻周数", "β", "R²", "p 值", "样本数"]
                    st.dataframe(d.round(4), width="stretch", hide_index=True)
                else:
                    st.caption("数据不足。")

            # --- 该股事件 CAR ---
            st.subheader("该股票的事件研究 CAR")
            events_df = analysis["events_df"]
            sub_ev = events_df[events_df["wind_code"] == selected_code] if not events_df.empty else pd.DataFrame()
            if sub_ev.empty:
                st.caption("该股票没有触发显著事件（90% / 10% 分位以外的 ΔF）。")
            else:
                weeks = list(range(-8, 9))
                up_arr = [c for c in sub_ev[sub_ev["event_type"] == "大幅上调"]["car_cumsum"].tolist() if len(c) == 17]
                down_arr = [c for c in sub_ev[sub_ev["event_type"] == "大幅下调"]["car_cumsum"].tolist() if len(c) == 17]

                fig_e = go.Figure()
                if up_arr:
                    up_mean = np.nanmean(np.array(up_arr), axis=0)
                    fig_e.add_trace(go.Scatter(
                        x=weeks, y=up_mean, name=f"大幅上调 (n={len(up_arr)})",
                        mode="lines+markers",
                        line=dict(color=PALETTE["up"], width=2.5),
                        marker=dict(size=7),
                    ))
                if down_arr:
                    down_mean = np.nanmean(np.array(down_arr), axis=0)
                    fig_e.add_trace(go.Scatter(
                        x=weeks, y=down_mean, name=f"大幅下调 (n={len(down_arr)})",
                        mode="lines+markers",
                        line=dict(color=PALETTE["down"], width=2.5),
                        marker=dict(size=7),
                    ))
                fig_e.add_hline(y=0, line_dash="dash", line_color="#888")
                fig_e.add_vline(x=0, line_dash="dot", line_color="#AAA", annotation_text="事件日")
                fig_e.update_layout(
                    title=f"{selected_name} 的事件窗口累计超额收益",
                    xaxis_title="事件窗口（周）", yaxis_title="累计超额收益",
                    height=400, hovermode="x unified",
                    **PLOTLY_LAYOUT,
                )
                st.plotly_chart(fig_e, width="stretch")


# ============================================================
# Tab 3：100 家排行榜
# ============================================================
with tab_rank:
    st.subheader("100 家公司多维度排行榜")
    st.caption("默认按 |相关系数| 降序。点击任意列头可切换排序。")

    if analysis is None:
        st.warning("尚无分析结果可用。")
    else:
        rank_df = build_ranking_df(
            analysis["stock_best"],
            analysis["df_a"],
            analysis["df_b"],
            analysis["events_df"],
            codes_names_df,
        )

        if rank_df.empty:
            st.warning("排行榜数据不足。")
        else:
            # ─ 头部 Top4 极值卡 ─
            c1, c2, c3, c4 = st.columns(4)

            valid_r = rank_df.dropna(subset=["相关r"])
            if not valid_r.empty:
                top_corr = valid_r.loc[valid_r["相关r"].abs().idxmax()]
                c1.metric("最强相关性", f"{top_corr['名称']}", f"r = {top_corr['相关r']:.3f}（lag={int(top_corr['最优lag'])}）")
            else:
                c1.metric("最强相关性", "—", "—")

            valid_a = rank_df.dropna(subset=["方向A R²"])
            if not valid_a.empty:
                top_predA = valid_a.loc[valid_a["方向A R²"].idxmax()]
                c2.metric(
                    "最强预测力（预期 → 股价）",
                    f"{top_predA['名称']}",
                    f"R² = {top_predA['方向A R²']:.3f}（前瞻 {int(top_predA['方向A 前瞻'])} 周）",
                )
            else:
                c2.metric("最强预测力（预期 → 股价）", "—", "—")

            valid_up = rank_df.dropna(subset=["上调CAR"])
            if not valid_up.empty:
                top_carUp = valid_up.loc[valid_up["上调CAR"].idxmax()]
                c3.metric("最大上调 CAR", f"{top_carUp['名称']}", f"+{top_carUp['上调CAR']:.3f}")
            else:
                c3.metric("最大上调 CAR", "—", "—")

            gc = analysis["gc"]
            if gc:
                g_dir = "预期 → 股价" if gc["f_to_r"]["sig"] else (
                    "股价 → 预期" if gc["r_to_f"]["sig"] else "未通过"
                )
                g_p = min(gc["f_to_r"]["pvalue"], gc["r_to_f"]["pvalue"])
                c4.metric("全市场 Granger 因果", g_dir, f"p = {g_p:.3f}")
            else:
                c4.metric("全市场 Granger 因果", "—", "—")

            # ─ 主表 ─
            st.markdown("##### 全量排行")
            sorted_df = rank_df.assign(_abs=rank_df["相关r"].abs()).sort_values(
                "_abs", ascending=False, na_position="last"
            ).drop(columns=["_abs"])

            # 加 sparkline 列：每只股票近 26 周股价
            def _spark(code, weeks=26):
                s = (
                    hist_df[hist_df["wind_code"] == code]
                    .sort_values("trade_date")["close_hkd"]
                    .tail(weeks)
                    .tolist()
                )
                return s if s else None

            sorted_df = sorted_df.copy()
            sorted_df["股价趋势"] = sorted_df["代码"].map(_spark)

            # CSV 下载
            csv_bytes = sorted_df.drop(columns=["股价趋势"]).to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 下载 CSV",
                data=csv_bytes,
                file_name=f"ranking_{selected_date}.csv",
                mime="text/csv",
            )

            st.dataframe(
                sorted_df,
                width="stretch", hide_index=True,
                column_config={
                    "相关r":     st.column_config.NumberColumn("相关 r", format="%.3f"),
                    "最优lag":   st.column_config.NumberColumn("最优 lag (周)", format="%d"),
                    "方向A R²":  st.column_config.NumberColumn("方向A R²", format="%.3f"),
                    "方向A 前瞻": st.column_config.NumberColumn("方向A 前瞻 (周)", format="%d"),
                    "方向B R²":  st.column_config.NumberColumn("方向B R²", format="%.3f"),
                    "方向B 前瞻": st.column_config.NumberColumn("方向B 前瞻 (周)", format="%d"),
                    "上调CAR":   st.column_config.NumberColumn("上调 CAR", format="%+.3f"),
                    "下调CAR":   st.column_config.NumberColumn("下调 CAR", format="%+.3f"),
                    "股价趋势":  st.column_config.LineChartColumn("股价趋势 (近 26 周)", width="medium"),
                },
            )

            st.caption(
                "说明：相关 r 来自 Layer 1 个股最优 lag；方向 A/B R² 取该股各前瞻期回归 R² 的最大值；"
                "CAR 为事件窗口末端（事件后 8 周）累计超额收益的平均值。"
            )

    # ─ 一致预期静态指标（折叠） ─
    with st.expander("一致预期静态指标（机构数 / 预测净利润 / 标准差）", expanded=False):
        if static_df.empty:
            st.warning("暂无静态指标数据。")
        else:
            rename_map = {
                "wind_code": "股票代码",
                "name": "公司名称",
                "inst_num_2026": "2026 预测机构家数",
                "netprofit_avg_2026": "2026 预测净利润均值(百万)",
                "netprofit_median_2026": "2026 预测净利润中值(百万)",
            }
            display_cols = [c for c in rename_map.keys() if c in static_df.columns]
            disp = static_df[display_cols].rename(columns=rename_map)
            for col in disp.columns:
                if "百万" in col:
                    disp[col] = disp[col].round(2)
                elif "家数" in col:
                    disp[col] = disp[col].fillna(0).astype(int)
            st.dataframe(disp, width="stretch", hide_index=True)
            st.caption("数据单位：百万元人民币 ｜ 来源：Wind 一致预期")
