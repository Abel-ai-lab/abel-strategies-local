from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAPER_CURVES = ROOT / "02_paper_actuals" / "paper_compounded_curves.csv"
PAPER_SUMMARY = ROOT / "02_paper_actuals" / "paper_compounded_summary.csv"
BACKTEST_CURVES = ROOT / "07_backtest_trade_logs" / "backtest_compounded_curves.csv"
BACKTEST_SUMMARY = ROOT / "07_backtest_trade_logs" / "backtest_compounded_summary.csv"
PAPER_WARNINGS = ROOT / "02_paper_actuals" / "paper_compounded_warnings.json"
BACKTEST_WARNINGS = ROOT / "07_backtest_trade_logs" / "backtest_compounded_warnings.json"


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_json_optional(path: Path):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def check_compounded_rows(rows: list[dict[str, str]], id_field: str, equity_field: str, return_field: str) -> dict[str, dict]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row[id_field]].append(row)

    summary = {}
    for strategy_id, strategy_rows in grouped.items():
        strategy_rows.sort(key=lambda row: (row["trading_date"], int(row["day_index"])))
        equity = 1.0
        for expected_day_index, row in enumerate(strategy_rows, 1):
            assert int(row["day_index"]) == expected_day_index, f"{strategy_id} day_index mismatch"
            pnl = float(row["pnl"] or 0.0)
            assert pnl > -1.0, f"{strategy_id} pnl would make non-positive equity on {row['trading_date']}"
            equity *= 1.0 + pnl
            assert abs(float(row[equity_field]) - equity) < 1e-12, f"{strategy_id} compounded equity mismatch on {row['trading_date']}"
            assert abs(float(row[return_field]) - (equity - 1.0)) < 1e-12, f"{strategy_id} compounded return mismatch on {row['trading_date']}"
        summary[strategy_id] = {
            "rows": len(strategy_rows),
            "start_date": strategy_rows[0]["trading_date"],
            "end_date": strategy_rows[-1]["trading_date"],
            "end_equity": equity,
            "total_return": equity - 1.0,
        }
    return summary


def main() -> None:
    assert PAPER_CURVES.exists(), f"missing scripted paper curves: {PAPER_CURVES}"
    assert PAPER_SUMMARY.exists(), f"missing scripted paper summary: {PAPER_SUMMARY}"
    assert BACKTEST_CURVES.exists(), f"missing scripted backtest curves: {BACKTEST_CURVES}"
    assert BACKTEST_SUMMARY.exists(), f"missing scripted backtest summary: {BACKTEST_SUMMARY}"

    paper_daily_rows = json.loads((ROOT / "02_paper_actuals" / "paper_daily_rows.json").read_text(encoding="utf-8"))
    paper_warning_ids = {str(row.get("strategy_id") or "") for row in read_json_optional(PAPER_WARNINGS)}
    expected_paper_rows = [row for row in paper_daily_rows if str(row.get("strategy_id") or "") not in paper_warning_ids]
    paper_curve_rows = read_csv_rows(PAPER_CURVES)
    paper_summary_rows = read_csv_rows(PAPER_SUMMARY)
    assert len(paper_curve_rows) == len(expected_paper_rows), "paper compounded curve row count must match valid paper_daily_rows.json rows"
    paper_expected = check_compounded_rows(paper_curve_rows, "strategy_id", "paper_compounded_equity", "paper_compounded_return")
    assert len(paper_summary_rows) == len(paper_expected), "paper summary row count mismatch"
    for row in paper_summary_rows:
        expected = paper_expected[row["strategy_id"]]
        assert int(row["rows"]) == expected["rows"], f"paper rows mismatch for {row['strategy_id']}"
        assert row["start_date"] == expected["start_date"], f"paper start date mismatch for {row['strategy_id']}"
        assert row["end_date"] == expected["end_date"], f"paper end date mismatch for {row['strategy_id']}"
        assert abs(float(row["paper_end_equity"]) - expected["end_equity"]) < 1e-12, f"paper end equity mismatch for {row['strategy_id']}"
        assert abs(float(row["paper_total_return"]) - expected["total_return"]) < 1e-12, f"paper total return mismatch for {row['strategy_id']}"

    backtest_index_rows = [
        row for row in read_csv_rows(ROOT / "07_backtest_trade_logs" / "backtest_trade_log_index.csv")
        if row["status"] in {"downloaded", "cached"}
    ]
    backtest_warning_ids = {str(row.get("strategy_id") or "") for row in read_json_optional(BACKTEST_WARNINGS)}
    expected_backtest_rows = sum(int(row["row_count"] or 0) for row in backtest_index_rows if row["strategy_id"] not in backtest_warning_ids)
    backtest_curve_rows = read_csv_rows(BACKTEST_CURVES)
    backtest_summary_rows = read_csv_rows(BACKTEST_SUMMARY)
    assert len(backtest_curve_rows) == expected_backtest_rows, "backtest compounded curve row count must match downloaded trade-log rows"
    backtest_expected = check_compounded_rows(backtest_curve_rows, "strategy_id", "backtest_compounded_equity", "backtest_compounded_return")
    assert len(backtest_summary_rows) == len(backtest_expected), "backtest summary row count mismatch"
    for row in backtest_summary_rows:
        expected = backtest_expected[row["strategy_id"]]
        assert int(row["rows"]) == expected["rows"], f"backtest rows mismatch for {row['strategy_id']}"
        assert row["start_date"] == expected["start_date"], f"backtest start date mismatch for {row['strategy_id']}"
        assert row["end_date"] == expected["end_date"], f"backtest end date mismatch for {row['strategy_id']}"
        assert abs(float(row["backtest_end_equity"]) - expected["end_equity"]) < 1e-12, f"backtest end equity mismatch for {row['strategy_id']}"
        assert abs(float(row["backtest_total_return"]) - expected["total_return"]) < 1e-12, f"backtest total return mismatch for {row['strategy_id']}"

    print({
        "paper_curve_rows": len(paper_curve_rows),
        "paper_strategies": len(paper_summary_rows),
        "backtest_curve_rows": len(backtest_curve_rows),
        "backtest_strategies": len(backtest_summary_rows),
    })


if __name__ == "__main__":
    main()
