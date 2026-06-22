from __future__ import annotations

import csv
import html
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import psycopg
import requests
import yaml
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "06_portfolio_selection"
DEFAULT_PREFERRED_CSV = ROOT / "references" / "preferred_selection.csv"
PREFERRED_CSV = Path(os.environ.get("PREFERRED_SELECTION_CSV") or os.environ.get("SELECTION_CSV") or DEFAULT_PREFERRED_CSV)
USER_ID = 318274928728084480
DEFAULT_START_DATE = "2026-03-20"
DEFAULT_END_DATE = "2026-06-16"
STOCK_BENCHMARK = "QQQ"
CRYPTO_BENCHMARK = "BTCUSD"
STOCK_PORTFOLIO_SIZE = 100
CRYPTO_PORTFOLIO_SIZE = 10
TEMPORARY_EXCLUDED_SYMBOLS = {"PYPL", "TMUS", "DDOG"}
BACKTEST_COMPOUNDED_BY_STRATEGY: dict[str, dict[str, float]] | None = None
WRITE_GENERATED_READMES = os.environ.get("WRITE_GENERATED_READMES") == "1"


def load_repo_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_paper_buy_hold_edges() -> dict[str, dict]:
    path = ROOT / "05_comparisons" / "paper_minus_benchmarks_sorted.csv"
    if not path.exists():
        return {}
    edges: dict[str, dict] = {}
    for row in read_csv_rows(path):
        symbol = str(row.get("target_asset") or "").strip().upper()
        paper_return = row.get("paper_return")
        if not symbol or paper_return in {None, ""}:
            continue
        if symbol not in edges or float(paper_return) > float(edges[symbol]["paper_return"]):
            edges[symbol] = row
    return edges


def update_manifest(audit: dict, component_rows: list[dict], stock_rows: list[dict], crypto_rows: list[dict]) -> None:
    manifest_path = ROOT / "manifest.json"
    if not manifest_path.exists():
        return

    manifest = read_json(manifest_path)
    counts = dict(manifest.get("counts", {}))
    counts.update({
        "portfolioComponentReturnRows": len(component_rows),
        "portfolioCryptoComponents": len(crypto_rows),
        "portfolioCryptoEvidenceComponents": audit["selection_metadata"]["crypto"]["selected_evidence_count"],
        "portfolioCryptoFullUniverseFillComponents": audit["selection_metadata"]["crypto"]["selected_full_universe_fill_count"],
        "portfolioCryptoOutsideComponents": audit["selection_metadata"]["crypto"]["selected_outside_count"],
        "portfolioCryptoPreferredComponents": audit["selection_metadata"]["crypto"]["selected_preferred_count"],
        "portfolioCurveRows": sum(item["rows"] for item in audit["curve_handoff"].values()),
        "portfolioBacktestCurveRows": sum(item["rows"] for item in audit["backtest_handoff"].values()),
        "portfolioBacktestCoveredComponents": sum(item["covered_component_count"] for item in audit["backtest_handoff"].values()),
        "portfolioStockComponents": len(stock_rows),
        "portfolioStockEvidenceComponents": audit["selection_metadata"]["stock"]["selected_evidence_count"],
        "portfolioStockFullUniverseFillComponents": audit["selection_metadata"]["stock"]["selected_full_universe_fill_count"],
        "portfolioStockOutsideComponents": audit["selection_metadata"]["stock"]["selected_outside_count"],
        "portfolioStockPreferredComponents": audit["selection_metadata"]["stock"]["selected_preferred_count"],
        "portfolioSummaryRows": len(audit["summary"]),
    })
    manifest["counts"] = {key: counts[key] for key in sorted(counts)}

    now = audit["generated_at"]
    manifest["generatedAt"] = now
    manifest["refreshedAt"] = now
    manifest["organizedAt"] = datetime.now(timezone.utc).date().isoformat()

    files = manifest.setdefault("files", [])
    for file_name in [
        "UPDATE_WORKFLOW.md",
        "06_portfolio_selection/README.md",
        "06_portfolio_selection/stock_equal_weight_portfolio.csv",
        "06_portfolio_selection/crypto_equal_weight_portfolio.csv",
        "06_portfolio_selection/portfolio_component_returns.csv",
        "06_portfolio_selection/portfolio_backtest_components.csv",
        "06_portfolio_selection/portfolio_backtest_curves.csv",
        "06_portfolio_selection/portfolio_backtest_metrics.csv",
        "06_portfolio_selection/portfolio_equity_curves.csv",
        "06_portfolio_selection/portfolio_equity_curves.json",
        "06_portfolio_selection/portfolio_curve_metrics.csv",
        "06_portfolio_selection/portfolio_vs_benchmark_summary.csv",
        "06_portfolio_selection/selection_audit.json",
        "06_portfolio_selection/summary.html",
    ]:
        if file_name not in files:
            files.append(file_name)

    refreshed = manifest.setdefault("refreshedFolders", [])
    if "06_portfolio_selection" not in refreshed:
        refreshed.append("06_portfolio_selection")

    layout = manifest.setdefault("layout", [])
    layout_entry = next((entry for entry in layout if entry.get("directory") == "06_portfolio_selection"), None)
    if layout_entry is None:
        layout.append({
            "dependsOn": ["01_strategy_universe", "03_market_data", "07_backtest_trade_logs"],
            "description": "Equal-weight stock and crypto portfolio construction ranked by paper return minus buy-and-hold return edge with backtest trade-log handoff.",
            "directory": "06_portfolio_selection",
        })
    else:
        layout_entry["dependsOn"] = ["01_strategy_universe", "03_market_data", "07_backtest_trade_logs"]
        layout_entry["description"] = "Equal-weight stock and crypto portfolio construction ranked by paper return minus buy-and-hold return edge with backtest trade-log handoff."

    source_note = "06 portfolio selection refreshed from prod full ticker universe with preferred reference pool and 07 backtest handoff"
    source = manifest.get("source", "").replace("; 06 portfolio selection refreshed from prod full ticker universe with preferred reference pool", "")
    source = source.replace("; 07 backtest trade logs refreshed from CHFS SIT using ignored local config", "")
    if source_note not in source:
        manifest["source"] = f"{source}; {source_note}" if source else source_note

    write_json(manifest_path, manifest)


def load_preferred_candidates() -> dict[str, dict]:
    if not PREFERRED_CSV.exists():
        return {}
    rows = read_csv_rows(PREFERRED_CSV)
    candidates = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        asset_type_raw = (row.get("asset_type") or "").strip().lower()
        if not symbol:
            continue
        asset_type = "equity" if asset_type_raw == "stock" else "crypto" if asset_type_raw == "crypto" else asset_type_raw
        candidates[symbol] = {
            "symbol": symbol,
            "asset_type": asset_type,
            "selection_order": row.get("selection_order", ""),
            "within_asset_order": row.get("within_asset_order", ""),
            "selection_source": row.get("selection_source", str(PREFERRED_CSV)),
        }
    return candidates


def infer_window() -> tuple[str, str]:
    summary_path = ROOT / "03_market_data" / "portfolio_benchmark_summary.csv"
    if summary_path.exists():
        rows = read_csv_rows(summary_path)
        if rows:
            starts = [row["requested_start_date"] for row in rows if row.get("requested_start_date")]
            ends = [row["requested_end_date"] for row in rows if row.get("requested_end_date")]
            if starts and ends:
                return min(starts), max(ends)
    return os.environ.get("PORTFOLIO_START_DATE", DEFAULT_START_DATE), os.environ.get("PORTFOLIO_END_DATE", DEFAULT_END_DATE)


