from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SYMBOLS = {"QQQ", "BTCUSD"}


def read_csv_rows(path: str) -> list[dict[str, str]]:
    with (ROOT / path).open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    summary = read_csv_rows("03_market_data/portfolio_benchmark_summary.csv")
    by_symbol = {row["symbol"]: row for row in summary}
    missing = REQUIRED_SYMBOLS - set(by_symbol)
    assert not missing, f"missing benchmark summary symbols: {sorted(missing)}"
    for symbol in sorted(REQUIRED_SYMBOLS):
        row = by_symbol[symbol]
        assert int(row["row_count"]) > 0, f"{symbol} has no benchmark rows"
        assert row["warning"] == "False", f"{symbol} summary is warning=True"
        assert row["first_trading_date"] <= row["requested_start_date"], f"{symbol} does not cover requested start"
        assert row["last_trading_date"] >= row["requested_end_date"], f"{symbol} does not cover requested end"

    bars = read_csv_rows("03_market_data/portfolio_benchmark_day_bars.csv")
    bar_symbols = {row["ticker"] for row in bars}
    assert REQUIRED_SYMBOLS <= bar_symbols, "benchmark bars missing required symbols"
    warnings = json.loads((ROOT / "03_market_data/portfolio_benchmark_warnings.json").read_text(encoding="utf-8"))
    assert warnings == [], "portfolio benchmark warnings must be empty"
    print({
        "symbols": sorted(REQUIRED_SYMBOLS),
        "rows": len(bars),
        "summary": {symbol: int(by_symbol[symbol]["row_count"]) for symbol in sorted(REQUIRED_SYMBOLS)},
    })


if __name__ == "__main__":
    main()
