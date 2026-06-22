from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "07_backtest_trade_logs"


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    strategies = json.loads((ROOT / "01_strategy_universe" / "strategies.json").read_text(encoding="utf-8"))
    index_rows = read_csv_rows(OUT / "backtest_trade_log_index.csv")
    summary = json.loads((OUT / "backtest_trade_log_summary.json").read_text(encoding="utf-8"))
    warnings = json.loads((OUT / "backtest_trade_log_warnings.json").read_text(encoding="utf-8"))

    strategy_ids = {str(row.get("strategy_id") or "") for row in strategies}
    index_strategy_ids = {row["strategy_id"] for row in index_rows}
    assert strategy_ids <= index_strategy_ids, "index must include all strategies.json strategy ids"
    ok_rows = [row for row in index_rows if row["status"] in {"downloaded", "cached"}]
    missing_rows = [row for row in index_rows if row["status"] not in {"downloaded", "cached"}]
    assert summary["downloaded_files"] == len(ok_rows), "summary downloaded_files mismatch"
    assert summary["missing_files"] == len(missing_rows), "summary missing_files mismatch"
    assert summary["warnings"] == len(warnings), "summary warning count mismatch"
    assert len(ok_rows) > 0, "expected at least one downloaded trade log"

    checked = 0
    for row in ok_rows[:10]:
        path = ROOT / row["local_path"]
        assert path.exists(), f"missing local trade log: {path}"
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames and "date" in reader.fieldnames, f"trade log missing date column: {path}"
        checked += 1

    print({
        "strategies": len(strategies),
        "downloaded_files": len(ok_rows),
        "missing_files": len(missing_rows),
        "warnings": len(warnings),
        "sample_files_checked": checked,
        "downloaded_rows": summary["downloaded_rows"],
    })


if __name__ == "__main__":
    main()
