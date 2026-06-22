from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPORARY_EXCLUDED_SYMBOLS = {"PYPL", "TMUS", "DDOG"}


def read_json(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def read_csv_rows(path: str) -> list[dict[str, str]]:
    with (ROOT / path).open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    strategies = read_json("01_strategy_universe/strategies.json")
    strategy_ids = {str(row["strategy_id"]) for row in strategies}
    target_tickers = {line.strip() for line in (ROOT / "01_strategy_universe/tickers.txt").read_text(encoding="utf-8").splitlines() if line.strip()}
    strategy_ticker = {str(row["strategy_id"]): row.get("target_asset") for row in strategies}
    paper_rows = read_json("02_paper_actuals/paper_daily_rows.json")
    paper_target_tickers = {strategy_ticker.get(str(row["strategy_id"])) for row in paper_rows if strategy_ticker.get(str(row["strategy_id"]))}

    market_rows = read_json("03_market_data/buy_hold_cap_day_bars.json")
    market_tickers = {row.get("ticker") for row in market_rows if row.get("ticker")}
    missing_market = sorted(paper_target_tickers - market_tickers)
    extra_market = sorted(market_tickers - target_tickers)
    assert not missing_market, f"03_market_data missing paper target tickers: {missing_market}"
    assert not extra_market, f"03_market_data has stale extra tickers: {extra_market[:20]}"

    buy_hold_summary = read_csv_rows("05_comparisons/strategy_buy_hold_summary.csv")
    assert len(buy_hold_summary) == len(strategy_ids), "buy-hold summary row count must match current strategies"
    assert {row["strategy_id"] for row in buy_hold_summary} == strategy_ids, "buy-hold summary strategy IDs are stale"

    comparison_rows = read_csv_rows("05_comparisons/paper_minus_benchmarks_sorted.csv")
    comparison_ids = {row["strategy_id"] for row in comparison_rows}
    assert comparison_ids == strategy_ids, "comparison output must contain exactly the current strategy universe"
    assert len(comparison_rows) == len(strategy_ids), "comparison output must have one row per current strategy"

    final_rows = read_csv_rows("05_comparisons/final_abel_portfolio_selection_latest_available.csv")
    assert {row["strategy_id"] for row in final_rows} <= strategy_ids, "final selection contains stale strategy IDs"
    assert not (TEMPORARY_EXCLUDED_SYMBOLS & {row["ticker"] for row in final_rows}), "final selection contains temporarily excluded symbols"
    assert len(final_rows) <= 30, "final selection should cap at 30 rows"

    manifest = read_json("manifest.json")
    assert manifest["counts"]["buyHoldCapBars"] == len(market_rows), "manifest buyHoldCapBars is stale"
    assert manifest["counts"]["strategyBuyHoldSummaryRows"] == len(buy_hold_summary), "manifest buy-hold summary count is stale"
    assert manifest["counts"]["paperMinusBenchmarksRows"] == len(comparison_rows), "manifest comparison count is stale"
    assert manifest["counts"]["finalAbelPortfolioLatestAvailableRows"] == len(final_rows), "manifest final selection count is stale"

    print({
        "target_tickers": len(target_tickers),
        "paper_target_tickers": len(paper_target_tickers),
        "market_rows": len(market_rows),
        "buy_hold_summary_rows": len(buy_hold_summary),
        "comparison_rows": len(comparison_rows),
        "final_rows": len(final_rows),
    })


if __name__ == "__main__":
    main()
