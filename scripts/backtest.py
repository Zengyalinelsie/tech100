"""历史回测 CLI：跑多空回测 + 导出 INF 对接信号文件。

用法（项目根目录）：
    uv run python scripts/backtest.py                       # 默认 fy1, hold=1
    uv run python scripts/backtest.py --signal fy2 --hold 4 --top-pct 0.2 --cost-bps 30

产出：
    data/backtest/nav_{signal}_h{hold}.csv        # 四条线 NAV
    data/backtest/metrics_{signal}_h{hold}.csv    # 指标表
    data/backtest/trades_{signal}_h{hold}.csv     # 每次换仓的多/空名单
    data/signals/signal_{date}.csv                # 最新一周 20买/20卖（INF 可读）
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT / "app"))

from factors import build_factor_panel          # noqa: E402
from strategy import backtest, latest_signal     # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--signal", choices=["fy1", "fy2"], default="fy1")
    p.add_argument("--hold", type=int, default=1, help="持有/换仓周数")
    p.add_argument("--top-pct", type=float, default=0.2)
    p.add_argument("--cost-bps", type=float, default=30.0)
    p.add_argument("--start", default=None, help="回测起始日 YYYY-MM-DD")
    args = p.parse_args()

    x_col = f"{args.signal}_rev_norm"
    out_bt = ROOT / "data" / "backtest"
    out_sig = ROOT / "data" / "signals"
    out_bt.mkdir(parents=True, exist_ok=True)
    out_sig.mkdir(parents=True, exist_ok=True)

    print(f"构建因子面板…")
    fac = build_factor_panel()

    print(f"回测：signal={x_col} hold={args.hold}w top={args.top_pct:.0%} cost={args.cost_bps}bps")
    res = backtest(fac, x_col=x_col, hold_weeks=args.hold,
                   top_pct=args.top_pct, cost_bps=args.cost_bps, start=args.start)

    tag = f"{args.signal}_h{args.hold}"
    res["nav"].to_csv(out_bt / f"nav_{tag}.csv")
    res["metrics"].round(4).to_csv(out_bt / f"metrics_{tag}.csv")
    res["trades"].to_csv(out_bt / f"trades_{tag}.csv", index=False)

    print("\n=== 指标 ===")
    print(res["metrics"].round(3).to_string())
    print("\n=== IC ===", {k: round(v, 4) for k, v in res["ic_summary"].items()})
    print("\n=== NAV 末值 ===")
    print(res["nav"].iloc[-1].round(3).to_string())

    sig = latest_signal(fac, x_col=x_col, top_pct=args.top_pct)
    date_tag = str(sig["trade_date"].iloc[0])
    sig_path = out_sig / f"signal_{date_tag}.csv"
    sig.to_csv(sig_path, index=False)
    print(f"\n本周选股已导出（INF 可读）：{sig_path}")
    print(sig.to_string(index=False))


if __name__ == "__main__":
    main()
