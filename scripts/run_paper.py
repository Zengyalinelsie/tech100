"""活体纸面组合：从本周起每周记录一次持仓，累积真实 track record。

与历史回测的区别：回测是"一次性回看 5.4 年"；纸面组合是"每周记一笔、向前累积"，
持仓落盘可审计，NAV 用最新价对历史持仓 mark-to-market 重算。

用法（项目根目录）：
    uv run python scripts/run_paper.py                 # fy1, top 20%
    uv run python scripts/run_paper.py --signal fy2

每周（或每个交易日采集后）跑一次即可。已挂进 run_daily.py。

产出：
    data/paper/positions.csv   每周持仓（trade_date, wind_code, name, side, weight, entry_close）
    data/paper/nav.csv         多头/空头/多空/恒生科技 NAV（向前累积）
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT / "app"))

from factors import build_factor_panel            # noqa: E402
from strategy import latest_signal, _bench_returns  # noqa: E402

PAPER_DIR = ROOT / "data" / "paper"
POS_PATH = PAPER_DIR / "positions.csv"
NAV_PATH = PAPER_DIR / "nav.csv"


def record_week(fac, x_col, top_pct):
    """把最新一周的持仓追加到 positions.csv（按日期去重，幂等）。"""
    sig = latest_signal(fac, x_col=x_col, top_pct=top_pct)
    sig = sig.rename(columns={"ref_close_hkd": "entry_close"})[
        ["trade_date", "wind_code", "name", "side", "target_weight", "entry_close"]
    ].rename(columns={"target_weight": "weight"})
    this_date = str(sig["trade_date"].iloc[0])

    if POS_PATH.exists():
        old = pd.read_csv(POS_PATH)
        if this_date in old["trade_date"].astype(str).values:
            print(f"ℹ️  {this_date} 已记录，跳过追加。")
            return old, this_date
        new = pd.concat([old, sig], ignore_index=True)
    else:
        new = sig
    new.to_csv(POS_PATH, index=False)
    print(f"✅ 记录持仓 {this_date}：多 {(sig.side=='LONG').sum()} 只 / 空 {(sig.side=='SHORT').sum()} 只")
    return new, this_date


def recompute_nav(fac):
    """用 panel 收益对 positions.csv 里历次持仓做 mark-to-market，向前累积 NAV。"""
    if not POS_PATH.exists():
        return pd.DataFrame()
    pos = pd.read_csv(POS_PATH)
    pos["trade_date"] = pd.to_datetime(pos["trade_date"])

    fac = fac.copy()
    fac["trade_date"] = pd.to_datetime(fac["trade_date"])
    ret_lookup = fac.set_index(["trade_date", "wind_code"])["ret"]
    all_dates = sorted(fac["trade_date"].unique())
    next_of = {d: (all_dates[i + 1] if i + 1 < len(all_dates) else None)
               for i, d in enumerate(all_dates)}

    rows = []
    for d, g in pos.groupby("trade_date"):
        nxt = next_of.get(d)
        if nxt is None:        # 最新一周还没有"下一周"收益，留待下次
            continue
        longs = g[g.side == "LONG"]["wind_code"].tolist()
        shorts = g[g.side == "SHORT"]["wind_code"].tolist()
        lr = ret_lookup.reindex([(nxt, c) for c in longs]).mean()
        sr = ret_lookup.reindex([(nxt, c) for c in shorts]).mean()
        lr = 0.0 if pd.isna(lr) else float(lr)
        sr = 0.0 if pd.isna(sr) else float(sr)
        rows.append({"trade_date": nxt, "long_ret": lr,
                     "short_ret": -sr, "ls_ret": lr - sr})

    if not rows:
        print("ℹ️  持仓不足 2 周，NAV 暂不可算（下周起累积）。")
        return pd.DataFrame()

    wk = pd.DataFrame(rows).set_index("trade_date").sort_index()
    bench = _bench_returns().reindex(wk.index)
    nav = pd.DataFrame({
        "多头": (1 + wk["long_ret"]).cumprod(),
        "空头": (1 + wk["short_ret"]).cumprod(),
        "多空": (1 + wk["ls_ret"]).cumprod(),
        "恒生科技": (1 + bench.fillna(0)).cumprod(),
    })
    nav.to_csv(NAV_PATH)
    print(f"✅ NAV 已更新（{len(nav)} 周）：")
    print(nav.iloc[-1].round(4).to_string())
    return nav


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--signal", choices=["fy1", "fy2"], default="fy1")
    p.add_argument("--top-pct", type=float, default=0.2)
    args = p.parse_args()
    x_col = f"{args.signal}_rev_norm"

    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    fac = build_factor_panel()
    record_week(fac, x_col, args.top_pct)
    recompute_nav(fac)


if __name__ == "__main__":
    main()
