"""
Streamlit 展示：香港科技100 一致预期 + 股价走势（交互式重构版）

启动方式：
    streamlit run app/dashboard.py
"""
import io
import sqlite3
from io import StringIO
from pathlib import Path

import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from leadlag_analysis import (
    compute_all_cross_correlations,
    bidirectional_prediction_regression,
    build_event_study,
)

# Streamlit Cloud 上中文字体是在 packages.txt 阶段后安装的，
# matplotlib 的字体缓存可能不包含它们，需要强制重新扫描。
try:
    fm._load_fontmanager(try_read_cache=False)
except Exception:
    pass

# 中文字体设置（兼容 macOS 本地 + Linux 服务器）
plt.rcParams["font.sans-serif"] = [
    "Heiti TC",
    "Arial Unicode MS",
    "Noto Sans CJK SC",
    "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei",
    "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).parent.parent.resolve()
DB_PATH = ROOT / "data" / "wind_history.db"

st.set_page_config(page_title="香港科技100 一致预期看板", layout="wide")


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
        conn,
        params=(update_date,),
    )
    conn.close()
    return df


@st.cache_data(ttl=60)
def get_weekly_data(update_date):
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT trade_date, wind_code, name, netprofit_avg, close_hkd
           FROM weekly_data WHERE update_date = ? ORDER BY wind_code, trade_date""",
        conn,
        params=(update_date,),
    )
    conn.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["netprofit_avg"] = pd.to_numeric(df["netprofit_avg"], errors="coerce")
    df["close_hkd"] = pd.to_numeric(df["close_hkd"], errors="coerce")
    return df


@st.cache_data(ttl=3600)
def get_all_historical_weekly():
    """读取全量历史 weekly 数据（用于时间差分析）。"""
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
        conn,
        params=(update_date,),
    )
    conn.close()
    return df


# ============== 侧边栏筛选 ==============
st.sidebar.header("筛选条件")

available_dates = get_available_dates()
if not available_dates:
    st.error("数据库为空，请先运行采集脚本。")
    st.stop()

selected_date = st.sidebar.selectbox("数据日期", available_dates, index=0)

# 加载数据
weekly_df = get_weekly_data(selected_date)
static_df = get_static_data(selected_date)
codes_names_df = get_codes_names(selected_date)

if weekly_df.empty:
    st.warning(f"{selected_date} 没有 weekly 数据。")
    st.stop()

# 筛选方式
filter_mode = st.sidebar.radio(
    "筛选方式",
    ["全部 100 只", "按股票代码", "按股票名称"],
    index=0,
)

# 代码/名称下拉框（联动）
all_codes = codes_names_df["wind_code"].tolist()
all_names = codes_names_df["name"].tolist()
code_to_name = dict(zip(codes_names_df["wind_code"], codes_names_df["name"]))
name_to_code = dict(zip(codes_names_df["name"], codes_names_df["wind_code"]))

selected_code = None
selected_name = None

if filter_mode == "按股票代码":
    selected_code = st.sidebar.selectbox("股票代码", all_codes, index=0)
    selected_name = code_to_name.get(selected_code, "")
    st.sidebar.markdown(f"**公司名称**：{selected_name}")
elif filter_mode == "按股票名称":
    selected_name = st.sidebar.selectbox("股票名称", all_names, index=0)
    selected_code = name_to_code.get(selected_name, "")
    st.sidebar.markdown(f"**股票代码**：{selected_code}")


# ============== 页面标题 ==============
st.title("香港科技100 一致预期看板")
st.caption(f"数据日期：{selected_date}")


# ============== Tab 路由 ==============
if filter_mode == "全部 100 只":
    tabs = st.tabs(["📈 100只概览", "⏱️ 时间差分析", "📋 静态指标"])
    tab_overview, tab_leadlag, tab_static = tabs
    tab_detail = None
else:
    tabs = st.tabs(["🔍 个股详情", "📈 100只概览", "⏱️ 时间差分析", "📋 静态指标"])
    tab_detail, tab_overview, tab_leadlag, tab_static = tabs


# ============== 缓存渲染 100只概览 ==============
@st.cache_data(ttl=3600)
def render_overview_png(weekly_json: str, date_str: str) -> bytes:
    """把 100 只概览渲染为 PNG 字节并缓存，避免每次刷新都重绘。"""
    weekly_df = pd.read_json(StringIO(weekly_json), orient="split")
    weekly_df["trade_date"] = pd.to_datetime(weekly_df["trade_date"])
    grouped = weekly_df.groupby("wind_code")
    codes = sorted(weekly_df["wind_code"].unique())
    n_codes = len(codes)
    n_cols = 4
    n_rows = (n_codes + n_cols - 1) // n_cols

    # 高 dpi + 合理 figsize，保证清晰度同时控制内存
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(20, 3.5 * n_rows), dpi=200, squeeze=False)
    fig.patch.set_facecolor("white")

    for idx, code in enumerate(codes):
        row = idx // n_cols
        col = idx % n_cols
        ax = axes[row][col]

        sub = grouped.get_group(code).sort_values("trade_date")
        name = sub["name"].iloc[0] if not sub["name"].isna().all() else code

        color1 = "#1f77b4"
        ax.plot(sub["trade_date"], sub["netprofit_avg"], color=color1, linewidth=1.2, label="预测净利润")
        ax.set_ylabel("净利润(百万)", color=color1, fontsize=9)
        ax.tick_params(axis="y", labelcolor=color1, labelsize=7)

        ax2 = ax.twinx()
        color2 = "#ff7f0e"
        ax2.plot(sub["trade_date"], sub["close_hkd"], color=color2, linewidth=1.2, linestyle="--", label="股价")
        ax2.set_ylabel("股价(HKD)", color=color2, fontsize=9)
        ax2.tick_params(axis="y", labelcolor=color2, labelsize=7)

        # 标题字体加大，提升可读性
        ax.set_title(f"{name}\n{code}", fontsize=11, fontweight="bold")

        n_ticks = min(3, len(sub))
        if n_ticks > 0:
            step = max(1, len(sub) // n_ticks)
            tick_positions = sub["trade_date"].iloc[::step]
            ax.set_xticks(tick_positions)
            ax.set_xticklabels([d.strftime("%m-%d") for d in tick_positions], rotation=45, ha="right", fontsize=6)

        ax.grid(True, alpha=0.25, linestyle=":")

    # 隐藏空白子图
    for idx in range(n_codes, n_rows * n_cols):
        row = idx // n_cols
        col = idx % n_cols
        axes[row][col].axis("off")

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ============== Tab 1/2: 100只概览 (matplotlib) ==============
with tab_overview:
    st.subheader("预测净利润平均值 vs 股价")

    codes = sorted(weekly_df["wind_code"].unique())
    n_codes = len(codes)
    if n_codes == 0:
        st.warning("暂无数据。")
    else:
        # 用 JSON 串作为缓存 key（比直接传 DataFrame 更稳定）
        png_bytes = render_overview_png(weekly_df.to_json(orient="split", date_format="iso"), selected_date)
        st.image(png_bytes, use_container_width=True)


# ============== Tab: 时间差分析 (Layer 1) ==============
with tab_leadlag:
    st.subheader("Layer 1：谁领先谁？领先多久？")
    st.caption("k > 0：预期领先股价 | k < 0：股价领先预期 | k = 0：基本同步")

    hist_df = get_all_historical_weekly()
    if hist_df.empty:
        st.warning("暂无历史数据。")
    else:
        with st.spinner("正在计算交叉相关（约需 10–20 秒）……"):
            agg, stock_best, _ = compute_all_cross_correlations(hist_df, max_lag=8)

        if agg.empty:
            st.warning("数据不足以计算交叉相关。")
        else:
            # 结论卡片
            best_idx = agg["mean_pearson"].abs().idxmax()
            best_row = agg.loc[best_idx]
            best_lag = int(best_row["lag"])
            best_corr = best_row["mean_pearson"]

            if best_lag > 0:
                lead_text = f"预期平均领先股价 **{best_lag} 周**"
            elif best_lag < 0:
                lead_text = f"股价平均领先预期 **{abs(best_lag)} 周**"
            else:
                lead_text = "预期与股价基本同步"

            st.info(f"📊 结论：{lead_text}（平均相关系数 = {best_corr:.3f}）")

            # 交叉相关图（截面平均）
            fig1, ax1 = plt.subplots(figsize=(10, 5), dpi=150)
            colors = ["#2ecc71" if c > 0 else "#e74c3c" for c in agg["mean_pearson"]]
            ax1.bar(agg["lag"], agg["mean_pearson"], color=colors, alpha=0.7)
            ax1.axhline(y=0, color="black", linewidth=0.8)
            ax1.set_xlabel("Lag (周)", fontsize=11)
            ax1.set_ylabel("平均 Pearson 相关系数", fontsize=11)
            ax1.set_title("交叉相关图（截面平均）：盈利预期变化 vs 股价收益率", fontsize=13, fontweight="bold")
            ax1.set_xticks(agg["lag"])
            ax1.grid(True, alpha=0.3)
            st.pyplot(fig1)

            # 统计表格
            tbl = agg.copy()
            tbl.columns = ["Lag(周)", "Pearson均值", "Pearson标准差", "Spearman均值", "Spearman标准差", "股票数", "T统计量", "P值"]
            st.dataframe(tbl.round(3), use_container_width=True, hide_index=True)

            # 个股最优 lag 分布
            if not stock_best.empty:
                st.subheader("个股层面的领先-滞后分布")
                fig2, ax2 = plt.subplots(figsize=(10, 4), dpi=150)
                lag_counts = stock_best["best_lag"].value_counts().sort_index()
                bar_colors = ["#e74c3c" if l < 0 else "#3498db" if l > 0 else "#95a5a6" for l in lag_counts.index]
                ax2.bar(lag_counts.index.astype(str), lag_counts.values, color=bar_colors, alpha=0.7)
                ax2.set_xlabel("最优 Lag (周)", fontsize=11)
                ax2.set_ylabel("股票数量", fontsize=11)
                ax2.set_title("每只股票的最优领先/滞后周数分布", fontsize=13, fontweight="bold")
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

            # ── 交互式：单只股票交叉相关 ──
            st.divider()
            st.subheader("🔍 单只股票交叉相关分析（交互式）")

            # 取有结果的股票列表
            available_codes = sorted(stock_best["wind_code"].unique()) if not stock_best.empty else []
            if available_codes:
                # 用 name + code 组合显示
                name_map = dict(zip(codes_names_df["wind_code"], codes_names_df["name"]))
                display_options = [f"{name_map.get(c, c)} ({c})" for c in available_codes]
                selected_display = st.selectbox("选择股票", display_options, index=0)
                selected_code_ll = selected_display.split("(")[-1].rstrip(")")

                # 从 stock_best 取该股票的最优 lag
                sb = stock_best[stock_best["wind_code"] == selected_code_ll]
                if not sb.empty:
                    best_lag_stock = int(sb.iloc[0]["best_lag"])
                    best_corr_stock = sb.iloc[0]["best_corr"]
                    st.markdown(
                        f"**{name_map.get(selected_code_ll, selected_code_ll)}**："
                        f"最优 lag = **{best_lag_stock} 周**，"
                        f"相关系数 = **{best_corr_stock:.3f}**"
                    )

                # 用 Plotly 画该股票的交叉相关柱状图
                # 需要从 df_raw 中筛选（但之前用 _ 丢弃了），重新计算单只
                from leadlag_analysis import prepare_stock_series, cross_correlation_series

                sub_hist = prepare_stock_series(hist_df, selected_code_ll)
                single_corr = cross_correlation_series(sub_hist["delta_f"], sub_hist["return_r"], max_lag=8)
                if single_corr:
                    lags_s = sorted(single_corr.keys())
                    pearsons_s = [single_corr[l]["pearson_r"] for l in lags_s]
                    spearmans_s = [single_corr[l]["spearman_r"] for l in lags_s]

                    fig3 = go.Figure()
                    fig3.add_trace(go.Bar(
                        x=lags_s,
                        y=pearsons_s,
                        name="Pearson",
                        marker_color=["#2ecc71" if v > 0 else "#e74c3c" for v in pearsons_s],
                        opacity=0.8,
                    ))
                    fig3.add_trace(go.Scatter(
                        x=lags_s,
                        y=spearmans_s,
                        name="Spearman",
                        mode="lines+markers",
                        line=dict(color="#3498db", width=2),
                        marker=dict(size=8),
                    ))
                    fig3.add_hline(y=0, line_dash="dash", line_color="black")
                    fig3.update_layout(
                        title=f"{name_map.get(selected_code_ll, selected_code_ll)} 的交叉相关图",
                        xaxis_title="Lag (周)",
                        yaxis_title="相关系数",
                        height=400,
                        hovermode="x unified",
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
                    )
                    st.plotly_chart(fig3, use_container_width=True)

            # ── Layer 2: 领先-滞后预测能力 + 事件研究 ──
            st.divider()
            st.subheader("Layer 2：领先-滞后有多强？能区分买卖时机吗？")

            with st.spinner("正在计算双向回归与事件研究（约需 10–20 秒）……"):
                df_a, df_b = bidirectional_prediction_regression(hist_df, forward_weeks=(1, 2, 4))
                events_df, event_agg = build_event_study(hist_df, window=8)

            # ── 2A: 双向预测回归 ──
            st.markdown("#### 📈 双向预测回归")
            st.caption("方向 A：预期变化 → 未来股价收益 | 方向 B：股价收益 → 未来预期变化")

            col_a, col_b = st.columns(2)

            with col_a:
                st.markdown("**方向 A：预期 → 股价**")
                if not df_a.empty:
                    agg_a = (
                        df_a.groupby("lag")
                        .agg(
                            mean_beta=("beta", "mean"),
                            std_beta=("beta", "std"),
                            mean_r2=("r_squared", "mean"),
                            std_r2=("r_squared", "std"),
                            sig_pct=("p_value", lambda x: (x < 0.05).mean() * 100),
                            n=("wind_code", "nunique"),
                        )
                        .reset_index()
                    )
                    agg_a.columns = ["前瞻周数", "β均值", "β标准差", "R²均值", "R²标准差", "β显著占比(%)", "股票数"]
                    st.dataframe(agg_a.round(4), use_container_width=True, hide_index=True)
                else:
                    st.warning("方向 A 数据不足。")

            with col_b:
                st.markdown("**方向 B：股价 → 预期**")
                if not df_b.empty:
                    agg_b = (
                        df_b.groupby("lag")
                        .agg(
                            mean_beta=("beta", "mean"),
                            std_beta=("beta", "std"),
                            mean_r2=("r_squared", "mean"),
                            std_r2=("r_squared", "std"),
                            sig_pct=("p_value", lambda x: (x < 0.05).mean() * 100),
                            n=("wind_code", "nunique"),
                        )
                        .reset_index()
                    )
                    agg_b.columns = ["前瞻周数", "β均值", "β标准差", "R²均值", "R²标准差", "β显著占比(%)", "股票数"]
                    st.dataframe(agg_b.round(4), use_container_width=True, hide_index=True)
                else:
                    st.warning("方向 B 数据不足。")

            # 结论卡片
            if not df_a.empty and not df_b.empty:
                best_a = agg_a.loc[agg_a["R²均值"].idxmax()]
                best_b = agg_b.loc[agg_b["R²均值"].idxmax()]
                if best_a["R²均值"] > best_b["R²均值"]:
                    conclusion = (
                        f"方向 A 更强：预期变化对未来 **{int(best_a['前瞻周数'])} 周** 股价的预测力更高"
                        f"（平均 R² = {best_a['R²均值']:.4f}），"
                        f"说明 **预期是领先指标**。"
                    )
                else:
                    conclusion = (
                        f"方向 B 更强：股价变化对未来 **{int(best_b['前瞻周数'])} 周** 预期变化的预测力更高"
                        f"（平均 R² = {best_b['R²均值']:.4f}），"
                        f"说明 **股价是领先指标**。"
                    )
                st.info(conclusion)

            # ── 2B: 事件研究 ──
            st.markdown("#### 📊 事件研究：预期大幅调整前后的股价反应")
            st.caption("超额收益 = 个股收益 − 等权市场平均收益 | 事件定义：ΔF 处于历史 90%/10% 分位")

            if event_agg.empty:
                st.warning("事件研究数据不足。")
            else:
                # Plotly CAR 曲线
                fig_car = go.Figure()
                fig_car.add_trace(go.Scatter(
                    x=event_agg["week"],
                    y=event_agg["up_mean"],
                    name="大幅上调",
                    mode="lines+markers",
                    line=dict(color="#2ecc71", width=2),
                    marker=dict(size=8),
                ))
                fig_car.add_trace(go.Scatter(
                    x=event_agg["week"],
                    y=event_agg["down_mean"],
                    name="大幅下调",
                    mode="lines+markers",
                    line=dict(color="#e74c3c", width=2),
                    marker=dict(size=8),
                ))
                fig_car.add_hline(y=0, line_dash="dash", line_color="black")
                fig_car.add_vline(x=0, line_dash="dot", line_color="gray", annotation_text="事件日")
                fig_car.update_layout(
                    title="累计超额收益（CAR）：预期大幅调整前后",
                    xaxis_title="事件窗口（周）",
                    yaxis_title="累计超额收益",
                    height=450,
                    hovermode="x unified",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
                )
                st.plotly_chart(fig_car, use_container_width=True)

                # 统计表格
                tbl_ev = event_agg[["week", "up_mean", "up_t", "up_p", "up_n", "down_mean", "down_t", "down_p", "down_n"]].copy()
                tbl_ev.columns = [
                    "周", "上调CAR均值", "上调T", "上调P", "上调N",
                    "下调CAR均值", "下调T", "下调P", "下调N",
                ]
                st.dataframe(tbl_ev.round(3), use_container_width=True, hide_index=True)

                # 关键结论
                up_event_week = event_agg[event_agg["week"] == 0]
                if not up_event_week.empty:
                    up_car = up_event_week.iloc[0]["up_mean"]
                    up_p = up_event_week.iloc[0]["up_p"]
                    down_car = up_event_week.iloc[0]["down_mean"]
                    down_p = up_event_week.iloc[0]["down_p"]
                    sig_up = "✅ 显著" if up_p < 0.05 else "❌ 不显著"
                    sig_down = "✅ 显著" if down_p < 0.05 else "❌ 不显著"
                    st.markdown(
                        f"**事件当周**：上调事件 CAR = {up_car:.3f}（{sig_up}，p={up_p:.3f}）| "
                        f"下调事件 CAR = {down_car:.3f}（{sig_down}，p={down_p:.3f}）"
                    )


# ============== Tab: 个股详情 (plotly 交互式) ==============
if tab_detail is not None:
    with tab_detail:
        stock_df = weekly_df[weekly_df["wind_code"] == selected_code].sort_values("trade_date")

        if stock_df.empty:
            st.warning(f"{selected_code} 没有数据。")
        else:
            st.subheader(f"{selected_name} ({selected_code})")

            # Plotly 交互式双Y轴图
            fig = make_subplots(specs=[[{"secondary_y": True}]])

            fig.add_trace(
                go.Scatter(
                    x=stock_df["trade_date"],
                    y=stock_df["netprofit_avg"],
                    name="预测净利润（百万）",
                    mode="lines+markers",
                    line=dict(color="#1f77b4", width=2),
                    marker=dict(size=4),
                    hovertemplate="日期: %{x}<br>预测净利润: %{y:,.0f} 百万<extra></extra>",
                ),
                secondary_y=False,
            )

            fig.add_trace(
                go.Scatter(
                    x=stock_df["trade_date"],
                    y=stock_df["close_hkd"],
                    name="股价（HKD）",
                    mode="lines+markers",
                    line=dict(color="#ff7f0e", width=2, dash="dash"),
                    marker=dict(size=4),
                    hovertemplate="日期: %{x}<br>股价: %{y:,.2f} HKD<extra></extra>",
                ),
                secondary_y=True,
            )

            fig.update_layout(
                title=dict(text=f"{selected_name} ({selected_code})", x=0.5),
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
                margin=dict(l=60, r=60, t=80, b=40),
                height=500,
            )

            fig.update_yaxes(title_text="预测净利润（百万）", secondary_y=False)
            fig.update_yaxes(title_text="股价（HKD）", secondary_y=True)
            fig.update_xaxes(title_text="日期")

            st.plotly_chart(fig, use_container_width=True)

            # 下方数据表格
            st.subheader("📋 完整数据")
            display_df = stock_df[["trade_date", "netprofit_avg", "close_hkd"]].copy()
            display_df.columns = ["日期", "预测净利润平均值（百万）", "股价（HKD）"]
            display_df["日期"] = display_df["日期"].dt.strftime("%Y-%m-%d")
            display_df["预测净利润平均值（百万）"] = display_df["预测净利润平均值（百万）"].round(2)
            display_df["股价（HKD）"] = display_df["股价（HKD）"].round(2)

            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "日期": st.column_config.TextColumn("日期", width="small"),
                    "预测净利润平均值（百万）": st.column_config.NumberColumn("预测净利润平均值（百万）", format="%.2f"),
                    "股价（HKD）": st.column_config.NumberColumn("股价（HKD）", format="%.2f"),
                },
            )


# ============== Tab 3: 静态指标 ==============
with tab_static:
    st.subheader(f"一致预期静态指标 — {selected_date}")
    if static_df.empty:
        st.warning("暂无静态指标数据。")
    else:
        rename_map = {
            "wind_code": "股票代码",
            "name": "公司名称",
            "inst_num_2025": "2025预测机构家数",
            "netprofit_avg_2025": "2025预测净利润平均(百万)",
            "netprofit_median_2025": "2025预测净利润中值(百万)",
            "inst_num_2026": "2026预测机构家数",
            "netprofit_avg_2026": "2026预测净利润平均(百万)",
            "netprofit_median_2026": "2026预测净利润中值(百万)",
        }
        display_cols = [c for c in rename_map.keys() if c in static_df.columns]
        display_df = static_df[display_cols].rename(columns=rename_map)

        # 数值格式化
        for col in display_df.columns:
            if "百万" in col:
                display_df[col] = display_df[col].round(2)
            elif "家数" in col:
                display_df[col] = display_df[col].fillna(0).astype(int)

        st.dataframe(display_df, use_container_width=True, hide_index=True)
        st.caption("数据单位：百万元人民币 | 来源：Wind 一致预期")