def get_candidates() -> list[dict]:
    config_path = Path(os.environ.get("ROUTER_CONFIG", ROOT / "config" / "router.prod.local.yaml"))
    pg = yaml.safe_load(config_path.read_text(encoding="utf-8"))["postgres"]
    conn = psycopg.connect(
        dbname=pg["dbname"],
        host=pg["host"],
        port=pg["port"],
        user=pg["username"],
        password=pg["password"],
        connect_timeout=20,
        row_factory=dict_row,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            with ranked as (
              select
                s.target_asset as symbol,
                s.asset_type,
                s.strategy_id,
                s.session_id,
                s.display_name,
                b.score as backtest_score,
                b.sharpe as backtest_sharpe,
                b.total_return as backtest_total_return,
                count(*) over (partition by s.target_asset, s.asset_type) as strategy_count,
                max(b.score) over (partition by s.target_asset, s.asset_type) as max_backtest_score,
                max(b.sharpe) over (partition by s.target_asset, s.asset_type) as max_backtest_sharpe,
                max(b.total_return) over (partition by s.target_asset, s.asset_type) as max_backtest_total_return,
                row_number() over (
                  partition by s.target_asset, s.asset_type
                  order by b.sharpe desc nulls last, b.total_return desc nulls last, s.strategy_id
                ) as rn
              from public.skill_dashboard_strategy s
              left join public.skill_dashboard_strategy_backtest b on b.strategy_id = s.strategy_id
              where s.owner_user_id = %s
                and s.status = 'ready'
                and s.is_active = true
                and s.target_asset is not null
                and s.target_asset <> ''
            )
            select
              symbol,
              asset_type,
              strategy_id,
              session_id,
              display_name,
              backtest_score,
              backtest_sharpe,
              backtest_total_return,
              strategy_count,
              max_backtest_score,
              max_backtest_sharpe,
              max_backtest_total_return
            from ranked
            where rn = 1
            order by asset_type, symbol
            """,
            (USER_ID,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    for row in rows:
        row["symbol"] = str(row["symbol"]).strip().upper()
        row["asset_type"] = str(row.get("asset_type") or "").strip().lower()
    return rows


def chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fetch_cap_bars(symbols: list[str], limit: int = 100, start_date: str | None = None, end_date: str | None = None) -> tuple[list[dict], list[dict]]:
    api_key = os.environ.get("CAP_API_KEY")
    if not api_key:
        raise SystemExit("CAP_API_KEY is required in .env or environment")
    base_url = os.environ.get("ABEL_CAP_BASE_URL") or os.environ.get("CAP_BASE_URL") or "https://cap-sit.abel.ai/api"
    url = urljoin(base_url.rstrip("/") + "/", "market/day_bar")
    session = requests.Session()
    all_rows: list[dict] = []
    warnings: list[dict] = []
    for batch in chunks(symbols, 100):
        payload = {
            "symbols": batch,
            "timeframe": "1d",
            "limit": limit,
            "fields": ["open", "high", "low", "close", "volume"],
        }
        if start_date:
            payload["start_date"] = start_date
        if end_date:
            payload["end_date"] = end_date
        try:
            response = session.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        except requests.RequestException as exc:
            warnings.append({"symbols": batch, "error": str(exc)})
            continue
        if response.status_code >= 400:
            warnings.append({"symbols": batch, "status_code": response.status_code, "body_preview": response.text[:300]})
            continue
        try:
            payload_json = response.json()
        except ValueError:
            warnings.append({"symbols": batch, "status_code": response.status_code, "body_preview": response.text[:300]})
            continue
        rows = payload_json.get("data", []) if isinstance(payload_json, dict) else []
        if not isinstance(rows, list):
            warnings.append({"symbols": batch, "reason": "response data is not a list"})
            continue
        for row in rows:
            symbol = row.get("symbol") or row.get("ticker")
            timestamp = row.get("timestamp") or row.get("trading_date") or row.get("date")
            close = row.get("close")
            if not symbol or timestamp is None or close is None:
                continue
            trading_date = str(timestamp)[:10]
            all_rows.append({
                "symbol": str(symbol).upper(),
                "trading_date": trading_date,
                "timestamp": row.get("timestamp") or f"{trading_date}T00:00:00Z",
                "open": row.get("open", close),
                "high": row.get("high", close),
                "low": row.get("low", close),
                "close": close,
                "volume": row.get("volume"),
            })
    all_rows.sort(key=lambda row: (row["symbol"], row["trading_date"]))
    return all_rows, warnings


def extend_benchmark_curve_to_handoff(handoff: dict, benchmark_symbol: str, benchmark_bars_by_date: dict[str, dict]) -> dict:
    combined = dict(handoff)
    curve_dates = [point["date"] for point in combined["equity_curve"]]
    benchmark_dates = [date for date in curve_dates if date in benchmark_bars_by_date]
    if not benchmark_dates:
        return combined
    start_close = float(benchmark_bars_by_date[benchmark_dates[0]]["close"])
    if start_close <= 0:
        return combined
    combined["benchmark_curve"] = [
        {
            "date": trading_date,
            "equity": float(benchmark_bars_by_date[trading_date]["close"]) / start_close,
        }
        for trading_date in benchmark_dates
        if float(benchmark_bars_by_date[trading_date]["close"]) > 0
    ]
    combined["benchmark_label"] = combined.get("benchmark_label") or benchmark_symbol
    combined["benchmark_curve_source"] = {
        "symbol": benchmark_symbol,
        "normalization": "first available benchmark close on combined equity_curve dates",
        "requested_start_date": curve_dates[0],
        "requested_end_date": curve_dates[-1],
        "first_date": combined["benchmark_curve"][0]["date"],
        "last_date": combined["benchmark_curve"][-1]["date"],
        "rows": len(combined["benchmark_curve"]),
    }
    return combined


def returns_by_symbol(bars: list[dict], start_date: str, end_date: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in bars:
        if start_date <= row["trading_date"] <= end_date:
            grouped[row["symbol"]].append(row)
    results = {}
    for symbol, rows in grouped.items():
        rows.sort(key=lambda row: row["trading_date"])
        start_rows = [row for row in rows if row["trading_date"] == start_date]
        end_rows = [row for row in rows if row["trading_date"] == end_date]
        if not start_rows or not end_rows:
            continue
        start_close = float(start_rows[0]["close"])
        end_close = float(end_rows[-1]["close"])
        if start_close <= 0:
            continue
        results[symbol] = {
            "symbol": symbol,
            "first_trading_date": start_rows[0]["trading_date"],
            "last_trading_date": end_rows[-1]["trading_date"],
            "row_count": len(rows),
            "first_close": start_close,
            "last_close": end_close,
            "total_return": end_close / start_close - 1.0,
        }
    return results


def load_paper_subscription_strategy_ids() -> set[str]:
    path = ROOT / "02_paper_actuals" / "paper_subscriptions.json"
    if not path.exists():
        return set()
    return {
        str(row.get("strategy_id"))
        for row in read_json(path)
        if str(row.get("status") or "").lower() == "active" and row.get("strategy_id") is not None
    }


def build_evidence_symbols(strategies: list[dict], trade_log_index: dict[str, dict], paper_strategy_ids: set[str]) -> set[str]:
    trade_log_strategy_ids = {
        strategy_id
        for strategy_id, row in trade_log_index.items()
        if row.get("status") in {"downloaded", "cached"} and row.get("local_path") and (ROOT / row["local_path"]).exists()
    }
    return {
        str(strategy.get("target_asset") or "").strip().upper()
        for strategy in strategies
        if str(strategy.get("strategy_id")) in trade_log_strategy_ids
        and str(strategy.get("strategy_id")) in paper_strategy_ids
        and str(strategy.get("target_asset") or "").strip()
    }


def build_trade_log_symbols(strategies: list[dict], trade_log_index: dict[str, dict]) -> set[str]:
    trade_log_strategy_ids = {
        strategy_id
        for strategy_id, row in trade_log_index.items()
        if row.get("status") in {"downloaded", "cached"} and row.get("local_path") and (ROOT / row["local_path"]).exists()
    }
    strategy_symbols = {
        str(strategy.get("target_asset") or "").strip().upper()
        for strategy in strategies
        if str(strategy.get("strategy_id")) in trade_log_strategy_ids
        and str(strategy.get("target_asset") or "").strip()
    }
    index_symbols = {
        str(row.get("target_asset") or "").strip().upper()
        for strategy_id, row in trade_log_index.items()
        if strategy_id in trade_log_strategy_ids and str(row.get("target_asset") or "").strip()
    }
    return strategy_symbols | index_symbols


def optional_float(value):
    if value in {None, ""}:
        return ""
    return float(value)


def build_component_rows(candidates: list[dict], returns: dict[str, dict], preferred: dict[str, dict], evidence_symbols: set[str], trade_log_symbols: set[str], paper_edges: dict[str, dict], strategy_by_id: dict[str, dict]) -> list[dict]:
    candidate_by_symbol = {row["symbol"]: row for row in candidates}
    rows = []
    for symbol, ret in returns.items():
        candidate = candidate_by_symbol.get(symbol)
        edge_row = paper_edges.get(symbol)
        if not candidate:
            continue
        edge_strategy = strategy_by_id.get(str(edge_row.get("strategy_id") or ""), candidate) if edge_row else candidate
        preferred_row = preferred.get(symbol, {})
        rows.append({
            "symbol": symbol,
            "asset_type": candidate["asset_type"],
            "preferred_candidate": "Y" if symbol in preferred else "N",
            "evidence_candidate": "Y" if symbol in evidence_symbols else "N",
            "trade_log_candidate": "Y" if symbol in trade_log_symbols else "N",
            "strategy_id": edge_row.get("strategy_id", candidate.get("strategy_id", "")) if edge_row else candidate.get("strategy_id", ""),
            "session_id": edge_strategy.get("session_id", candidate.get("session_id", "")),
            "display_name": edge_row.get("display_name", candidate.get("display_name", "")) if edge_row else candidate.get("display_name", ""),
            "backtest_score": edge_strategy.get("backtest_score", candidate.get("backtest_score", "")),
            "backtest_sharpe": edge_strategy.get("backtest_sharpe", candidate.get("backtest_sharpe", "")),
            "backtest_total_return": edge_strategy.get("backtest_total_return", candidate.get("backtest_total_return", "")),
            "paper_return": optional_float(edge_row.get("paper_return") if edge_row else ""),
            "paper_return_pct": optional_float(edge_row.get("paper_return_pct") if edge_row else ""),
            "buy_hold_return": optional_float(edge_row.get("buy_hold_return") if edge_row else ""),
            "buy_hold_return_pct": optional_float(edge_row.get("buy_hold_return_pct") if edge_row else ""),
            "paper_minus_buy_hold_return": optional_float(edge_row.get("paper_minus_buy_hold_return") if edge_row else ""),
            "paper_minus_buy_hold_return_pct": optional_float(edge_row.get("paper_minus_buy_hold_return_pct") if edge_row else ""),
            "selection_score": ret["total_return"],
            "selection_score_pct": ret["total_return"] * 100.0,
            "preferred_selection_order": preferred_row.get("selection_order", ""),
            "preferred_within_asset_order": preferred_row.get("within_asset_order", ""),
            "strategy_count": candidate.get("strategy_count", 0),
            "max_backtest_score": candidate.get("max_backtest_score", ""),
            "max_backtest_sharpe": candidate.get("max_backtest_sharpe", ""),
            "max_backtest_total_return": candidate.get("max_backtest_total_return", ""),
            "first_trading_date": ret["first_trading_date"],
            "last_trading_date": ret["last_trading_date"],
            "row_count": ret["row_count"],
            "first_close": ret["first_close"],
            "last_close": ret["last_close"],
            "total_return": ret["total_return"],
            "total_return_pct": ret["total_return"] * 100.0,
        })
    rows.sort(key=lambda row: (row["asset_type"], -float(row["selection_score"]), row["symbol"]))
    return rows


def portfolio_return(rows: list[dict], size: int) -> float:
    return sum(float(row["total_return"]) for row in rows) / size


def select_portfolio(rows: list[dict], asset_type: str, size: int, excluded_symbols: set[str], benchmark_return: float, optimize_for_return: bool = False) -> tuple[list[dict], dict]:
    eligible = [row for row in rows if row["asset_type"] == asset_type and row["symbol"] not in excluded_symbols and row.get("strategy_id") and row.get("trade_log_candidate") == "Y"]
    eligible.sort(key=lambda row: (-float(row["selection_score"]), row["symbol"]))
    selected = eligible[:size]
    preferred = [row for row in eligible if row["preferred_candidate"] == "Y"]
    outside = [row for row in eligible if row["preferred_candidate"] != "Y"]
    replacements = []

    selected.sort(key=lambda row: (-float(row["selection_score"]), row["symbol"]))
    weight = 1.0 / size
    for idx, row in enumerate(selected, 1):
        row["selection_rank"] = idx
        row["weight"] = weight
        row["weighted_return"] = weight * float(row["total_return"])
        row["weighted_buy_hold_return"] = "" if row.get("buy_hold_return") in {None, ""} else weight * float(row["buy_hold_return"])
        row["weighted_selection_score"] = weight * float(row["selection_score"])
    metadata = {
        "eligible_count": len(eligible),
        "preferred_eligible_count": len(preferred),
        "evidence_eligible_count": sum(1 for row in eligible if row["evidence_candidate"] == "Y"),
        "outside_eligible_count": len(outside),
        "selected_preferred_count": sum(1 for row in selected if row["preferred_candidate"] == "Y"),
        "selected_evidence_count": sum(1 for row in selected if row["evidence_candidate"] == "Y"),
        "selected_outside_count": sum(1 for row in selected if row["preferred_candidate"] != "Y"),
        "selected_full_universe_fill_count": sum(1 for row in selected if row["evidence_candidate"] != "Y"),
        "replacement_count": len(replacements),
        "replacements": replacements,
    }
    return selected, metadata


def summary_row(name: str, rows: list[dict], benchmark_symbol: str, benchmark_return: float, start_date: str, end_date: str) -> dict:
    portfolio_return = sum(float(row["weighted_return"]) for row in rows)
    return {
        "portfolio": name,
        "component_count": len(rows),
        "start_date": start_date,
        "end_date": end_date,
        "portfolio_return": portfolio_return,
        "portfolio_return_pct": portfolio_return * 100.0,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_return": benchmark_return,
        "benchmark_return_pct": benchmark_return * 100.0,
        "excess_return": portfolio_return - benchmark_return,
        "excess_return_pct": (portfolio_return - benchmark_return) * 100.0,
        "beats_benchmark": "Y" if portfolio_return > benchmark_return else "N",
    }


def bars_by_symbol_date(bars: list[dict]) -> dict[str, dict[str, dict]]:
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in bars:
        grouped[row["symbol"]][row["trading_date"]] = row
    return grouped


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def annualized_volatility(daily_returns: list[float], periods_per_year: int) -> float | None:
    if len(daily_returns) < 2:
        return None
    mean = sum(daily_returns) / len(daily_returns)
    variance = sum((value - mean) ** 2 for value in daily_returns) / (len(daily_returns) - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def annualized_sharpe(daily_returns: list[float], periods_per_year: int) -> float | None:
    vol = annualized_volatility(daily_returns, periods_per_year)
    if not vol or vol == 0:
        return None
    mean = sum(daily_returns) / len(daily_returns)
    return mean / (vol / math.sqrt(periods_per_year)) * math.sqrt(periods_per_year)


def annualized_return(total_return: float, rows: int, periods_per_year: int) -> float | None:
    if rows <= 1 or total_return <= -1:
        return None
    return (1.0 + total_return) ** (periods_per_year / (rows - 1)) - 1.0


def build_portfolio_curve(
    portfolio_name: str,
    rows: list[dict],
    benchmark_symbol: str,
    bars_by_date: dict[str, dict[str, dict]],
    start_date: str,
    end_date: str,
    periods_per_year: int,
) -> tuple[dict, list[dict], dict]:
    symbols = [row["symbol"] for row in rows]
    weights = {row["symbol"]: float(row["weight"]) for row in rows}
    symbol_dates = [set(bars_by_date.get(symbol, {})) for symbol in symbols + [benchmark_symbol]]
    common_dates = [date for date in sorted(set.intersection(*symbol_dates)) if start_date <= date <= end_date] if symbol_dates else []
    if not common_dates:
        raise SystemExit(f"No common curve dates for {portfolio_name}")

    start_date = common_dates[0]
    start_closes = {symbol: float(bars_by_date[symbol][start_date]["close"]) for symbol in symbols}
    benchmark_start_close = float(bars_by_date[benchmark_symbol][start_date]["close"])
    if any(value <= 0 for value in start_closes.values()) or benchmark_start_close <= 0:
        raise SystemExit(f"Invalid curve start close for {portfolio_name}")

    curve_rows = []
    equity_curve = []
    benchmark_curve = []
    previous_portfolio = None
    previous_benchmark = None
    portfolio_daily_returns = []
    benchmark_daily_returns = []

    for trading_date in common_dates:
        portfolio_equity = sum(
            weights[symbol] * (float(bars_by_date[symbol][trading_date]["close"]) / start_closes[symbol])
            for symbol in symbols
        )
        benchmark_equity = float(bars_by_date[benchmark_symbol][trading_date]["close"]) / benchmark_start_close
        portfolio_daily_return = 0.0 if previous_portfolio is None else portfolio_equity / previous_portfolio - 1.0
        benchmark_daily_return = 0.0 if previous_benchmark is None else benchmark_equity / previous_benchmark - 1.0
        if previous_portfolio is not None:
            portfolio_daily_returns.append(portfolio_daily_return)
            benchmark_daily_returns.append(benchmark_daily_return)
        previous_portfolio = portfolio_equity
        previous_benchmark = benchmark_equity
        equity_curve.append({"date": trading_date, "equity": portfolio_equity, "segment": "paper"})
        benchmark_curve.append({"date": trading_date, "equity": benchmark_equity})
        curve_rows.append({
            "portfolio": portfolio_name,
            "trading_date": trading_date,
            "portfolio_equity": portfolio_equity,
            "benchmark_symbol": benchmark_symbol,
            "benchmark_equity": benchmark_equity,
            "excess_equity": portfolio_equity - benchmark_equity,
            "portfolio_daily_return": portfolio_daily_return,
            "benchmark_daily_return": benchmark_daily_return,
        })

    portfolio_values = [point["equity"] for point in equity_curve]
    benchmark_values = [point["equity"] for point in benchmark_curve]
    portfolio_total_return = portfolio_values[-1] - 1.0
    benchmark_total_return = benchmark_values[-1] - 1.0
    metrics = {
        "portfolio": portfolio_name,
        "benchmark_symbol": benchmark_symbol,
        "start_date": common_dates[0],
        "end_date": common_dates[-1],
        "rows": len(common_dates),
        "portfolio_start_equity": portfolio_values[0],
        "portfolio_end_equity": portfolio_values[-1],
        "benchmark_start_equity": benchmark_values[0],
        "benchmark_end_equity": benchmark_values[-1],
        "portfolio_total_return": portfolio_total_return,
        "portfolio_total_return_pct": portfolio_total_return * 100.0,
        "benchmark_total_return": benchmark_total_return,
        "benchmark_total_return_pct": benchmark_total_return * 100.0,
        "excess_total_return": portfolio_total_return - benchmark_total_return,
        "excess_total_return_pct": (portfolio_total_return - benchmark_total_return) * 100.0,
        "portfolio_max_drawdown": max_drawdown(portfolio_values),
        "portfolio_max_drawdown_pct": max_drawdown(portfolio_values) * 100.0,
        "benchmark_max_drawdown": max_drawdown(benchmark_values),
        "benchmark_max_drawdown_pct": max_drawdown(benchmark_values) * 100.0,
        "portfolio_annualized_return": annualized_return(portfolio_total_return, len(common_dates), periods_per_year),
        "benchmark_annualized_return": annualized_return(benchmark_total_return, len(common_dates), periods_per_year),
        "portfolio_annualized_volatility": annualized_volatility(portfolio_daily_returns, periods_per_year),
        "benchmark_annualized_volatility": annualized_volatility(benchmark_daily_returns, periods_per_year),
        "portfolio_annualized_sharpe": annualized_sharpe(portfolio_daily_returns, periods_per_year),
        "benchmark_annualized_sharpe": annualized_sharpe(benchmark_daily_returns, periods_per_year),
        "beats_benchmark": "Y" if portfolio_total_return > benchmark_total_return else "N",
    }
    for key in [
        "portfolio_annualized_return",
        "benchmark_annualized_return",
        "portfolio_annualized_volatility",
        "benchmark_annualized_volatility",
    ]:
        metrics[f"{key}_pct"] = None if metrics[key] is None else metrics[key] * 100.0
    handoff = {
        "id": portfolio_name,
        "name": portfolio_name,
        "paper_start": common_dates[0],
        "benchmark_label": benchmark_symbol,
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
        "metrics": metrics,
    }
    return handoff, curve_rows, metrics


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def score_parts(score: str | None) -> tuple[int, int]:
    if not score or "/" not in str(score):
        return 0, 0
    left, right = str(score).split("/", 1)
    try:
        return int(left), int(right)
    except ValueError:
        return 0, 0


def load_trade_log_index() -> dict[str, dict]:
    path = ROOT / "07_backtest_trade_logs" / "backtest_trade_log_index.csv"
    if not path.exists():
        return {}
    return {row["strategy_id"]: row for row in read_csv_rows(path)}


def strategy_sort_key(strategy: dict) -> tuple[float, float, int, str]:
    score_num, _ = score_parts(strategy.get("backtest_score"))
    return (
        as_float(strategy.get("backtest_sharpe"), -1e9),
        as_float(strategy.get("backtest_total_return"), -1e9),
        score_num,
        str(strategy.get("strategy_id") or ""),
    )


def choose_backtest_components(portfolio_name: str, portfolio_rows: list[dict], strategies: list[dict], trade_log_index: dict[str, dict]) -> list[dict]:
    strategies_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for strategy in strategies:
        symbol = str(strategy.get("target_asset") or "").strip().upper()
        if symbol:
            strategies_by_symbol[symbol].append(strategy)

    component_rows = []
    for row in portfolio_rows:
        symbol = row["symbol"]
        if str(row.get("strategy_id") or "") in trade_log_index:
            selected = row
        else:
            candidates = [strategy for strategy in strategies_by_symbol.get(symbol, []) if str(strategy.get("strategy_id") or "") in trade_log_index]
            candidates.sort(key=strategy_sort_key, reverse=True)
            selected = candidates[0] if candidates else None
        index_row = trade_log_index.get(str(selected.get("strategy_id"))) if selected else None
        local_path = index_row.get("local_path", "") if index_row else ""
        path_exists = bool(local_path) and (ROOT / local_path).exists()
        status = "covered" if selected and path_exists else "missing_01_trade_log"
        representative = selected or row
        component_rows.append({
            "portfolio": portfolio_name,
            "symbol": symbol,
            "asset_type": row["asset_type"],
            "portfolio_weight": row["weight"],
            "backtest_weight": "",
            "strategy_id": representative.get("strategy_id", ""),
            "session_id": representative.get("session_id", ""),
            "display_name": representative.get("display_name", ""),
            "backtest_score": representative.get("backtest_score", ""),
            "backtest_sharpe": representative.get("backtest_sharpe", ""),
            "backtest_total_return": representative.get("backtest_total_return", ""),
            "trade_log_path": local_path,
            "status": status,
        })
    covered = [row for row in component_rows if row["status"] == "covered"]
    weight = 1.0 / len(covered) if covered else 0.0
    for row in covered:
        row["backtest_weight"] = weight
    return component_rows


def read_trade_log_curve(local_path: str) -> dict[str, float]:
    compounded = load_backtest_compounded_curves()
    strategy_id = Path(local_path).stem
    if strategy_id in compounded:
        return compounded[strategy_id]

    path = ROOT / local_path
    rows = read_csv_rows(path)
    equity = 1.0
    curve = {}
    for row in rows:
        trading_date = str(row.get("date") or "")[:10]
        if not trading_date:
            continue
        pnl = as_float(row.get("pnl"), 0.0)
        if pnl <= -1.0:
            raise SystemExit(f"Invalid pnl {pnl} in {local_path} on {trading_date}; compounded equity would be non-positive")
        equity *= 1.0 + pnl
        if equity <= 0.0:
            raise SystemExit(f"Non-positive pnl-compounded equity in {local_path} on {trading_date}")
        curve[trading_date] = equity
    return curve


def load_backtest_compounded_curves() -> dict[str, dict[str, float]]:
    global BACKTEST_COMPOUNDED_BY_STRATEGY
    if BACKTEST_COMPOUNDED_BY_STRATEGY is not None:
        return BACKTEST_COMPOUNDED_BY_STRATEGY
    path = ROOT / "07_backtest_trade_logs" / "backtest_compounded_curves.csv"
    curves: dict[str, dict[str, float]] = defaultdict(dict)
    if path.exists():
        for row in read_csv_rows(path):
            strategy_id = str(row.get("strategy_id") or "")
            trading_date = str(row.get("trading_date") or "")[:10]
            if strategy_id and trading_date:
                curves[strategy_id][trading_date] = as_float(row.get("backtest_compounded_equity"), 1.0)
    BACKTEST_COMPOUNDED_BY_STRATEGY = dict(curves)
    return BACKTEST_COMPOUNDED_BY_STRATEGY


def build_backtest_curve(portfolio_name: str, component_rows: list[dict], periods_per_year: int) -> tuple[dict, list[dict], dict]:
    covered = [row for row in component_rows if row["status"] == "covered"]
    if not covered:
        raise SystemExit(f"No covered backtest trade logs for {portfolio_name}")
    curves = {row["symbol"]: read_trade_log_curve(row["trade_log_path"]) for row in covered}
    common_dates = sorted(set.intersection(*(set(curve) for curve in curves.values())))
    if not common_dates:
        raise SystemExit(f"No common backtest dates for {portfolio_name}")

    start_date = common_dates[0]
    start_values = {symbol: curves[symbol][start_date] for symbol in curves}
    weight = 1.0 / len(covered)
    previous = None
    daily_returns = []
    curve_rows = []
    equity_curve = []
    for trading_date in common_dates:
        equity = sum(weight * (curves[symbol][trading_date] / start_values[symbol]) for symbol in curves)
        daily_return = 0.0 if previous is None else equity / previous - 1.0
        if previous is not None:
            daily_returns.append(daily_return)
        previous = equity
        point = {"date": trading_date, "equity": equity, "segment": "backtest"}
        equity_curve.append(point)
        curve_rows.append({
            "portfolio": portfolio_name,
            "trading_date": trading_date,
            "backtest_equity": equity,
            "backtest_daily_return": daily_return,
            "component_count": len(component_rows),
            "covered_component_count": len(covered),
            "missing_component_count": len(component_rows) - len(covered),
        })

    values = [point["equity"] for point in equity_curve]
    total_return = values[-1] - 1.0
    metrics = {
        "portfolio": portfolio_name,
        "start_date": common_dates[0],
        "end_date": common_dates[-1],
        "rows": len(common_dates),
        "component_count": len(component_rows),
        "covered_component_count": len(covered),
        "missing_component_count": len(component_rows) - len(covered),
        "backtest_start_equity": values[0],
        "backtest_end_equity": values[-1],
        "backtest_total_return": total_return,
        "backtest_total_return_pct": total_return * 100.0,
        "backtest_max_drawdown": max_drawdown(values),
        "backtest_max_drawdown_pct": max_drawdown(values) * 100.0,
        "backtest_annualized_return": annualized_return(total_return, len(common_dates), periods_per_year),
        "backtest_annualized_volatility": annualized_volatility(daily_returns, periods_per_year),
        "backtest_annualized_sharpe": annualized_sharpe(daily_returns, periods_per_year),
    }
    for key in ["backtest_annualized_return", "backtest_annualized_volatility"]:
        metrics[f"{key}_pct"] = None if metrics[key] is None else metrics[key] * 100.0
    handoff = {
        "id": portfolio_name,
        "name": portfolio_name,
        "equity_curve": equity_curve,
        "metrics": metrics,
    }
    return handoff, curve_rows, metrics


def attach_backtest_to_handoff(paper_handoff: dict, backtest_handoff: dict) -> dict:
    combined = dict(paper_handoff)
    backtest_curve = list(backtest_handoff["equity_curve"])
    paper_curve = list(paper_handoff["equity_curve"])
    backtest_end = backtest_curve[-1]["equity"] if backtest_curve else 1.0
    scaled_paper_curve = [
        {"date": point["date"], "equity": point["equity"] * backtest_end, "segment": "paper"}
        for point in paper_curve
    ]
    combined["equity_curve"] = backtest_curve + scaled_paper_curve
    combined["backtest_equity_curve"] = backtest_curve
    combined["paper_equity_curve"] = paper_curve
    combined["paper_equity_curve_scaled"] = scaled_paper_curve
    combined["backtest_metrics"] = backtest_handoff["metrics"]
    combined["paper_metrics"] = paper_handoff["metrics"]
    return combined


def metric_value(value) -> str:
    if value is None:
        return ""
    return value


def chart_svg(curve_rows: list[dict], title: str, benchmark_symbol: str) -> str:
    width = 960
    height = 300
    pad_left = 52
    pad_right = 20
    pad_top = 24
    pad_bottom = 38
    values = [float(row["portfolio_equity"]) for row in curve_rows] + [float(row["benchmark_equity"]) for row in curve_rows]
    min_value = min(values)
    max_value = max(values)
    if min_value == max_value:
        min_value -= 0.05
        max_value += 0.05
    value_pad = (max_value - min_value) * 0.08
    min_value -= value_pad
    max_value += value_pad

    def x_at(index: int) -> float:
        if len(curve_rows) == 1:
            return pad_left
        return pad_left + index * (width - pad_left - pad_right) / (len(curve_rows) - 1)

    def y_at(value: float) -> float:
        return pad_top + (max_value - value) * (height - pad_top - pad_bottom) / (max_value - min_value)

    portfolio_points = " ".join(f"{x_at(idx):.2f},{y_at(float(row['portfolio_equity'])):.2f}" for idx, row in enumerate(curve_rows))
    benchmark_points = " ".join(f"{x_at(idx):.2f},{y_at(float(row['benchmark_equity'])):.2f}" for idx, row in enumerate(curve_rows))
    area_points = f"{pad_left},{height - pad_bottom} {portfolio_points} {width - pad_right},{height - pad_bottom}"
    first_date = curve_rows[0]["trading_date"]
    last_date = curve_rows[-1]["trading_date"]
    y_ticks = [min_value + (max_value - min_value) * idx / 4 for idx in range(5)]
    grid = "".join(
        f"<line x1='{pad_left}' x2='{width - pad_right}' y1='{y_at(value):.2f}' y2='{y_at(value):.2f}' stroke='#dde3ee' stroke-dasharray='3 3'/><text x='8' y='{y_at(value) + 4:.2f}'>{value:.2f}x</text>"
        for value in y_ticks
    )
    return f"""
<figure class="chart-card">
  <figcaption><strong>{html.escape(title)}</strong><span>portfolio mint area, {html.escape(benchmark_symbol)} muted dashed line</span></figcaption>
  <svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)} equity curve">
    <rect width="{width}" height="{height}" fill="#ffffff"/>
    <g class="axis-labels">{grid}</g>
    <polyline points="{benchmark_points}" fill="none" stroke="#667085" stroke-width="2" stroke-dasharray="5 5"/>
    <polygon points="{area_points}" fill="#19c7a11f"/>
    <polyline points="{portfolio_points}" fill="none" stroke="#19c7a1" stroke-width="3"/>
    <text x="{pad_left}" y="{height - 10}">{html.escape(first_date)}</text>
    <text x="{width - pad_right - 80}" y="{height - 10}">{html.escape(last_date)}</text>
  </svg>
