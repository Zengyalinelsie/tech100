"""
全局视觉主题：深蓝-红梯度配色（红涨蓝跌）+ matplotlib/plotly 共用样式。
所有图表请从这里取色，禁止在调用处硬编码颜色。
"""

PALETTE = {
    "up":        "#E9B5C1",  # 柔和粉 — 涨/正/上调事件
    "up_soft":   "#F4D8DF",  # 极浅粉 — 辅助
    "down":      "#B4C5D9",  # 柔和浅蓝 — 跌/负/下调事件
    "down_soft": "#D6E0EC",  # 极浅蓝 — 辅助/CI 带
    "price":     "#5A88B0",  # 中度蓝 — 股价线（保留可读性）
    "forecast":  "#F2A07B",  # 中橙 — 预期/净利润线
    "neutral":   "#9A9A9A",  # 中灰 — 同步/不显著
    "band":      "rgba(180,197,217,0.35)",
    "axis":      "#333333",  # 坐标轴文字
    "title":     "#1F1F1F",  # 标题
}

PLOTLY_LAYOUT = dict(
    font=dict(family="PingFang SC, Helvetica, Arial", size=12, color=PALETTE["axis"]),
    plot_bgcolor="white",
    paper_bgcolor="white",
    xaxis=dict(
        gridcolor="#E5E5E5", zerolinecolor="#CCCCCC",
        tickfont=dict(color=PALETTE["axis"], size=11),
        title_font=dict(color=PALETTE["axis"], size=12),
    ),
    yaxis=dict(
        gridcolor="#E5E5E5", zerolinecolor="#CCCCCC",
        tickfont=dict(color=PALETTE["axis"], size=11),
        title_font=dict(color=PALETTE["axis"], size=12),
    ),
    legend=dict(
        orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5,
        font=dict(color=PALETTE["axis"], size=11),
    ),
    margin=dict(l=50, r=30, t=60, b=40),
)


def bar_color(values):
    return [PALETTE["up"] if v >= 0 else PALETTE["down"] for v in values]


def lag_color(values):
    out = []
    for v in values:
        if v > 0:
            out.append(PALETTE["up"])
        elif v < 0:
            out.append(PALETTE["down"])
        else:
            out.append(PALETTE["neutral"])
    return out


CUSTOM_CSS = """
<style>
html, body, [class*="css"] {
    font-family: "PingFang SC", Helvetica, Arial, sans-serif;
}
h1, h2, h3, h4 { color: #1F1F1F; }
[data-testid="stMetricLabel"] { color: #555; font-weight: 500; }
[data-testid="stMetricValue"] { color: #1F1F1F; }
section[data-testid="stSidebar"] { background-color: #FAFAFA; }
.stTabs [data-baseweb="tab-list"] { gap: 8px; }
.stTabs [data-baseweb="tab"] {
    background: #F2F2F2;
    border-radius: 6px 6px 0 0;
    padding: 8px 16px;
    color: #333;
}
.stTabs [aria-selected="true"] {
    background: #B4C5D9 !important;
    color: #1F1F1F !important;
    font-weight: 600;
}
</style>
"""
