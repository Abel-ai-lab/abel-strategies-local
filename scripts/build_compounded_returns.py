from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAPER_OUT = ROOT / "02_paper_actuals"
BACKTEST_OUT = ROOT / "07_backtest_trade_logs"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    os.replace(tmp, path)


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def strategy_metadata() -> dict[str, dict]:
    strategies = read_json(ROOT / "01_strategy_universe" / "strategies.json")
    return {str(row.get("strategy_id") or ""): row for row in strategies}


def compound_pnl_rows(rows: list[dict], date_key: str, source_name: str) -> tuple[list[dict], dict, dict | None]:
    rows.sort(key=lambda row: str(row.get(date_key) or "")[:10])
    equity = 1.0
    sum_pnl = 0.0
    curve_rows = []
    for day_index, row in enumerate(rows, 1):
        trading_date = str(row.get(date_key) or "")[:10]
        if not trading_date:
            continue
        pnl = as_float(row.get("pnl"), 0.0)
        if pnl <= -1.0:
            return [], {"rows": 0, "sum_pnl": 0.0, "end_equity": "", "total_return": ""}, {
                "source": source_name,
                "strategy_id": str(row.get("strategy_id") or ""),
                "trading_date": trading_date,
                "pnl": pnl,
                "reason": "pnl_would_make_non_positive_equity",
            }
        equity *= 1.0 + pnl
        if equity <= 0.0:
            return [], {"rows": 0, "sum_pnl": 0.0, "end_equity": "", "total_return": ""}, {
                "source": source_name,
                "strategy_id": str(row.get("strategy_id") or ""),
                "trading_date": trading_date,
                "pnl": pnl,
                "reason": "non_positive_compounded_equity",
            }
        sum_pnl += pnl
        curve_rows.append({
            "trading_date": trading_date,
            "day_index": day_index,
            "pnl": pnl,
            "sum_pnl": sum_pnl,
            "compounded_equity": equity,
            "compounded_return": equity - 1.0,
        })
    if not curve_rows:
        return [], {"rows": 0, "sum_pnl": 0.0, "end_equity": "", "total_return": ""}, None
    return curve_rows, {
        "rows": len(curve_rows),
        "start_date": curve_rows[0]["trading_date"],
        "end_date": curve_rows[-1]["trading_date"],
        "sum_pnl": sum_pnl,
        "end_equity": equity,
        "total_return": equity - 1.0,
    }, None