</figure>
"""


def backtest_chart_svg(curve_rows: list[dict], title: str) -> str:
    width = 960
    height = 300
    pad_left = 52
    pad_right = 20
    pad_top = 24
    pad_bottom = 38
    values = [float(row["backtest_equity"]) for row in curve_rows]
    min_value = min(values)
    max_value = max(values)
    if min_value == max_value:
        min_value -= 0.05
        max_value += 0.05
    value_pad = (max_value - min_value) * 0.08
    min_value -= value_pad
    max_value += value_pad

    def x_at(index: int) -> float:
        if len(curve_rows) == 1:
            return pad_left
        return pad_left + index * (width - pad_left - pad_right) / (len(curve_rows) - 1)

    def y_at(value: float) -> float:
        return pad_top + (max_value - value) * (height - pad_top - pad_bottom) / (max_value - min_value)

    points = " ".join(f"{x_at(idx):.2f},{y_at(float(row['backtest_equity'])):.2f}" for idx, row in enumerate(curve_rows))
    area_points = f"{pad_left},{height - pad_bottom} {points} {width - pad_right},{height - pad_bottom}"
    first_date = curve_rows[0]["trading_date"]
    last_date = curve_rows[-1]["trading_date"]
    y_ticks = [min_value + (max_value - min_value) * idx / 4 for idx in range(5)]
    grid = "".join(
        f"<line x1='{pad_left}' x2='{width - pad_right}' y1='{y_at(value):.2f}' y2='{y_at(value):.2f}' stroke='#dde3ee' stroke-dasharray='3 3'/><text x='8' y='{y_at(value) + 4:.2f}'>{value:.2f}x</text>"
        for value in y_ticks
    )
    return f"""
