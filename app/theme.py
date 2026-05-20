"""
全局视觉主题：莫兰迪配色 + matplotlib/plotly 共用样式。
所有图表请从这里取色，禁止在调用处硬编码颜色。
"""

PALETTE = {
    "up":       "#8ECFC9",
    "down":     "#FA7F6F",
    "neutral":  "#999999",
    "price":    "#82B0D2",
    "forecast": "#FFBE7A",
    "band":     "#BEB8DC",
    "bg":       "#E7DAD2",
    "title":    "#2C3E50",
}

PLOTLY_LAYOUT = dict(
    font=dict(family="PingFang SC, Helvetica, Arial", size=12, color="#333"),
    plot_bgcolor="white",
    paper_bgcolor="white",
    xaxis=dict(gridcolor="#EEE", zerolinecolor="#DDD"),
    yaxis=dict(gridcolor="#EEE", zerolinecolor="#DDD"),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
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
h1, h2, h3, h4 { color: #2C3E50; }
[data-testid="stMetricLabel"] { color: #555; font-weight: 500; }
[data-testid="stMetricValue"] { color: #2C3E50; }
section[data-testid="stSidebar"] { background-color: #FAFAF8; }
.stTabs [data-baseweb="tab-list"] { gap: 8px; }
.stTabs [data-baseweb="tab"] {
    background: #F5F1EC;
    border-radius: 6px 6px 0 0;
    padding: 8px 16px;
}
.stTabs [aria-selected="true"] {
    background: #82B0D2 !important;
    color: white !important;
}
</style>
"""