def build_paper_compounded(strategy_by_id: dict[str, dict]) -> dict:
    paper_rows = read_json(ROOT / "02_paper_actuals" / "paper_daily_rows.json")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in paper_rows:
        strategy_id = str(row.get("strategy_id") or "")
        if strategy_id:
            grouped[strategy_id].append(row)

    curve_rows = []
    summary_rows = []
    warnings = []
    for strategy_id in sorted(grouped):
        strategy = strategy_by_id.get(strategy_id, {})
        curve, summary, warning = compound_pnl_rows(grouped[strategy_id], "trading_date", "paper")
        if warning:
            warning.update({
                "target_asset": strategy.get("target_asset", ""),
                "asset_type": strategy.get("asset_type", ""),
                "display_name": strategy.get("display_name", ""),
            })
            warnings.append(warning)
        if not curve:
            continue
        source_by_date = {str(row.get("trading_date") or "")[:10]: row for row in grouped[strategy_id]}
        for point in curve:
            source = source_by_date[point["trading_date"]]
            curve_rows.append({
                "strategy_id": strategy_id,
                "target_asset": strategy.get("target_asset", ""),
                "asset_type": strategy.get("asset_type", ""),
                "display_name": strategy.get("display_name", ""),
                "trading_date": point["trading_date"],
                "day_index": point["day_index"],
                "pnl": point["pnl"],
                "sum_pnl": point["sum_pnl"],
                "paper_compounded_equity": point["compounded_equity"],
                "paper_compounded_return": point["compounded_return"],
                "asset_return": source.get("asset_return", ""),
                "benchmark_return": source.get("benchmark_return", ""),
                "position": source.get("position", ""),
                "next_position": source.get("next_position", ""),
                "close": source.get("close", ""),
                "generated_by_run_id": source.get("generated_by_run_id", ""),
            })
        summary_rows.append({
            "strategy_id": strategy_id,
            "target_asset": strategy.get("target_asset", ""),
            "asset_type": strategy.get("asset_type", ""),
            "display_name": strategy.get("display_name", ""),
            "start_date": summary["start_date"],
            "end_date": summary["end_date"],
            "rows": summary["rows"],
            "paper_start_equity": 1.0,
            "paper_end_equity": summary["end_equity"],
            "paper_total_return": summary["total_return"],
            "paper_total_return_pct": summary["total_return"] * 100.0,
            "paper_sum_pnl": summary["sum_pnl"],
            "paper_sum_pnl_pct": summary["sum_pnl"] * 100.0,
        })

    curve_fields = [
        "strategy_id", "target_asset", "asset_type", "display_name", "trading_date", "day_index",
        "pnl", "sum_pnl", "paper_compounded_equity", "paper_compounded_return",
        "asset_return", "benchmark_return", "position", "next_position", "close", "generated_by_run_id",
    ]
    summary_fields = [
        "strategy_id", "target_asset", "asset_type", "display_name", "start_date", "end_date", "rows",
        "paper_start_equity", "paper_end_equity", "paper_total_return", "paper_total_return_pct",
        "paper_sum_pnl", "paper_sum_pnl_pct",
    ]
    write_csv(PAPER_OUT / "paper_compounded_curves.csv", curve_fields, curve_rows)
    write_csv(PAPER_OUT / "paper_compounded_summary.csv", summary_fields, summary_rows)
    write_json(PAPER_OUT / "paper_compounded_warnings.json", warnings)
    return {"curve_rows": len(curve_rows), "summary_rows": len(summary_rows), "warnings": len(warnings)}