<figure class="chart-card">
  <figcaption><strong>{html.escape(title)}</strong><span>backtest graphite area from 07 trade logs</span></figcaption>
  <svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)} backtest equity curve">
    <rect width="{width}" height="{height}" fill="#ffffff"/>
    <g class="axis-labels">{grid}</g>
    <polygon points="{area_points}" fill="#1720331f"/>
    <polyline points="{points}" fill="none" stroke="#172033" stroke-width="2.6"/>
    <text x="{pad_left}" y="{height - 10}">{html.escape(first_date)}</text>
    <text x="{width - pad_right - 80}" y="{height - 10}">{html.escape(last_date)}</text>
  </svg>
</figure>
"""


def combined_handoff_chart_svg(handoff: dict, title: str) -> str:
    width = 960
    height = 300
    pad_left = 52
    pad_right = 20
    pad_top = 24
    pad_bottom = 38
    curve = list(handoff["equity_curve"])
    benchmark_by_date = {point["date"]: float(point["equity"]) for point in handoff.get("benchmark_curve", [])}
    values = [float(point["equity"]) for point in curve] + list(benchmark_by_date.values())
    min_value = min(values)
    max_value = max(values)
    if min_value == max_value:
        min_value -= 0.05
        max_value += 0.05
    value_pad = (max_value - min_value) * 0.08
    min_value -= value_pad
    max_value += value_pad

    def x_at(index: int) -> float:
        if len(curve) == 1:
            return pad_left
        return pad_left + index * (width - pad_left - pad_right) / (len(curve) - 1)

    def y_at(value: float) -> float:
        return pad_top + (max_value - value) * (height - pad_top - pad_bottom) / (max_value - min_value)

    def points_for(segment: str) -> str:
        return " ".join(
            f"{x_at(idx):.2f},{y_at(float(point['equity'])):.2f}"
            for idx, point in enumerate(curve)
            if point["segment"] == segment
        )

    backtest_points = points_for("backtest")
    paper_points = points_for("paper")
    benchmark_points = " ".join(
        f"{x_at(idx):.2f},{y_at(benchmark_by_date[point['date']]):.2f}"
        for idx, point in enumerate(curve)
        if point["date"] in benchmark_by_date
    )
    paper_start = handoff.get("paper_start") or (handoff.get("paper_equity_curve") or [{}])[0].get("date", "")
    paper_start_index = next((idx for idx, point in enumerate(curve) if point["segment"] == "paper"), None)
    paper_line = ""
    if paper_start_index is not None:
        x = x_at(paper_start_index)
        paper_line = f"<line x1='{x:.2f}' x2='{x:.2f}' y1='{pad_top}' y2='{height - pad_bottom}' stroke='#19c7a1' stroke-width='1.4' stroke-dasharray='4 4'/><text x='{x + 6:.2f}' y='{pad_top + 14}'>paper start</text>"
    first_date = curve[0]["date"]
    last_date = curve[-1]["date"]
    benchmark_label = handoff.get("benchmark_label") or "benchmark"
    y_ticks = [min_value + (max_value - min_value) * idx / 4 for idx in range(5)]
    grid = "".join(
        f"<line x1='{pad_left}' x2='{width - pad_right}' y1='{y_at(value):.2f}' y2='{y_at(value):.2f}' stroke='#dde3ee' stroke-dasharray='3 3'/><text x='8' y='{y_at(value) + 4:.2f}'>{value:.2f}x</text>"
        for value in y_ticks
    )
    return f"""
