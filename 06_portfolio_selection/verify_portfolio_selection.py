from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent


def read_csv_rows(path: str) -> list[dict[str, str]]:
    with (ROOT / path).open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_project_csv_rows(path: str) -> list[dict[str, str]]:
    with (PROJECT_ROOT / path).open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def pnl_compounded_curve(local_path: str) -> dict[str, float]:
    equity = 1.0
    curve: dict[str, float] = {}
    for row in read_project_csv_rows(local_path):
        trading_date = str(row.get("date") or "")[:10]
        if not trading_date:
            continue
        pnl = float(row.get("pnl") or 0.0)
        assert pnl > -1.0, f"pnl would make non-positive equity in {local_path} on {trading_date}"
        equity *= 1.0 + pnl
        assert equity > 0.0, f"non-positive pnl-compounded equity in {local_path} on {trading_date}"
        curve[trading_date] = equity
    return curve


def expected_pnl_compounded_portfolio_curve(component_rows: list[dict[str, str]]) -> dict[str, float]:
    covered = [row for row in component_rows if row["status"] == "covered"]
    curves = {row["symbol"]: pnl_compounded_curve(row["trade_log_path"]) for row in covered}
    common_dates = sorted(set.intersection(*(set(curve) for curve in curves.values()))) if curves else []
    assert common_dates, "expected covered backtest trade logs to have common dates"
    start_values = {symbol: curves[symbol][common_dates[0]] for symbol in curves}
    weight = 1.0 / len(curves)
    return {
        trading_date: sum(weight * (curves[symbol][trading_date] / start_values[symbol]) for symbol in curves)
        for trading_date in common_dates
    }