def build_backtest_compounded() -> dict:
    index_rows = [
        row for row in read_csv_rows(BACKTEST_OUT / "backtest_trade_log_index.csv")
        if row.get("status") in {"downloaded", "cached"} and row.get("local_path")
    ]
    curve_rows = []
    summary_rows = []
    warnings = []
    for index_row in sorted(index_rows, key=lambda row: row["strategy_id"]):
        strategy_id = index_row["strategy_id"]
        local_path = index_row["local_path"]
        source_rows = read_csv_rows(ROOT / local_path)
        for row in source_rows:
            row["strategy_id"] = strategy_id
        curve, summary, warning = compound_pnl_rows(source_rows, "date", "backtest")
        if warning:
            warning.update({
                "target_asset": index_row.get("target_asset", ""),
                "asset_type": index_row.get("asset_type", ""),
                "display_name": index_row.get("display_name", ""),
                "local_path": local_path,
            })
            warnings.append(warning)
        if not curve:
            continue
        source_by_date = {str(row.get("date") or "")[:10]: row for row in source_rows}
        for point in curve:
            source = source_by_date[point["trading_date"]]
            curve_rows.append({
                "strategy_id": strategy_id,
                "target_asset": index_row.get("target_asset", ""),
                "asset_type": index_row.get("asset_type", ""),
                "display_name": index_row.get("display_name", ""),
                "trading_date": point["trading_date"],
                "day_index": point["day_index"],
                "pnl": point["pnl"],
                "sum_pnl": point["sum_pnl"],
                "backtest_compounded_equity": point["compounded_equity"],
                "backtest_compounded_return": point["compounded_return"],
                "asset_return": source.get("asset_return", ""),
                "position": source.get("position", ""),
                "next_position": source.get("next_position", ""),
                "gross_pnl": source.get("gross_pnl", ""),
                "turnover": source.get("turnover", ""),
                "execution_cost": source.get("execution_cost", ""),
                "source": source.get("source", ""),
                "local_path": local_path,
            })
        summary_rows.append({
            "strategy_id": strategy_id,
            "target_asset": index_row.get("target_asset", ""),
            "asset_type": index_row.get("asset_type", ""),
            "display_name": index_row.get("display_name", ""),
            "start_date": summary["start_date"],
            "end_date": summary["end_date"],
            "rows": summary["rows"],
            "backtest_start_equity": 1.0,
            "backtest_end_equity": summary["end_equity"],
            "backtest_total_return": summary["total_return"],
            "backtest_total_return_pct": summary["total_return"] * 100.0,
            "backtest_sum_pnl": summary["sum_pnl"],
            "backtest_sum_pnl_pct": summary["sum_pnl"] * 100.0,
            "index_backtest_total_return": index_row.get("backtest_total_return", ""),
            "local_path": local_path,
        })

    curve_fields = [
        "strategy_id", "target_asset", "asset_type", "display_name", "trading_date", "day_index",
        "pnl", "sum_pnl", "backtest_compounded_equity", "backtest_compounded_return",
        "asset_return", "position", "next_position", "gross_pnl", "turnover", "execution_cost", "source", "local_path",
    ]
    summary_fields = [
        "strategy_id", "target_asset", "asset_type", "display_name", "start_date", "end_date", "rows",
        "backtest_start_equity", "backtest_end_equity", "backtest_total_return", "backtest_total_return_pct",
        "backtest_sum_pnl", "backtest_sum_pnl_pct", "index_backtest_total_return", "local_path",
    ]
    write_csv(BACKTEST_OUT / "backtest_compounded_curves.csv", curve_fields, curve_rows)
    write_csv(BACKTEST_OUT / "backtest_compounded_summary.csv", summary_fields, summary_rows)
    write_json(BACKTEST_OUT / "backtest_compounded_warnings.json", warnings)
    return {"curve_rows": len(curve_rows), "summary_rows": len(summary_rows), "warnings": len(warnings)}


def update_manifest(summary: dict) -> None:
    manifest_path = ROOT / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = read_json(manifest_path)
    counts = dict(manifest.get("counts", {}))
    counts.update({
        "paperCompoundedCurveRows": summary["paper"]["curve_rows"],
        "paperCompoundedSummaryRows": summary["paper"]["summary_rows"],
        "backtestCompoundedCurveRows": summary["backtest"]["curve_rows"],
        "backtestCompoundedSummaryRows": summary["backtest"]["summary_rows"],
        "paperCompoundedWarnings": summary["paper"]["warnings"],
        "backtestCompoundedWarnings": summary["backtest"]["warnings"],
    })
    manifest["counts"] = {key: counts[key] for key in sorted(counts)}
    now = summary["generated_at"]
    manifest["generatedAt"] = now
    manifest["refreshedAt"] = now
    files = manifest.setdefault("files", [])
    for file_name in [
        "02_paper_actuals/paper_compounded_curves.csv",
        "02_paper_actuals/paper_compounded_summary.csv",
        "02_paper_actuals/paper_compounded_warnings.json",
        "07_backtest_trade_logs/backtest_compounded_curves.csv",
        "07_backtest_trade_logs/backtest_compounded_summary.csv",
        "07_backtest_trade_logs/backtest_compounded_warnings.json",
    ]:
        if file_name not in files:
            files.append(file_name)
    write_json(manifest_path, manifest)


def main() -> None:
    strategy_by_id = strategy_metadata()
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Compounded from daily pnl fields: equity_t = equity_(t-1) * (1 + pnl_t).",
        "paper": build_paper_compounded(strategy_by_id),
        "backtest": build_backtest_compounded(),
    }
    write_json(ROOT / "references" / "compounded_returns_summary.json", summary)
    update_manifest(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