<figure class="chart-card">
  <figcaption><strong>{html.escape(title)}</strong><span>graphite backtest + mint paper + {html.escape(benchmark_label)} muted dashed</span></figcaption>
  <svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)} combined backtest and paper equity curve">
    <rect width="{width}" height="{height}" fill="#ffffff"/>
    <g class="axis-labels">{grid}</g>
    <polyline points="{benchmark_points}" fill="none" stroke="#667085" stroke-width="2" stroke-dasharray="5 5"/>
    <polyline points="{backtest_points}" fill="none" stroke="#172033" stroke-width="2.4"/>
    <polyline points="{paper_points}" fill="none" stroke="#19c7a1" stroke-width="3"/>
    {paper_line}
    <text x="{pad_left}" y="{height - 10}">{html.escape(first_date)}</text>
    <text x="{width - pad_right - 80}" y="{height - 10}">{html.escape(last_date)}</text>
  </svg>
</figure>
"""


def fmt_pct(value: float) -> str:
    return f"{value * 100.0:.2f}%"


def html_table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key, "")
            if key.endswith("return") or key.endswith("drawdown") or key.endswith("volatility") or key in {"weight", "excess_return", "portfolio_return", "benchmark_return"}:
                try:
                    value = fmt_pct(float(value))
                except (TypeError, ValueError):
                    pass
            elif key.endswith("_pct"):
                try:
                    value = f"{float(value):.2f}%"
                except (TypeError, ValueError):
                    pass
            cells.append(f"<td>{html.escape(str(value))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return "<div class=\"table-wrap\"><table><thead><tr>" + head + "</tr></thead><tbody>" + "\n".join(body) + "</tbody></table></div>"


def write_html(
    summary_rows: list[dict],
    stock_rows: list[dict],
    crypto_rows: list[dict],
    start_date: str,
    end_date: str,
    stock_curve_rows: list[dict],
    crypto_curve_rows: list[dict],
    curve_metrics: list[dict],
    stock_backtest_rows: list[dict],
    crypto_backtest_rows: list[dict],
    backtest_metrics: list[dict],
    stock_handoff: dict,
    crypto_handoff: dict,
) -> None:
    cards = "".join(
        f"<article><strong>{html.escape(row['portfolio'])}</strong><span>{html.escape(row['beats_benchmark'])} vs {html.escape(row['benchmark_symbol'])}</span><b>{row['excess_return_pct']:.2f}% excess</b></article>"
        for row in summary_rows
    )
    component_columns = [
        ("selection_rank", "Rank"),
        ("symbol", "Ticker"),
        ("preferred_candidate", "Preferred"),
        ("evidence_candidate", "Evidence"),
        ("weight", "Weight"),
        ("total_return", "CAP Return"),
        ("paper_return", "Paper Return"),
        ("buy_hold_return", "Buy-Hold Return"),
        ("selection_score_pct", "Selection Score"),
        ("strategy_count", "Strategies"),
    ]
    summary_columns = [
        ("portfolio", "Portfolio"),
        ("component_count", "Count"),
        ("portfolio_return", "Portfolio"),
        ("benchmark_symbol", "Benchmark"),
        ("benchmark_return", "Benchmark Return"),
        ("excess_return", "Excess"),
        ("beats_benchmark", "Pass"),
    ]
    metric_columns = [
        ("portfolio", "Portfolio"),
        ("rows", "Curve Rows"),
        ("portfolio_total_return", "Portfolio Return"),
        ("benchmark_symbol", "Benchmark"),
        ("benchmark_total_return", "Benchmark Return"),
        ("excess_total_return", "Excess"),
        ("portfolio_max_drawdown", "Portfolio Max DD"),
        ("benchmark_max_drawdown", "Benchmark Max DD"),
        ("portfolio_annualized_volatility", "Portfolio Ann Vol"),
        ("portfolio_annualized_sharpe", "Portfolio Ann Sharpe"),
        ("beats_benchmark", "Pass"),
    ]
    backtest_metric_columns = [
        ("portfolio", "Portfolio"),
        ("rows", "Rows"),
        ("covered_component_count", "Covered"),
        ("missing_component_count", "Missing"),
        ("backtest_total_return", "Backtest Return"),
        ("backtest_max_drawdown", "Backtest Max DD"),
        ("backtest_annualized_volatility", "Backtest Ann Vol"),
        ("backtest_annualized_sharpe", "Backtest Ann Sharpe"),
    ]
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>06 Portfolio Selection</title>
  <style>
    body {{ margin: 0; background: #f5f7fb; color: #172033; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
    main {{ max-width: 1320px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin: 24px 0 12px; }}
    .muted {{ color: #667085; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin: 18px 0; }}
    .charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 14px; margin: 16px 0; }}
    article, section {{ background: #fff; border: 1px solid #dde3ee; border-radius: 14px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(16,24,40,.04); }}
    article strong, article span, article b {{ display: block; }}
    article span {{ color: #667085; margin-top: 4px; }}
    article b {{ margin-top: 6px; font-size: 20px; color: #087443; }}
    .table-wrap {{ overflow: auto; border: 1px solid #dde3ee; border-radius: 10px; }}
    table {{ border-collapse: separate; border-spacing: 0; width: 100%; white-space: nowrap; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #dde3ee; text-align: left; }}
    th {{ position: sticky; top: 0; background: #eef3ff; font-size: 12px; text-transform: uppercase; letter-spacing: .035em; }}
    tbody tr:hover {{ background: #f8fbff; }}
    .chart-card {{ margin: 0; background: #fff; border: 1px solid #dde3ee; border-radius: 14px; padding: 14px; }}
    .chart-card figcaption {{ display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; color: #172033; }}
    .chart-card figcaption span {{ color: #667085; font-size: 12px; }}
    svg {{ width: 100%; height: auto; display: block; }}
    svg text {{ fill: #667085; font-size: 12px; font-family: inherit; }}
  </style>
</head>
<body><main>
  <h1>06 Portfolio Selection</h1>
  <p class="muted">Selection window: {html.escape(start_date)} to {html.escape(end_date)}. Components are equal-weight. The workflow selects from CAP-available tickers with router strategy ids and covered trade logs by highest CAP paper-window total return, maximizing portfolio-vs-benchmark edge for each sleeve. PYPL, TMUS, and DDOG are excluded for now.</p>
  <div class="cards">{cards}</div>
  <section><h2>Combined Backtest + Paper Handoff</h2><p class="muted">These static SVGs mirror the website handoff contract: `equity_curve` is drawn as graphite backtest followed by scaled mint paper, with `benchmark_curve` normalized from the first available CAP benchmark close on combined curve dates. Benchmark history is requested back to the earliest portfolio backtest start and drawn as a muted dashed line wherever available.</p><div class="charts">{combined_handoff_chart_svg(stock_handoff, '100 Stock Equal-Weight Combined Handoff')}{combined_handoff_chart_svg(crypto_handoff, '10 Crypto Equal-Weight Combined Handoff')}</div></section>
  <section><h2>Backtest Metrics</h2>{html_table(backtest_metrics, backtest_metric_columns)}</section>
  <section><h2>Paper-Window Portfolio vs Benchmark</h2><p class="muted">Paper-window curves use normalized CAP closes. Portfolio value starts at 1.0 and equals the weighted sum of each selected component's normalized close. This mirrors the website's portfolio/benchmark curve handoff shape.</p><div class="charts">{chart_svg(stock_curve_rows, '100 Stock Equal-Weight vs QQQ', 'QQQ')}{chart_svg(crypto_curve_rows, '10 Crypto Equal-Weight vs BTCUSD', 'BTCUSD')}</div></section>
  <section><h2>Paper-Window Curve Metrics</h2>{html_table(curve_metrics, metric_columns)}</section>
  <section><h2>Portfolio vs Benchmark</h2>{html_table(summary_rows, summary_columns)}</section>
  <section><h2>100 Stock Equal-Weight Portfolio</h2>{html_table(stock_rows, component_columns)}</section>
  <section><h2>10 Crypto Equal-Weight Portfolio</h2>{html_table(crypto_rows, component_columns)}</section>
</main></body></html>
"""
    (OUT / "summary.html").write_text(page, encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    load_repo_env()
    start_date, end_date = infer_window()
    preferred_candidates = load_preferred_candidates()
    paper_edges = load_paper_buy_hold_edges()
    strategies = read_json(ROOT / "01_strategy_universe" / "strategies.json")
    strategy_by_id = {str(row.get("strategy_id") or ""): row for row in strategies}
    trade_log_index = load_trade_log_index()
    paper_strategy_ids = load_paper_subscription_strategy_ids()
    evidence_symbols = build_evidence_symbols(strategies, trade_log_index, paper_strategy_ids)
    trade_log_symbols = build_trade_log_symbols(strategies, trade_log_index)
    candidates = get_candidates()
    stock_candidates = [row for row in candidates if row["asset_type"] == "equity"]
    crypto_candidates = [row for row in candidates if row["asset_type"] == "crypto"]
    symbols = sorted({row["symbol"] for row in stock_candidates + crypto_candidates} | {STOCK_BENCHMARK, CRYPTO_BENCHMARK})
    bars, warnings = fetch_cap_bars(symbols)
    returns = returns_by_symbol(bars, start_date, end_date)
    component_rows = build_component_rows(candidates, returns, preferred_candidates, evidence_symbols, trade_log_symbols, paper_edges, strategy_by_id)

    if STOCK_BENCHMARK not in returns:
        raise SystemExit(f"Missing {STOCK_BENCHMARK} benchmark return")
    if CRYPTO_BENCHMARK not in returns:
        raise SystemExit(f"Missing {CRYPTO_BENCHMARK} benchmark return")

    stock_rows, stock_selection_meta = select_portfolio(component_rows, "equity", STOCK_PORTFOLIO_SIZE, {STOCK_BENCHMARK} | TEMPORARY_EXCLUDED_SYMBOLS, returns[STOCK_BENCHMARK]["total_return"])
    crypto_rows, crypto_selection_meta = select_portfolio(component_rows, "crypto", CRYPTO_PORTFOLIO_SIZE, {CRYPTO_BENCHMARK} | TEMPORARY_EXCLUDED_SYMBOLS, returns[CRYPTO_BENCHMARK]["total_return"], optimize_for_return=True)
    if len(stock_rows) != STOCK_PORTFOLIO_SIZE:
        raise SystemExit(f"Only {len(stock_rows)} stock candidates have complete price coverage")
    if len(crypto_rows) != CRYPTO_PORTFOLIO_SIZE:
        raise SystemExit(f"Only {len(crypto_rows)} crypto candidates have complete price coverage after excluding BTCUSD")

    summary_rows = [
        summary_row("stock_equal_weight_100", stock_rows, STOCK_BENCHMARK, returns[STOCK_BENCHMARK]["total_return"], start_date, end_date),
        summary_row("crypto_equal_weight_10", crypto_rows, CRYPTO_BENCHMARK, returns[CRYPTO_BENCHMARK]["total_return"], start_date, end_date),
    ]
    bar_lookup = bars_by_symbol_date(bars)
    stock_curve, stock_curve_rows, stock_curve_metrics = build_portfolio_curve("stock_equal_weight_100", stock_rows, STOCK_BENCHMARK, bar_lookup, start_date, end_date, 252)
    crypto_curve, crypto_curve_rows, crypto_curve_metrics = build_portfolio_curve("crypto_equal_weight_10", crypto_rows, CRYPTO_BENCHMARK, bar_lookup, start_date, end_date, 365)
    stock_backtest_components = choose_backtest_components("stock_equal_weight_100", stock_rows, strategies, trade_log_index)
    crypto_backtest_components = choose_backtest_components("crypto_equal_weight_10", crypto_rows, strategies, trade_log_index)
    stock_backtest, stock_backtest_rows, stock_backtest_metrics = build_backtest_curve("stock_equal_weight_100", stock_backtest_components, 252)
    crypto_backtest, crypto_backtest_rows, crypto_backtest_metrics = build_backtest_curve("crypto_equal_weight_10", crypto_backtest_components, 365)
    stock_curve = attach_backtest_to_handoff(stock_curve, stock_backtest)
    crypto_curve = attach_backtest_to_handoff(crypto_curve, crypto_backtest)
    benchmark_start_date = min(stock_curve["equity_curve"][0]["date"], crypto_curve["equity_curve"][0]["date"])
    benchmark_end_date = max(stock_curve["equity_curve"][-1]["date"], crypto_curve["equity_curve"][-1]["date"])
    extended_benchmark_bars, extended_benchmark_warnings = fetch_cap_bars(
        [STOCK_BENCHMARK, CRYPTO_BENCHMARK],
        limit=5000,
        start_date=benchmark_start_date,
        end_date=benchmark_end_date,
    )
    extended_benchmark_lookup = bars_by_symbol_date(extended_benchmark_bars)
    stock_curve = extend_benchmark_curve_to_handoff(stock_curve, STOCK_BENCHMARK, extended_benchmark_lookup.get(STOCK_BENCHMARK, {}))
    crypto_curve = extend_benchmark_curve_to_handoff(crypto_curve, CRYPTO_BENCHMARK, extended_benchmark_lookup.get(CRYPTO_BENCHMARK, {}))
    curve_handoff = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "07 backtest trade-log pnl compounded curves plus CAP market/day_bar normalized closes",
        "chart_shape": "Matches website StrategyPortfolio equity_curve and benchmark_curve inputs. equity_curve includes a pnl-compounded backtest segment and scaled paper segment; benchmark_curve is extended as far back as CAP benchmark bars are available on combined curve dates; paper_equity_curve keeps the paper window normalized to 1.0.",
        "chart_contract": {
            "component": "services/abel-official-website/website/src/components/strategies/EquityCurveChart.tsx",
            "library": "recharts AreaChart",
            "curve_prop": "equity_curve",
            "benchmark_prop": "benchmark_curve",
            "benchmark_label_prop": "benchmark_label",
            "paper_start_prop": "paper_start",
            "backtest_style": "graphite area where point.segment == 'backtest'",
            "paper_style": "mint area where point.segment == 'paper'",
            "benchmark_style": "muted dashed line",
            "primary_pages": ["/abel-portfolio", "/abel-portfolio/crypto", "/strategies/[id] portfolio detail"],
        },
        "benchmark_fetch": {
            "requested_symbols": [STOCK_BENCHMARK, CRYPTO_BENCHMARK],
            "requested_start_date": benchmark_start_date,
            "requested_end_date": benchmark_end_date,
            "rows": len(extended_benchmark_bars),
            "warnings": extended_benchmark_warnings,
        },
        "portfolios": {
            "stock_equal_weight_100": stock_curve,
            "crypto_equal_weight_10": crypto_curve,
        },
    }
    curve_rows = stock_curve_rows + crypto_curve_rows
    curve_metrics = [stock_curve_metrics, crypto_curve_metrics]
    backtest_component_rows = stock_backtest_components + crypto_backtest_components
    backtest_curve_rows = stock_backtest_rows + crypto_backtest_rows
    backtest_metrics = [stock_backtest_metrics, crypto_backtest_metrics]

    portfolio_fields = [
        "selection_rank", "symbol", "asset_type", "weight", "total_return", "total_return_pct", "weighted_return",
        "paper_return", "paper_return_pct", "buy_hold_return", "buy_hold_return_pct", "paper_minus_buy_hold_return", "paper_minus_buy_hold_return_pct", "selection_score", "selection_score_pct", "weighted_buy_hold_return", "weighted_selection_score",
        "preferred_candidate", "evidence_candidate", "trade_log_candidate", "strategy_id", "session_id", "display_name", "backtest_score", "backtest_sharpe", "backtest_total_return",
        "first_trading_date", "last_trading_date", "row_count", "first_close", "last_close", "strategy_count", "max_backtest_score", "max_backtest_sharpe", "max_backtest_total_return",
    ]
    component_fields = [
        "symbol", "asset_type", "preferred_candidate", "evidence_candidate", "trade_log_candidate", "strategy_id", "session_id", "display_name", "backtest_score", "backtest_sharpe", "backtest_total_return",
        "paper_return", "paper_return_pct", "buy_hold_return", "buy_hold_return_pct", "paper_minus_buy_hold_return", "paper_minus_buy_hold_return_pct", "selection_score", "selection_score_pct",
        "preferred_selection_order", "preferred_within_asset_order", "strategy_count", "max_backtest_score", "max_backtest_sharpe", "max_backtest_total_return",
        "first_trading_date", "last_trading_date", "row_count", "first_close", "last_close", "total_return", "total_return_pct",
    ]
    summary_fields = [
        "portfolio", "component_count", "start_date", "end_date", "portfolio_return", "portfolio_return_pct",
        "benchmark_symbol", "benchmark_return", "benchmark_return_pct", "excess_return", "excess_return_pct", "beats_benchmark",
    ]
    curve_fields = [
        "portfolio", "trading_date", "portfolio_equity", "benchmark_symbol", "benchmark_equity", "excess_equity",
        "portfolio_daily_return", "benchmark_daily_return",
    ]
    curve_metric_fields = [
        "portfolio", "benchmark_symbol", "start_date", "end_date", "rows",
        "portfolio_start_equity", "portfolio_end_equity", "benchmark_start_equity", "benchmark_end_equity",
        "portfolio_total_return", "portfolio_total_return_pct", "benchmark_total_return", "benchmark_total_return_pct",
        "excess_total_return", "excess_total_return_pct", "portfolio_max_drawdown", "portfolio_max_drawdown_pct",
        "benchmark_max_drawdown", "benchmark_max_drawdown_pct", "portfolio_annualized_return", "portfolio_annualized_return_pct",
        "benchmark_annualized_return", "benchmark_annualized_return_pct", "portfolio_annualized_volatility",
        "portfolio_annualized_volatility_pct", "benchmark_annualized_volatility", "benchmark_annualized_volatility_pct",
        "portfolio_annualized_sharpe", "benchmark_annualized_sharpe", "beats_benchmark",
    ]
    backtest_component_fields = [
        "portfolio", "symbol", "asset_type", "portfolio_weight", "backtest_weight", "strategy_id", "session_id",
        "display_name", "backtest_score", "backtest_sharpe", "backtest_total_return", "trade_log_path", "status",
    ]
    backtest_curve_fields = [
        "portfolio", "trading_date", "backtest_equity", "backtest_daily_return", "component_count",
        "covered_component_count", "missing_component_count",
    ]
    backtest_metric_fields = [
        "portfolio", "start_date", "end_date", "rows", "component_count", "covered_component_count", "missing_component_count",
        "backtest_start_equity", "backtest_end_equity", "backtest_total_return", "backtest_total_return_pct",
        "backtest_max_drawdown", "backtest_max_drawdown_pct", "backtest_annualized_return", "backtest_annualized_return_pct",
        "backtest_annualized_volatility", "backtest_annualized_volatility_pct", "backtest_annualized_sharpe",
    ]

    write_csv(OUT / "stock_equal_weight_portfolio.csv", portfolio_fields, stock_rows)
    write_csv(OUT / "crypto_equal_weight_portfolio.csv", portfolio_fields, crypto_rows)
    write_csv(OUT / "portfolio_component_returns.csv", component_fields, component_rows)
    write_csv(OUT / "portfolio_backtest_components.csv", backtest_component_fields, backtest_component_rows)
    write_csv(OUT / "portfolio_backtest_curves.csv", backtest_curve_fields, backtest_curve_rows)
    write_csv(OUT / "portfolio_backtest_metrics.csv", backtest_metric_fields, backtest_metrics)
    write_csv(OUT / "portfolio_equity_curves.csv", curve_fields, curve_rows)
    write_csv(OUT / "portfolio_curve_metrics.csv", curve_metric_fields, curve_metrics)
    write_json(OUT / "portfolio_equity_curves.json", curve_handoff)
    write_csv(OUT / "portfolio_vs_benchmark_summary.csv", summary_fields, summary_rows)
    write_html(summary_rows, stock_rows, crypto_rows, start_date, end_date, stock_curve_rows, crypto_curve_rows, curve_metrics, stock_backtest_rows, crypto_backtest_rows, backtest_metrics, stock_curve, crypto_curve)

    valid_counts = Counter(row["asset_type"] for row in component_rows)
    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": str(USER_ID),
        "selection_principle": "Maximize paper-window CAP total-return edge against the fixed benchmark first, require a router strategy id and covered 01/07 trade-log handoff for every selected ticker, keep active paper-subscription evidence visible, and temporarily exclude PYPL, TMUS, and DDOG.",
        "preferred_candidate_csv": str(PREFERRED_CSV) if PREFERRED_CSV.exists() else "",
        "selection_window": {"start_date": start_date, "end_date": end_date},
        "candidate_counts": {
            "router_total_unique_tickers": len({row["symbol"] for row in candidates}),
            "router_equity_unique_tickers": len({row["symbol"] for row in stock_candidates}),
            "router_crypto_unique_tickers": len({row["symbol"] for row in crypto_candidates}),
            "valid_equity_with_complete_cap_window": valid_counts.get("equity", 0),
            "valid_crypto_with_complete_cap_window": valid_counts.get("crypto", 0),
            "preferred_reference_tickers": len(preferred_candidates),
            "paper_buy_hold_edge_tickers": len(paper_edges),
            "tickers_with_router_strategy_id": sum(1 for row in candidates if row.get("strategy_id")),
            "tickers_with_covered_trade_log_strategy": len(trade_log_symbols),
            "evidence_tickers_with_trade_log_and_paper_subscription": len(evidence_symbols),
        },
        "benchmark_symbols": {"stock": STOCK_BENCHMARK, "crypto": CRYPTO_BENCHMARK},
        "cap_fetch": {
            "requested_symbols": len(symbols),
            "bar_rows": len(bars),
            "warnings": warnings,
        },
        "benchmark_fetch": {
            "requested_symbols": [STOCK_BENCHMARK, CRYPTO_BENCHMARK],
            "requested_start_date": benchmark_start_date,
            "requested_end_date": benchmark_end_date,
            "bar_rows": len(extended_benchmark_bars),
            "warnings": extended_benchmark_warnings,
        },
        "selection_rule": {
            "stock": "Select 100 from CAP-available equity tickers that have a router strategy id and covered 01/07 trade-log handoff, ordered by CAP paper-window total_return to maximize excess versus QQQ; temporarily exclude PYPL, TMUS, and DDOG.",
            "crypto": "Select 10 from CAP-available crypto tickers excluding BTCUSD that have a router strategy id and covered 01/07 trade-log handoff, ordered by CAP paper-window total_return to maximize excess versus BTCUSD; temporarily exclude PYPL, TMUS, and DDOG if present.",
            "weighting": "Equal weight within each portfolio.",
        },
        "selection_metadata": {
            "stock": stock_selection_meta,
            "crypto": crypto_selection_meta,
        },
        "curve_handoff": {
            "stock": stock_curve_metrics,
            "crypto": crypto_curve_metrics,
        },
        "backtest_handoff": {
            "stock": stock_backtest_metrics,
            "crypto": crypto_backtest_metrics,
        },
        "summary": summary_rows,
    }
    write_json(OUT / "selection_audit.json", audit)

    readme = f"""# 06 Portfolio Selection

Generated at `{audit['generated_at']}`.

## Objective

- Select 100 US stock tickers as an equal-weight portfolio ranked by CAP paper-window total return to maximize edge versus `{STOCK_BENCHMARK}`.
- Select 10 crypto tickers as an equal-weight portfolio ranked by CAP paper-window total return to maximize edge versus `{CRYPTO_BENCHMARK}`.

## Workflow

1. Pull the full active ready router strategy universe for user `{USER_ID}`.
2. Group strategies by `target_asset` and `asset_type` to get unique ticker candidates.
3. Load `05_comparisons/paper_minus_benchmarks_sorted.csv` to keep scripted paper-pnl evidence when available.
4. Fetch CAP `market/day_bar` prices for the full prod candidate universe plus `{STOCK_BENCHMARK}` and `{CRYPTO_BENCHMARK}`.
5. Use the common benchmark window `{start_date}` to `{end_date}`.
6. Require a ticker to have both start-date and end-date closes, a router strategy id, and covered 01/07 trade-log handoff.
7. Temporarily exclude `PYPL`, `TMUS`, and `DDOG` from selection.
8. Select the highest CAP paper-window total-return tickers for each asset sleeve.
9. Weight components equally and compare average selected CAP total return to the fixed sleeve benchmark return.

## Search Principle

- Rank by CAP paper-window `total_return` to maximize portfolio-vs-benchmark excess versus the fixed benchmark.
- Require every final ticker to have a router strategy id.
- Require covered 01/07 trade-log handoff and keep active paper-subscription evidence visible in outputs.
- Temporarily exclude `PYPL`, `TMUS`, and `DDOG`.
- Keep final portfolios equal-weight: 1% per stock component and 10% per crypto component.

## Reference Retention

| Portfolio | Preferred Components | Evidence Components | Full-Universe Fill | Outside Components | Replacements |
| --- | ---: | ---: | ---: | ---: | ---: |
| stock_equal_weight_100 | {stock_selection_meta['selected_preferred_count']} | {stock_selection_meta['selected_evidence_count']} | {stock_selection_meta['selected_full_universe_fill_count']} | {stock_selection_meta['selected_outside_count']} | {stock_selection_meta['replacement_count']} |
| crypto_equal_weight_10 | {crypto_selection_meta['selected_preferred_count']} | {crypto_selection_meta['selected_evidence_count']} | {crypto_selection_meta['selected_full_universe_fill_count']} | {crypto_selection_meta['selected_outside_count']} | {crypto_selection_meta['replacement_count']} |

## Results

| Portfolio | Components | Return | Benchmark | Benchmark Return | Excess | Pass |
| --- | ---: | ---: | --- | ---: | ---: | --- |
"""
    for row in summary_rows:
        readme += f"| {row['portfolio']} | {row['component_count']} | {row['portfolio_return_pct']:.2f}% | {row['benchmark_symbol']} | {row['benchmark_return_pct']:.2f}% | {row['excess_return_pct']:.2f}% | {row['beats_benchmark']} |\n"
    readme += """
## Backtest Metrics

| Portfolio | Rows | Covered Components | Missing Components | Backtest Return | Backtest Max DD | Backtest Ann Vol | Backtest Ann Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
"""
    for row in backtest_metrics:
        ann_vol = row['backtest_annualized_volatility_pct'] if row['backtest_annualized_volatility_pct'] is not None else float('nan')
        sharpe = row['backtest_annualized_sharpe'] if row['backtest_annualized_sharpe'] is not None else float('nan')
        readme += f"| {row['portfolio']} | {row['rows']} | {row['covered_component_count']} | {row['missing_component_count']} | {row['backtest_total_return_pct']:.2f}% | {row['backtest_max_drawdown_pct']:.2f}% | {ann_vol:.2f}% | {sharpe:.3f} |\n"
    readme += """
## Paper-Window Curve Metrics

| Portfolio | Curve Rows | Portfolio End | Benchmark End | Portfolio Max DD | Benchmark Max DD | Portfolio Ann Vol | Portfolio Ann Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
"""
    for row in curve_metrics:
        readme += f"| {row['portfolio']} | {row['rows']} | {row['portfolio_end_equity']:.4f}x | {row['benchmark_end_equity']:.4f}x | {row['portfolio_max_drawdown_pct']:.2f}% | {row['benchmark_max_drawdown_pct']:.2f}% | {row['portfolio_annualized_volatility_pct']:.2f}% | {row['portfolio_annualized_sharpe']:.3f} |\n"
    readme += """
## Curve Handoff

- `portfolio_equity_curves.json` mirrors the official website portfolio shape: `equity_curve`, `benchmark_curve`, and `benchmark_label`.
- Website reference: `services/abel-official-website/website/src/components/strategies/EquityCurveChart.tsx` renders a Recharts `AreaChart`; `curve` receives `equity_curve`, `benchmark` receives `benchmark_curve`, `benchmarkLabel` receives `benchmark_label`, and `paperStart` marks the handoff guideline.
- `EquityCurveChart` draws `segment = backtest` as graphite, `segment = paper` as mint, and the benchmark as a muted dashed line.
- Main website callers are `/abel-portfolio` and `/abel-portfolio/crypto` through `services/abel-official-website/website/src/app/strategies/StrategiesContent.tsx`, plus `/strategies/[id]` portfolio detail through `services/abel-official-website/website/src/app/strategies/[id]/AbelEtfDetailContent.tsx`.
- Website data mapping is `services/abel-official-website/website/src/data/strategies.ts::mapPublicPortfolio()`: router `portfolio.chart.points` becomes `portfolio.equity_curve`; router `portfolio.chart.benchmark.points` becomes `portfolio.benchmark_curve`.
- `equity_curve` includes a graphite-style backtest segment plus a scaled paper segment; `paper_equity_curve` keeps the paper window normalized to 1.0.
- `benchmark_curve` is requested from CAP back to the earliest portfolio backtest start and normalized from the first available benchmark close on combined `equity_curve` dates, so it extends before paper start when benchmark data is available.
- `portfolio_backtest_components.csv` records which 01 strategy/trade-log was used for each selected ticker's backtest component.
- `portfolio_backtest_curves.csv` is the flat daily backtest time series rebuilt from daily-compounded trade-log `pnl`; `cum_return` is not used for the 06 portfolio backtest curve.
- `portfolio_backtest_metrics.csv` lists backtest return, drawdown, volatility, Sharpe, and coverage metrics.
- `portfolio_equity_curves.csv` is the flat daily time series used by the static handoff chart.
- `portfolio_curve_metrics.csv` lists return, drawdown, volatility, Sharpe, and benchmark comparison metrics.
- Curves use normalized CAP closes. Portfolio equity is `sum(weight * close_t / close_start)` on common dates shared by all selected components and the benchmark.
- Backtest uses `07_backtest_trade_logs`; every selected ticker must have a strategy id. For each selected ticker with a covered 01/07 trade log, the highest-Sharpe available 01 strategy is selected, each component curve is rebuilt as `equity_t = equity_(t-1) * (1 + pnl_t)`, and covered components are equal-weighted on common dates. Components without a covered 01/07 trade log keep their router strategy id and are reported in coverage as missing.

## Files

- `stock_equal_weight_portfolio.csv`: selected 100 stock components.
- `crypto_equal_weight_portfolio.csv`: selected 10 crypto components.
- `portfolio_component_returns.csv`: all valid candidates with complete CAP window returns.
- `portfolio_backtest_components.csv`: selected representative strategy per component for backtest.
- `portfolio_backtest_curves.csv`: flat backtest equity curves.
- `portfolio_backtest_metrics.csv`: backtest metrics and component coverage.
- `portfolio_equity_curves.json`: website-compatible portfolio and benchmark curve handoff.
- `portfolio_equity_curves.csv`: flat daily portfolio-vs-benchmark curve rows.
- `portfolio_curve_metrics.csv`: curve-level metrics for both portfolios.
- `portfolio_vs_benchmark_summary.csv`: portfolio-level pass/fail summary.
- `selection_audit.json`: full workflow audit metadata.
- `summary.html`: human-readable report.

## Caveat

This is an in-window realized-return selection workflow. It demonstrates a portfolio construction that beat the benchmark over the stated historical window; it is not an out-of-sample performance claim.
"""
    if WRITE_GENERATED_READMES:
        (OUT / "README.md").write_text(readme, encoding="utf-8")
    update_manifest(audit, component_rows, stock_rows, crypto_rows)

    print(json.dumps({
        "start_date": start_date,
        "end_date": end_date,
        "stock_components": len(stock_rows),
        "crypto_components": len(crypto_rows),
        "summary": summary_rows,
        "output_dir": str(OUT),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
