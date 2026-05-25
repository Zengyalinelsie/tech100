"""各 Tab 顶部的「一句话白话结论」生成器——纯函数,无 Streamlit 依赖。

输入 dashboard 已算好的现成变量,输出一句中文结论字符串,给非量化用户一个
"所以呢 / 我该怎么看"的落点。V8 看板重构成四模块后可直接 import 复用。
"""
from __future__ import annotations

import math


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def verdict_market(best_lag, best_r, best_sig) -> str:
    """Tab 市场整体:预期 vs 股价 的领先滞后结论。best_sig 含 '★' 表示显著。"""
    if not _ok(best_lag) or not _ok(best_r):
        return "数据不足,暂无市场级结论。"
    sig = "★" in str(best_sig or "")
    if best_lag > 0:
        tail = "、统计显著 → 可顺势用预期修正选股" if sig else ",但未达显著,信号偏弱、谨慎参考"
        return f"本周:预期修正**领先**股价 {best_lag} 周(r={best_r:+.3f}){tail}。"
    if best_lag < 0:
        tail = "、且显著" if sig else ",但未达显著"
        return f"本周:**股价领先**预期 {abs(best_lag)} 周(r={best_r:+.3f}){tail},市场抢跑基本面。"
    return "本周:预期与股价基本**同步**,信息已被快速定价,预期信号的领先优势有限。"


def verdict_rank(n_lead_exp, n_lead_price, n_sig, total) -> str:
    """Tab 排行榜:全市场领先/滞后分布与显著占比。"""
    if not total:
        return "数据不足,暂无排行结论。"
    pct = n_sig / total
    note = "显著占比偏低,个股联动结论需谨慎。" if pct < 0.3 else "显著占比可观,联动结构较可信。"
    return (f"{total} 只中 **{n_lead_exp} 只预期领先**、{n_lead_price} 只股价领先;"
            f"{n_sig} 只达统计显著({pct:.0%})。{note}")


def verdict_stock(name, best_lag, best_r) -> str:
    """Tab 个股深度:单只股票的预期-股价领先滞后。"""
    if not _ok(best_lag) or not _ok(best_r):
        return f"{name}:样本不足,暂无法判断预期与股价的领先滞后。"
    if best_lag > 0:
        return f"{name}:预期修正**领先**股价约 {best_lag} 周(r={best_r:+.3f}),预期变动对其股价有参考价值。"
    if best_lag < 0:
        return f"{name}:**股价领先**预期约 {abs(best_lag)} 周(r={best_r:+.3f}),股价先动、预期后补。"
    return f"{name}:预期与股价基本同步(r={best_r:+.3f})。"


def verdict_backtest(ic_mean, ic_ir, dsr=None, long_ir=None) -> str:
    """Tab 回测:选股力是否达标 + 多重检验后的稳健性。"""
    if not _ok(ic_mean):
        return "数据不足,无法判断选股力。"
    if dsr is not None and _ok(dsr):
        ds = f" Deflated Sharpe={dsr:.2f}" + ("(扣多重检验后仍稳健)" if dsr > 0.95
                                              else "(扣多重检验后存疑,警惕过拟合)")
    else:
        ds = ""
    if ic_mean > 0.03:
        return f"**选股力达标**:IC {ic_mean:+.4f}、ICIR {ic_ir:+.2f},信号有稳定预测力。{ds}"
    if ic_mean > 0:
        ir_txt = f",优先看多头相对基准 IR={long_ir:+.2f}" if _ok(long_ir) else ""
        return f"IC **弱正**({ic_mean:+.4f}),选股力未充分体现{ir_txt}。{ds}"
    return f"IC **非正**({ic_mean:+.4f}),当前信号缺乏预测力,建议换因子或调权重。{ds}"


def verdict_screen(top_name=None, neutralize=None, date=None) -> str:
    """Tab 多因子选股:今日清单一句话 + 中性化状态提醒。"""
    if neutralize:
        label = "行业+市值" if "size" in str(neutralize) else "行业"
        neu = f"已做**{label}中性化**(剔除行业/市值净暴露)"
    else:
        neu = "**未做**行业/市值中性化(排名可能偏向某些行业/大市值)"
    head = f"按当前权重,{date} 综合总分第一为 **{top_name}**。" if top_name else "按当前权重生成今日清单。"
    return f"{head}{neu};下单前请先看下方回测的 IC 与 Deflated Sharpe。"