def main() -> None:
    stock = read_csv_rows("stock_equal_weight_portfolio.csv")
    crypto = read_csv_rows("crypto_equal_weight_portfolio.csv")
    summary = read_csv_rows("portfolio_vs_benchmark_summary.csv")
    curve_rows = read_csv_rows("portfolio_equity_curves.csv")
    curve_metrics = read_csv_rows("portfolio_curve_metrics.csv")
    backtest_components = read_csv_rows("portfolio_backtest_components.csv")
    backtest_rows = read_csv_rows("portfolio_backtest_curves.csv")
    backtest_metrics = read_csv_rows("portfolio_backtest_metrics.csv")
    audit = json.loads((ROOT / "selection_audit.json").read_text(encoding="utf-8"))
    curve_handoff = json.loads((ROOT / "portfolio_equity_curves.json").read_text(encoding="utf-8"))

    assert len(stock) == 100, f"stock portfolio must have 100 components, got {len(stock)}"
    assert len(crypto) == 10, f"crypto portfolio must have 10 components, got {len(crypto)}"
    assert len({row["symbol"] for row in stock}) == 100, "stock symbols must be unique"
    assert len({row["symbol"] for row in crypto}) == 10, "crypto symbols must be unique"
    assert all(row["asset_type"] == "equity" for row in stock), "stock portfolio must contain only equity rows"
    assert all(row["asset_type"] == "crypto" for row in crypto), "crypto portfolio must contain only crypto rows"
    assert all(row.get("strategy_id") for row in stock), "stock portfolio rows must have strategy_id"
    assert all(row.get("strategy_id") for row in crypto), "crypto portfolio rows must have strategy_id"
    assert not ({"PYPL", "TMUS", "DDOG"} & {row["symbol"] for row in stock + crypto}), "temporarily excluded symbols must not be selected"
    assert "BTCUSD" not in {row["symbol"] for row in crypto}, "crypto portfolio must exclude BTCUSD benchmark"

    stock_weight = sum(float(row["weight"]) for row in stock)
    crypto_weight = sum(float(row["weight"]) for row in crypto)
    assert abs(stock_weight - 1.0) < 1e-9, f"stock weights must sum to 1, got {stock_weight}"
    assert abs(crypto_weight - 1.0) < 1e-9, f"crypto weights must sum to 1, got {crypto_weight}"

    by_portfolio = {row["portfolio"]: row for row in summary}
    assert set(by_portfolio) == {"stock_equal_weight_100", "crypto_equal_weight_10"}, "summary portfolios mismatch"
    assert by_portfolio["stock_equal_weight_100"]["benchmark_symbol"] == "QQQ", "stock benchmark must be QQQ"
    assert by_portfolio["crypto_equal_weight_10"]["benchmark_symbol"] == "BTCUSD", "crypto benchmark must be BTCUSD"
    for row in summary:
        assert row["beats_benchmark"] == "Y", f"{row['portfolio']} must beat benchmark after edge optimization"
        assert float(row["portfolio_return"]) > float(row["benchmark_return"]), f"{row['portfolio']} return <= benchmark"

    assert audit["selection_rule"]["weighting"] == "Equal weight within each portfolio.", "audit weighting rule missing"
    assert "Maximize paper-window CAP total-return edge against the fixed benchmark" in audit["selection_principle"], "audit selection principle missing"
    assert "active paper-subscription evidence" in audit["selection_principle"], "audit evidence principle missing"

    stock_preferred = sum(1 for row in stock if row["preferred_candidate"] == "Y")
    stock_outside = sum(1 for row in stock if row["preferred_candidate"] == "N")
    crypto_preferred = sum(1 for row in crypto if row["preferred_candidate"] == "Y")
    crypto_outside = sum(1 for row in crypto if row["preferred_candidate"] == "N")
    stock_evidence = sum(1 for row in stock if row["evidence_candidate"] == "Y")
    stock_full_universe_fill = sum(1 for row in stock if row["evidence_candidate"] == "N")
    crypto_evidence = sum(1 for row in crypto if row["evidence_candidate"] == "Y")
    crypto_full_universe_fill = sum(1 for row in crypto if row["evidence_candidate"] == "N")
    assert stock_preferred + stock_outside == len(stock), "stock preferred flags must be Y/N"
    assert crypto_preferred + crypto_outside == len(crypto), "crypto preferred flags must be Y/N"
    assert stock_evidence + stock_full_universe_fill == len(stock), "stock evidence flags must be Y/N"
    assert crypto_evidence + crypto_full_universe_fill == len(crypto), "crypto evidence flags must be Y/N"

    stock_meta = audit["selection_metadata"]["stock"]
    crypto_meta = audit["selection_metadata"]["crypto"]
    assert stock_meta["selected_preferred_count"] == stock_preferred, "stock preferred count mismatch"
    assert stock_meta["selected_outside_count"] == stock_outside, "stock outside count mismatch"
    assert stock_meta["selected_evidence_count"] == stock_evidence, "stock evidence count mismatch"
    assert stock_meta["selected_full_universe_fill_count"] == stock_full_universe_fill, "stock full-universe fill count mismatch"
    assert crypto_meta["selected_preferred_count"] == crypto_preferred, "crypto preferred count mismatch"
    assert crypto_meta["selected_outside_count"] == crypto_outside, "crypto outside count mismatch"
    assert crypto_meta["selected_evidence_count"] == crypto_evidence, "crypto evidence count mismatch"
    assert crypto_meta["selected_full_universe_fill_count"] == crypto_full_universe_fill, "crypto full-universe fill count mismatch"

    curve_by_portfolio: dict[str, list[dict[str, str]]] = {}
    for row in curve_rows:
        curve_by_portfolio.setdefault(row["portfolio"], []).append(row)
    metric_by_portfolio = {row["portfolio"]: row for row in curve_metrics}
    backtest_by_portfolio: dict[str, list[dict[str, str]]] = {}
    for row in backtest_rows:
        backtest_by_portfolio.setdefault(row["portfolio"], []).append(row)
    backtest_component_by_portfolio: dict[str, list[dict[str, str]]] = {}
    for row in backtest_components:
        backtest_component_by_portfolio.setdefault(row["portfolio"], []).append(row)
    missing_strategy_components = [row for row in backtest_components if not row.get("strategy_id")]
    assert not missing_strategy_components, f"selected components must all have strategy_id rows: {missing_strategy_components}"
    backtest_metric_by_portfolio = {row["portfolio"]: row for row in backtest_metrics}
    assert set(curve_by_portfolio) == set(by_portfolio), "curve portfolios mismatch"
    assert set(metric_by_portfolio) == set(by_portfolio), "curve metric portfolios mismatch"
    assert set(backtest_by_portfolio) == set(by_portfolio), "backtest curve portfolios mismatch"
    assert set(backtest_component_by_portfolio) == set(by_portfolio), "backtest component portfolios mismatch"
    assert set(backtest_metric_by_portfolio) == set(by_portfolio), "backtest metric portfolios mismatch"
    assert set(curve_handoff["portfolios"]) == set(by_portfolio), "curve handoff portfolios mismatch"

    for portfolio, rows in curve_by_portfolio.items():
        assert rows == sorted(rows, key=lambda row: row["trading_date"]), f"{portfolio} curve dates must be sorted"
        assert abs(float(rows[0]["portfolio_equity"]) - 1.0) < 1e-12, f"{portfolio} portfolio curve must start at 1"
        assert abs(float(rows[0]["benchmark_equity"]) - 1.0) < 1e-12, f"{portfolio} benchmark curve must start at 1"
        metric_row = metric_by_portfolio[portfolio]
        final_portfolio_return = float(rows[-1]["portfolio_equity"]) - 1.0
        final_benchmark_return = float(rows[-1]["benchmark_equity"]) - 1.0
        assert abs(final_portfolio_return - float(metric_row["portfolio_total_return"])) < 1e-9, f"{portfolio} curve final portfolio return mismatch"
        assert abs(final_benchmark_return - float(metric_row["benchmark_total_return"])) < 1e-9, f"{portfolio} curve final benchmark return mismatch"
        assert int(metric_row["rows"]) == len(rows), f"{portfolio} metric rows mismatch"
        assert abs(float(metric_row["portfolio_end_equity"]) - float(rows[-1]["portfolio_equity"])) < 1e-12, f"{portfolio} metric portfolio end mismatch"
        assert abs(float(metric_row["benchmark_end_equity"]) - float(rows[-1]["benchmark_equity"])) < 1e-12, f"{portfolio} metric benchmark end mismatch"
        handoff = curve_handoff["portfolios"][portfolio]
        assert len(handoff["paper_equity_curve"]) == len(rows), f"{portfolio} handoff paper curve row count mismatch"
        assert len(handoff["benchmark_curve"]) >= len(rows), f"{portfolio} handoff benchmark curve should cover at least the paper window"
        assert abs(float(handoff["benchmark_curve"][0]["equity"]) - 1.0) < 1e-12, f"{portfolio} benchmark curve must start normalized at 1"
        assert all(point["segment"] == "paper" for point in handoff["paper_equity_curve"]), f"{portfolio} handoff paper segment must be paper"
        assert [point["date"] for point in handoff["paper_equity_curve"]] == [row["trading_date"] for row in rows], f"{portfolio} handoff paper dates mismatch"

    for portfolio, rows in backtest_by_portfolio.items():
        assert rows == sorted(rows, key=lambda row: row["trading_date"]), f"{portfolio} backtest dates must be sorted"
        assert abs(float(rows[0]["backtest_equity"]) - 1.0) < 1e-12, f"{portfolio} backtest curve must start at 1"
        expected_curve = expected_pnl_compounded_portfolio_curve(backtest_component_by_portfolio[portfolio])
        assert [row["trading_date"] for row in rows] == list(expected_curve), f"{portfolio} backtest dates must match pnl-compounded trade logs"
        for row in rows:
            expected_equity = expected_curve[row["trading_date"]]
            assert abs(float(row["backtest_equity"]) - expected_equity) < 1e-12, f"{portfolio} backtest equity must be pnl-compounded"
        metric_row = backtest_metric_by_portfolio[portfolio]
        component_rows = backtest_component_by_portfolio[portfolio]
        covered = [row for row in component_rows if row["status"] == "covered"]
        missing = [row for row in component_rows if row["status"] != "covered"]
        assert not missing, f"{portfolio} must have covered backtest components: {missing}"
        assert int(metric_row["rows"]) == len(rows), f"{portfolio} backtest metric rows mismatch"
        assert int(metric_row["covered_component_count"]) == len(covered), f"{portfolio} backtest coverage mismatch"
        assert int(metric_row["missing_component_count"]) == len(missing), f"{portfolio} backtest missing count mismatch"
        assert abs(float(metric_row["backtest_end_equity"]) - float(rows[-1]["backtest_equity"])) < 1e-12, f"{portfolio} backtest metric end mismatch"
        handoff = curve_handoff["portfolios"][portfolio]
        assert len(handoff["backtest_equity_curve"]) == len(rows), f"{portfolio} handoff backtest row count mismatch"
        assert all(point["segment"] == "backtest" for point in handoff["backtest_equity_curve"]), f"{portfolio} handoff backtest segment mismatch"
        assert len(handoff["equity_curve"]) == len(handoff["backtest_equity_curve"]) + len(handoff["paper_equity_curve"]), f"{portfolio} combined equity curve row count mismatch"
        assert handoff["benchmark_curve"][0]["date"] <= handoff["backtest_equity_curve"][0]["date"], f"{portfolio} benchmark curve should extend to the backtest start when available"
        assert audit["backtest_handoff"]["stock" if portfolio.startswith("stock") else "crypto"]["rows"] == len(rows), f"{portfolio} audit backtest rows mismatch"

    print({
        "stock_components": len(stock),
        "crypto_components": len(crypto),
        "curve_rows": len(curve_rows),
        "backtest_curve_rows": len(backtest_rows),
        "backtest_covered_components": sum(1 for row in backtest_components if row["status"] == "covered"),
        "backtest_missing_components": sum(1 for row in backtest_components if row["status"] != "covered"),
        "stock_preferred_components": stock_preferred,
        "stock_evidence_components": stock_evidence,
        "stock_full_universe_fill_components": stock_full_universe_fill,
        "stock_outside_components": stock_outside,
        "crypto_preferred_components": crypto_preferred,
        "crypto_evidence_components": crypto_evidence,
        "crypto_full_universe_fill_components": crypto_full_universe_fill,
        "crypto_outside_components": crypto_outside,
        "stock_excess_pct": float(by_portfolio["stock_equal_weight_100"]["excess_return_pct"]),
        "crypto_excess_pct": float(by_portfolio["crypto_equal_weight_10"]["excess_return_pct"]),
    })


if __name__ == "__main__":
    main()
