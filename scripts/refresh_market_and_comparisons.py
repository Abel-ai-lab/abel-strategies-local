from __future__ import annotations

import csv
import html
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("ROUTER_CONFIG", ROOT / "config" / "router.prod.local.yaml"))
SELECTION_CSV = os.environ.get("SELECTION_CSV")
CORE_ETFS = ["SPY", "QQQ", "DIA", "IWM"]
TEMPORARY_EXCLUDED_SYMBOLS = {"PYPL", "TMUS", "DDOG"}
WRITE_GENERATED_READMES = os.environ.get("WRITE_GENERATED_READMES") == "1"


def normalize(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def read_json(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def json_write(path: str, data) -> None:
    p = ROOT / path
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def text_write(path: str, text: str) -> None:
    p = ROOT / path
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, p)


def csv_write(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    p = ROOT / path
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    os.replace(tmp, p)


def csv_count(path: str) -> int:
    with (ROOT / path).open("r", encoding="utf-8", newline="") as f:
        return max(sum(1 for _ in f) - 1, 0)


def read_csv_rows(path: str) -> list[dict[str, str]]:
    p = ROOT / path
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def pct(value):
    return None if value is None else value * 100.0


def round_float(value, digits=12):
    if value is None or value == "":
        return ""
    return round(float(value), digits)


def fmt_pct(value) -> str:
    if value is None or value == "":
        return "-"
    return f"{float(value) * 100:.2f}%"


def load_selection_stock_symbols() -> set[str]:
    if not SELECTION_CSV:
        return set()
    path = Path(SELECTION_CSV)
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8", newline="") as f:
        return {row["symbol"].strip() for row in csv.DictReader(f) if row.get("asset_type") == "stock" and row.get("symbol")}


def fetch_daily_bars(cur, table: str, symbols: list[str], start_date: str, end_date: str) -> list[dict]:
    if not symbols:
        return []
    cur.execute(
        f"""
        select
          symbol as ticker,
          left(date, 10) as trading_date,
          (array_agg(open order by date_timestamp asc))[1] as open,
          max(high) as high,
          min(low) as low,
          (array_agg(close order by date_timestamp desc))[1] as close,
          sum(volume) as volume
        from public.{table}
        where deleted = 0
          and symbol = any(%s)
          and left(date, 10) between %s and %s
        group by symbol, left(date, 10)
        order by symbol asc, left(date, 10) asc
        """,
        (symbols, start_date, end_date),
    )
    rows = []
    for row in cur.fetchall():
        trading_date = row["trading_date"]
        rows.append({
            "ticker": row["ticker"],
            "trading_date": trading_date,
            "timestamp": f"{trading_date}T00:00:00Z",
            "open": normalize(row["open"]),
            "high": normalize(row["high"]),
            "low": normalize(row["low"]),
            "close": normalize(row["close"]),
            "volume": normalize(row["volume"]),
        })
    return rows


strategies = read_json("01_strategy_universe/strategies.json")
paper_rows = read_json("02_paper_actuals/paper_daily_rows.json")
performance_points = read_json("02_paper_actuals/performance_points.json")
llm_rows = read_json("04_llm_benchmark/llm_benchmark_rows.json")
manifest_path = ROOT / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

strategy_by_id = {str(row["strategy_id"]): row for row in strategies}
target_tickers = sorted({row.get("target_asset") for row in strategies if row.get("target_asset")})
asset_type_by_ticker = {}
for row in strategies:
    ticker = row.get("target_asset")
    if ticker:
        asset_type_by_ticker[ticker] = row.get("asset_type")

paper_by_sid = defaultdict(list)
paper_by_sid_date = {}
for row in paper_rows:
    sid = str(row["strategy_id"])
    paper_by_sid[sid].append(row)
    paper_by_sid_date[(sid, row["trading_date"])] = row
for rows in paper_by_sid.values():
    rows.sort(key=lambda r: r["trading_date"])

all_paper_dates = [row["trading_date"] for row in paper_rows]
if not all_paper_dates:
    raise SystemExit("No paper rows available; cannot refresh market comparisons")
min_paper_date = min(all_paper_dates)
max_paper_date = max(all_paper_dates)

stock_tickers = sorted(t for t in target_tickers if asset_type_by_ticker.get(t) != "crypto" and not t.endswith("USD"))
crypto_tickers = sorted(t for t in target_tickers if t not in stock_tickers)

cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
pg = cfg["postgres"]
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
    target_bars = []
    target_bars.extend(fetch_daily_bars(cur, "ref_stock_price", stock_tickers, min_paper_date, max_paper_date))
    target_bars.extend(fetch_daily_bars(cur, "ref_crypto_price", crypto_tickers, min_paper_date, max_paper_date))
    core_bars = fetch_daily_bars(cur, "ref_stock_price", CORE_ETFS, "2015-01-01", datetime.now(timezone.utc).date().isoformat())
conn.close()

target_bar_keys = {(row["ticker"], row["trading_date"]) for row in target_bars}
fallback_by_ticker_date = {}
for row in paper_rows:
    sid = str(row["strategy_id"])
    strategy = strategy_by_id.get(sid)
    ticker = strategy.get("target_asset") if strategy else None
    close = row.get("close")
    if not ticker or close is None or (ticker, row["trading_date"]) in target_bar_keys:
        continue
    fallback_by_ticker_date.setdefault((ticker, row["trading_date"]), float(close))
for (ticker, trading_date), close in sorted(fallback_by_ticker_date.items()):
    target_bars.append({
        "ticker": ticker,
        "trading_date": trading_date,
        "timestamp": f"{trading_date}T00:00:00Z",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": None,
    })

target_bars.sort(key=lambda r: (r["ticker"], r["trading_date"]))
core_bars.sort(key=lambda r: (r["ticker"], r["trading_date"]))
json_write("03_market_data/buy_hold_cap_day_bars.json", target_bars)
json_write("03_market_data/core_us_benchmark_etf_day_bars.json", core_bars)
csv_write("03_market_data/core_us_benchmark_etf_day_bars.csv", ["ticker", "trading_date", "timestamp", "open", "high", "low", "close", "volume"], core_bars)

bars_by_ticker_date = {(row["ticker"], row["trading_date"]): row for row in target_bars}
core_summary = []
core_warnings = []
for symbol in CORE_ETFS:
    rows = [r for r in core_bars if r["ticker"] == symbol]
    if rows:
        first = rows[0]
        last = rows[-1]
        first_close = float(first["close"])
        last_close = float(last["close"])
        total_return = (last_close / first_close - 1.0) if first_close else None
        core_summary.append({
            "symbol": symbol,
            "row_count": len(rows),
            "first_trading_date": first["trading_date"],
            "last_trading_date": last["trading_date"],
            "first_close": first_close,
            "last_close": last_close,
            "total_return": round_float(total_return),
            "warning": False,
        })
    else:
        core_summary.append({"symbol": symbol, "row_count": 0, "warning": True})
        core_warnings.append({"symbol": symbol, "reason": "no router ref_stock_price rows"})
csv_write("03_market_data/core_us_benchmark_etf_summary.csv", ["symbol", "row_count", "first_trading_date", "last_trading_date", "first_close", "last_close", "total_return", "warning"], core_summary)
json_write("03_market_data/core_us_benchmark_etf_warnings.json", core_warnings)

buy_hold_rows = []
buy_hold_summary_rows = []
buy_hold_warnings = []
buy_hold_return_by_sid = {}
buy_hold_window_by_sid = {}

for sid, strategy in strategy_by_id.items():
    ticker = strategy.get("target_asset")
    rows = paper_by_sid.get(sid, [])
    warning_parts = []
    if not ticker or not rows:
        warning_parts.append("missing ticker or paper rows")
        buy_hold_warnings.append({"strategy_id": sid, "ticker": ticker, "reason": "; ".join(warning_parts)})
        buy_hold_summary_rows.append({
            "strategy_id": sid,
            "display_name": strategy.get("display_name"),
            "ticker": ticker,
            "paper_rows_count": len(rows),
            "buy_hold_rows_count": 0,
            "warnings": "; ".join(warning_parts),
        })
        continue

    first_close = None
    previous_close = None
    produced = []
    missing_dates = []
    for paper in rows:
        trading_date = paper["trading_date"]
        bar = bars_by_ticker_date.get((ticker, trading_date))
        if not bar:
            missing_dates.append(trading_date)
            continue
        close = float(bar["close"])
        if first_close is None:
            first_close = close
        daily_return = 0.0 if previous_close is None or previous_close == 0 else close / previous_close - 1.0
        cum_return = 0.0 if not first_close else close / first_close - 1.0
        produced.append({
            "strategy_id": sid,
            "session_id": str(strategy.get("session_id")) if strategy.get("session_id") is not None else "",
            "display_name": strategy.get("display_name"),
            "ticker": ticker,
            "strategy_start_date": rows[0]["trading_date"],
            "strategy_latest_paper_date": rows[-1]["trading_date"],
            "trading_date": trading_date,
            "day_index": len(produced),
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": close,
            "volume": bar.get("volume"),
            "first_close": first_close,
            "previous_close": previous_close,
            "buy_hold_daily_return": round_float(daily_return),
            "buy_hold_cum_return": round_float(cum_return),
            "buy_hold_chart_value": round_float(1.0 + cum_return),
            "paper_has_row": True,
            "paper_close": paper.get("close"),
            "paper_asset_return": paper.get("asset_return"),
            "paper_position": paper.get("position"),
            "paper_next_position": paper.get("next_position"),
            "paper_pnl": paper.get("pnl"),
            "paper_benchmark_return": paper.get("benchmark_return"),
            "paper_generated_by_run_id": str(paper.get("generated_by_run_id")) if paper.get("generated_by_run_id") is not None else "",
        })
        previous_close = close
    if missing_dates:
        warning_parts.append(f"missing market bars for {len(missing_dates)} paper dates")
        buy_hold_warnings.append({"strategy_id": sid, "ticker": ticker, "reason": warning_parts[-1], "missing_dates": missing_dates})
    buy_hold_rows.extend(produced)
    latest_paper = rows[-1]
    latest = produced[-1] if produced else None
    if latest:
        buy_hold_return_by_sid[sid] = float(latest["buy_hold_cum_return"])
        buy_hold_window_by_sid[sid] = {"from": produced[0]["trading_date"], "to": produced[-1]["trading_date"], "rows": len(produced)}
    buy_hold_summary_rows.append({
        "strategy_id": sid,
        "display_name": strategy.get("display_name"),
        "ticker": ticker,
        "paper_start_date": rows[0]["trading_date"],
        "paper_latest_date": rows[-1]["trading_date"],
        "paper_rows_count": len(rows),
        "buy_hold_rows_count": len(produced),
        "first_close": produced[0]["first_close"] if produced else "",
        "latest_close": latest["close"] if latest else "",
        "buy_hold_cum_return": latest["buy_hold_cum_return"] if latest else "",
        "buy_hold_chart_value": latest["buy_hold_chart_value"] if latest else "",
        "latest_paper_position": latest_paper.get("position"),
        "latest_paper_next_position": latest_paper.get("next_position"),
        "latest_paper_pnl": latest_paper.get("pnl"),
        "warnings": "; ".join(warning_parts),
    })

buy_hold_fields = [
    "strategy_id", "session_id", "display_name", "ticker", "strategy_start_date", "strategy_latest_paper_date",
    "trading_date", "day_index", "open", "high", "low", "close", "volume", "first_close", "previous_close",
    "buy_hold_daily_return", "buy_hold_cum_return", "buy_hold_chart_value", "paper_has_row", "paper_close",
    "paper_asset_return", "paper_position", "paper_next_position", "paper_pnl", "paper_benchmark_return", "paper_generated_by_run_id",
]
json_write("05_comparisons/strategy_buy_hold_daily_rows.json", buy_hold_rows)
csv_write("05_comparisons/strategy_buy_hold_daily_rows.csv", buy_hold_fields, buy_hold_rows)
csv_write("05_comparisons/strategy_buy_hold_summary.csv", ["strategy_id", "display_name", "ticker", "paper_start_date", "paper_latest_date", "paper_rows_count", "buy_hold_rows_count", "first_close", "latest_close", "buy_hold_cum_return", "buy_hold_chart_value", "latest_paper_position", "latest_paper_next_position", "latest_paper_pnl", "warnings"], buy_hold_summary_rows)
json_write("05_comparisons/strategy_buy_hold_warnings.json", buy_hold_warnings)

point_by_sid = defaultdict(list)
for row in performance_points:
    point_by_sid[str(row["strategy_id"])].append(row)
for rows in point_by_sid.values():
    rows.sort(key=lambda r: r["trading_date"])
paper_compounded_by_sid = {
    row["strategy_id"]: row
    for row in read_csv_rows("02_paper_actuals/paper_compounded_summary.csv")
    if row.get("paper_total_return") not in {None, ""}
}

llm_by_sid = defaultdict(list)
for row in llm_rows:
    if row.get("source_llm_run_id"):
        llm_by_sid[str(row["strategy_id"])].append(row)
for rows in llm_by_sid.values():
    rows.sort(key=lambda r: r["trading_date"])

comparison_rows = []
for sid, strategy in strategy_by_id.items():
    p_rows = paper_by_sid.get(sid, [])
    if not p_rows:
        comparison_rows.append({
            "source_llm_rank": "",
            "target_asset": strategy.get("target_asset"),
            "strategy_id": sid,
            "display_name": strategy.get("display_name"),
            "asset_type": strategy.get("asset_type"),
            "paper_from_date": "",
            "paper_to_date": "",
            "paper_days": 0,
            "paper_return": "",
            "paper_return_pct": "",
            "llm_from_date": "",
            "llm_to_date": "",
            "llm_rows": 0,
            "llm_return": "",
            "llm_return_pct": "",
            "paper_minus_llm_return": "",
            "paper_minus_llm_return_pct": "",
            "has_llm": "N",
            "buy_hold_from_date": "",
            "buy_hold_to_date": "",
            "buy_hold_rows": 0,
            "buy_hold_return": "",
            "buy_hold_return_pct": "",
            "paper_minus_buy_hold_return": "",
            "paper_minus_buy_hold_return_pct": "",
            "has_buy_hold": "N",
            "benchmark_count": 0,
            "benchmark_pass": "N",
            "benchmark_edge_return": "",
            "benchmark_edge_return_pct": "",
            "benchmark_edge_source": "",
        })
        continue
    points = point_by_sid.get(sid, [])
    if sid in paper_compounded_by_sid:
        paper_return = float(paper_compounded_by_sid[sid]["paper_total_return"])
    else:
        paper_return = float(points[-1].get("paper_strategy_cum_return") or 0.0) if points else sum(float(r.get("pnl") or 0.0) for r in p_rows)
    llm = llm_by_sid.get(sid, [])
    has_llm = bool(llm)
    llm_return = float(llm[-1].get("benchmark_cum_return")) if has_llm else None
    bh_window = buy_hold_window_by_sid.get(sid)
    has_buy_hold = sid in buy_hold_return_by_sid
    buy_hold_return = buy_hold_return_by_sid.get(sid)
    paper_minus_llm = paper_return - llm_return if has_llm else None
    paper_minus_buy_hold = paper_return - buy_hold_return if has_buy_hold else None
    benchmark_diffs = [x for x in [paper_minus_llm, paper_minus_buy_hold] if x is not None]
    benchmark_pass = bool(benchmark_diffs) and all(x >= 0 for x in benchmark_diffs)
    edge = min(benchmark_diffs) if benchmark_diffs else None
    if edge is None:
        edge_source = ""
    elif paper_minus_llm is not None and abs(edge - paper_minus_llm) < 1e-15:
        edge_source = "llm"
    else:
        edge_source = "buy_hold"
    comparison_rows.append({
        "source_llm_rank": "",
        "target_asset": strategy.get("target_asset"),
        "strategy_id": sid,
        "display_name": strategy.get("display_name"),
        "asset_type": strategy.get("asset_type"),
        "paper_from_date": p_rows[0]["trading_date"],
        "paper_to_date": p_rows[-1]["trading_date"],
        "paper_days": len(p_rows),
        "paper_return": round_float(paper_return),
        "paper_return_pct": round_float(pct(paper_return), 10),
        "llm_from_date": llm[0]["trading_date"] if has_llm else "",
        "llm_to_date": llm[-1]["trading_date"] if has_llm else "",
        "llm_rows": len(llm),
        "llm_return": round_float(llm_return),
        "llm_return_pct": round_float(pct(llm_return), 10),
        "paper_minus_llm_return": round_float(paper_minus_llm),
        "paper_minus_llm_return_pct": round_float(pct(paper_minus_llm), 10),
        "has_llm": "Y" if has_llm else "N",
        "buy_hold_from_date": bh_window["from"] if bh_window else "",
        "buy_hold_to_date": bh_window["to"] if bh_window else "",
        "buy_hold_rows": bh_window["rows"] if bh_window else 0,
        "buy_hold_return": round_float(buy_hold_return),
        "buy_hold_return_pct": round_float(pct(buy_hold_return), 10),
        "paper_minus_buy_hold_return": round_float(paper_minus_buy_hold),
        "paper_minus_buy_hold_return_pct": round_float(pct(paper_minus_buy_hold), 10),
        "has_buy_hold": "Y" if has_buy_hold else "N",
        "benchmark_count": len(benchmark_diffs),
        "benchmark_pass": "Y" if benchmark_pass else "N",
        "benchmark_edge_return": round_float(edge),
        "benchmark_edge_return_pct": round_float(pct(edge), 10),
        "benchmark_edge_source": edge_source,
    })

llm_ranked = sorted([r for r in comparison_rows if r["has_llm"] == "Y"], key=lambda r: float(r["paper_minus_llm_return"] or -999), reverse=True)
for rank, row in enumerate(llm_ranked, 1):
    row["source_llm_rank"] = rank

comparison_rows.sort(key=lambda r: (r["benchmark_pass"] != "Y", -float(r["benchmark_edge_return"] or -999)))
for rank, row in enumerate(comparison_rows, 1):
    row["rank"] = rank

comparison_fields = ["rank", "source_llm_rank", "target_asset", "strategy_id", "display_name", "asset_type", "paper_from_date", "paper_to_date", "paper_days", "paper_return", "paper_return_pct", "llm_from_date", "llm_to_date", "llm_rows", "llm_return", "llm_return_pct", "paper_minus_llm_return", "paper_minus_llm_return_pct", "has_llm", "buy_hold_from_date", "buy_hold_to_date", "buy_hold_rows", "buy_hold_return", "buy_hold_return_pct", "paper_minus_buy_hold_return", "paper_minus_buy_hold_return_pct", "has_buy_hold", "benchmark_count", "benchmark_pass", "benchmark_edge_return", "benchmark_edge_return_pct", "benchmark_edge_source"]
csv_write("05_comparisons/paper_minus_benchmarks_sorted.csv", comparison_fields, comparison_rows)

paper_minus_llm_rows = [r for r in comparison_rows if r["has_llm"] == "Y"]
paper_minus_llm_rows.sort(key=lambda r: float(r["paper_minus_llm_return"] or -999), reverse=True)
csv_write("05_comparisons/paper_minus_llm_sorted.csv", comparison_fields, paper_minus_llm_rows)
text_write("05_comparisons/sorted_paper_minus_llm.txt", "\n".join(f"{i}. {r['target_asset']} {r['paper_minus_llm_return_pct']}% {r['display_name']}" for i, r in enumerate(paper_minus_llm_rows, 1)) + "\n")

both_rows = [dict(r) for r in comparison_rows if r["has_llm"] == "Y" and r["has_buy_hold"] == "Y"]
for rank, row in enumerate(both_rows, 1):
    row["both_benchmarks_rank"] = rank
csv_write("05_comparisons/paper_minus_both_benchmarks_sorted.csv", ["both_benchmarks_rank"] + comparison_fields, both_rows)

stock_selection_symbols = load_selection_stock_symbols()
final_rows = []
seen_tickers = set()
for row in comparison_rows:
    ticker = row["target_asset"]
    if row["benchmark_pass"] != "Y" or row["asset_type"] != "equity" or ticker.endswith("USD") or ticker in TEMPORARY_EXCLUDED_SYMBOLS:
        continue
    if float(row["paper_return"] or 0.0) < -0.02 or ticker in seen_tickers:
        continue
    seen_tickers.add(ticker)
    final_rows.append({
        "selection_rank": len(final_rows) + 1,
        "ticker": ticker,
        "strategy_id": row["strategy_id"],
        "display_name": row["display_name"],
        "csv_rank": row["rank"],
        "paper_from_date": row["paper_from_date"],
        "paper_to_date": row["paper_to_date"],
        "paper_days": row["paper_days"],
        "paper_return_pct": row["paper_return_pct"],
        "llm_return_pct": row["llm_return_pct"],
        "buy_hold_return_pct": row["buy_hold_return_pct"],
        "paper_minus_llm_return_pct": row["paper_minus_llm_return_pct"],
        "paper_minus_buy_hold_return_pct": row["paper_minus_buy_hold_return_pct"],
        "benchmark_edge_return_pct": row["benchmark_edge_return_pct"],
        "benchmark_edge_source": row["benchmark_edge_source"],
        "has_llm": row["has_llm"],
        "is_common_qqq_constituent": "Y" if ticker in stock_selection_symbols else "N",
    })
    if len(final_rows) == 30:
        break
final_fields = ["selection_rank", "ticker", "strategy_id", "display_name", "csv_rank", "paper_from_date", "paper_to_date", "paper_days", "paper_return_pct", "llm_return_pct", "buy_hold_return_pct", "paper_minus_llm_return_pct", "paper_minus_buy_hold_return_pct", "benchmark_edge_return_pct", "benchmark_edge_source", "has_llm", "is_common_qqq_constituent"]
csv_write("05_comparisons/final_abel_portfolio_selection_latest_available.csv", final_fields, final_rows)

def table_md(rows: list[dict], cols: list[str], limit=30) -> str:
    shown = rows[:limit]
    if not shown:
        return "No rows.\n"
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in shown:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def display_value(row: dict, col: str) -> str:
    value = row.get(col, "")
    if value is None or value == "":
        return "-"
    if col.endswith("_pct"):
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def cell_class(row: dict, col: str) -> str:
    value = row.get(col, "")
    classes = []
    if col.endswith("_pct") and value not in (None, ""):
        try:
            number = float(value)
            classes.append("pos" if number > 0 else "neg" if number < 0 else "zero")
        except (TypeError, ValueError):
            pass
    if col == "benchmark_pass":
        classes.append("pass" if value == "Y" else "fail")
    if col in {"rank", "source_llm_rank", "selection_rank", "paper_days", "benchmark_count"}:
        classes.append("num")
    return " ".join(classes)


def html_table(rows: list[dict], columns: list[tuple[str, str]], empty: str = "No rows.") -> str:
    if not rows:
        return f"<p class=\"empty\">{html.escape(empty)}</p>"
    header = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body_rows = []
    for row in rows:
        cells = []
        for key, _ in columns:
            klass = cell_class(row, key)
            class_attr = f' class="{klass}"' if klass else ""
            cells.append(f"<td{class_attr}>{html.escape(display_value(row, key))}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<div class=\"table-wrap\"><table><thead><tr>" + header + "</tr></thead><tbody>" + "\n".join(body_rows) + "</tbody></table></div>"


def html_page(title: str, subtitle: str, cards: list[tuple[str, str]], sections: list[tuple[str, str]]) -> str:
    card_html = "".join(
        f"<article class=\"card\"><div class=\"card-value\">{html.escape(str(value))}</div><div class=\"card-label\">{html.escape(label)}</div></article>"
        for label, value in cards
    )
    section_html = "".join(f"<section><h2>{html.escape(name)}</h2>{content}</section>" for name, content in sections)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7fb;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --line: #dde3ee;
      --head: #eef3ff;
      --pos: #087443;
      --neg: #b42318;
      --zero: #475467;
      --accent: #3157d5;
    }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 28px; }}
    header {{ margin-bottom: 20px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: -0.02em; }}
    h2 {{ margin: 28px 0 12px; font-size: 18px; }}
    .subtitle {{ margin: 0; color: var(--muted); }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 20px 0 8px; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04); }}
    .card-value {{ font-size: 24px; font-weight: 750; letter-spacing: -0.02em; }}
    .card-label {{ margin-top: 4px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
    section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 4px 16px 16px; margin-top: 16px; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04); }}
    .table-wrap {{ overflow: auto; border: 1px solid var(--line); border-radius: 12px; }}
    table {{ border-collapse: separate; border-spacing: 0; min-width: 100%; white-space: nowrap; }}
    th, td {{ padding: 9px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: var(--head); z-index: 1; color: #344054; font-size: 12px; text-transform: uppercase; letter-spacing: 0.035em; }}
    tbody tr:hover {{ background: #f8fbff; }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .pos {{ color: var(--pos); font-weight: 650; }}
    .neg {{ color: var(--neg); font-weight: 650; }}
    .zero {{ color: var(--zero); }}
    .pass, .fail {{ display: inline-block; min-width: 1.6em; text-align: center; border-radius: 999px; padding: 1px 8px; font-weight: 700; }}
    .pass {{ background: #dcfae6; color: #067647; }}
    .fail {{ background: #fee4e2; color: #b42318; }}
    .empty {{ color: var(--muted); }}
    @media (max-width: 720px) {{ main {{ padding: 16px; }} h1 {{ font-size: 22px; }} th, td {{ padding: 8px; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{html.escape(title)}</h1>
      <p class="subtitle">{html.escape(subtitle)}</p>
    </header>
    <div class="cards">{card_html}</div>
    {section_html}
  </main>
</body>
</html>
"""

now = datetime.now(timezone.utc).isoformat()
summary_md = f"""# Paper Strategies vs LLM Bench / Buy-and-Hold

Generated from refreshed router market/reference data on {now}.

| Metric | Value |
| --- | ---: |
| CSV rows | {len(comparison_rows)} |
| Has LLM bench | {sum(1 for r in comparison_rows if r['has_llm'] == 'Y')} |
| Has buy-and-hold | {sum(1 for r in comparison_rows if r['has_buy_hold'] == 'Y')} |
| Has both benchmarks | {len(both_rows)} |
| Benchmark pass rows | {sum(1 for r in comparison_rows if r['benchmark_pass'] == 'Y')} |
| Final selection rows | {len(final_rows)} |

## Top Benchmark Pass Rows

{table_md([r for r in comparison_rows if r['benchmark_pass'] == 'Y'], ['rank', 'target_asset', 'paper_return_pct', 'llm_return_pct', 'buy_hold_return_pct', 'benchmark_edge_return_pct', 'benchmark_edge_source', 'display_name'], 20)}
"""
text_write("05_comparisons/paper_minus_benchmarks_summary.md", summary_md)
benchmark_cards = [
    ("Strategies", len(comparison_rows)),
    ("With LLM", sum(1 for r in comparison_rows if r["has_llm"] == "Y")),
    ("With Buy-Hold", sum(1 for r in comparison_rows if r["has_buy_hold"] == "Y")),
    ("Both Benchmarks", len(both_rows)),
    ("Benchmark Pass", sum(1 for r in comparison_rows if r["benchmark_pass"] == "Y")),
    ("Final Picks", len(final_rows)),
]
benchmark_columns = [
    ("rank", "Rank"),
    ("target_asset", "Ticker"),
    ("strategy_id", "Strategy ID"),
    ("asset_type", "Asset"),
    ("paper_days", "Paper Days"),
    ("paper_return_pct", "Paper"),
    ("llm_return_pct", "LLM"),
    ("buy_hold_return_pct", "Buy-Hold"),
    ("benchmark_edge_return_pct", "Edge"),
    ("benchmark_edge_source", "Edge Source"),
    ("benchmark_pass", "Pass"),
    ("display_name", "Strategy"),
]
final_columns = [
    ("selection_rank", "Pick"),
    ("ticker", "Ticker"),
    ("strategy_id", "Strategy ID"),
    ("paper_return_pct", "Paper"),
    ("llm_return_pct", "LLM"),
    ("buy_hold_return_pct", "Buy-Hold"),
    ("benchmark_edge_return_pct", "Edge"),
    ("has_llm", "Has LLM"),
    ("is_common_qqq_constituent", "QQQ List"),
    ("display_name", "Strategy"),
]
benchmark_html = html_page(
    "Paper Strategies vs LLM Bench / Buy-and-Hold",
    f"Refreshed {now}. Full strategy universe included; rows without paper data are retained with empty metrics.",
    benchmark_cards,
    [
        ("Final Selection", html_table(final_rows, final_columns, "No final picks passed the selection rule.")),
        ("Benchmark Pass Rows", html_table([r for r in comparison_rows if r["benchmark_pass"] == "Y"], benchmark_columns)),
        ("All Strategies", html_table(comparison_rows, benchmark_columns)),
    ],
)
text_write("05_comparisons/paper_minus_benchmarks_summary.html", benchmark_html)

both_md = f"""# Paper Strategies Beating Both Benchmarks

Generated from refreshed router market/reference data on {now}.

Rows with both LLM and buy-and-hold benchmarks: {len(both_rows)}.
Rows beating both: {sum(1 for r in both_rows if r['benchmark_pass'] == 'Y')}.

{table_md([r for r in both_rows if r['benchmark_pass'] == 'Y'], ['both_benchmarks_rank', 'target_asset', 'paper_return_pct', 'llm_return_pct', 'buy_hold_return_pct', 'benchmark_edge_return_pct', 'display_name'], 30)}
"""
text_write("05_comparisons/paper_minus_both_benchmarks_summary.md", both_md)

llm_md = f"""# Paper Strategies vs LLM Benchmark

Generated from refreshed router market/reference data on {now}.

{table_md(paper_minus_llm_rows, ['source_llm_rank', 'target_asset', 'paper_return_pct', 'llm_return_pct', 'paper_minus_llm_return_pct', 'display_name'], 30)}
"""
text_write("05_comparisons/paper_minus_llm_summary.md", llm_md)
llm_cards = [
    ("LLM Rows", len(paper_minus_llm_rows)),
    ("Paper Beat LLM", sum(1 for r in paper_minus_llm_rows if float(r["paper_minus_llm_return"] or 0) >= 0)),
    ("Avg Edge", f"{(sum(float(r['paper_minus_llm_return_pct'] or 0) for r in paper_minus_llm_rows) / len(paper_minus_llm_rows)):.2f}%" if paper_minus_llm_rows else "-"),
]
llm_columns = [
    ("source_llm_rank", "LLM Rank"),
    ("target_asset", "Ticker"),
    ("strategy_id", "Strategy ID"),
    ("paper_days", "Paper Days"),
    ("paper_return_pct", "Paper"),
    ("llm_return_pct", "LLM"),
    ("paper_minus_llm_return_pct", "Paper - LLM"),
    ("paper_from_date", "Paper From"),
    ("paper_to_date", "Paper To"),
    ("display_name", "Strategy"),
]
llm_html = html_page(
    "Paper Strategies vs LLM Benchmark",
    f"Refreshed {now}. Sorted by Paper - LLM edge descending.",
    llm_cards,
    [("LLM Benchmark Ranking", html_table(paper_minus_llm_rows, llm_columns))],
)
text_write("05_comparisons/paper_minus_llm_summary.html", llm_html)

final_md = f"""# Final Abel Portfolio Selection - Latest Available Data

Data refreshed from router PostgreSQL strategy/paper/LLM data and router `ref_stock_price` / `ref_crypto_price` daily aggregates on {now}.

Selection rule: `benchmark_pass = Y`, `asset_type = equity`, ticker not ending in `USD`, `paper_return >= -2%`, unique ticker, top 30 by refreshed comparison rank.

{table_md(final_rows, ['selection_rank', 'ticker', 'strategy_id', 'paper_return_pct', 'llm_return_pct', 'buy_hold_return_pct', 'benchmark_edge_return_pct', 'has_llm', 'is_common_qqq_constituent'], 30)}
"""
text_write("05_comparisons/final_abel_portfolio_selection_latest_available.md", final_md)

manifest_counts = dict(manifest.get("counts", {}))
manifest_counts.update({
    "buyHoldCapBars": len(target_bars),
    "coreUsBenchmarkEtfDayBars": len(core_bars),
    "coreUsBenchmarkEtfSymbols": len(CORE_ETFS),
    "coreUsBenchmarkEtfWarnings": len(core_warnings),
    "strategyBuyHoldDailyRows": len(buy_hold_rows),
    "strategyBuyHoldSummaryRows": len(buy_hold_summary_rows),
    "strategyBuyHoldWarnings": len(buy_hold_warnings),
    "paperMinusBenchmarksRows": len(comparison_rows),
    "paperMinusBenchmarksPassRows": sum(1 for r in comparison_rows if r["benchmark_pass"] == "Y"),
    "paperMinusBothBenchmarksRows": len(both_rows),
    "paperMinusBothBenchmarksWinners": sum(1 for r in both_rows if r["benchmark_pass"] == "Y"),
    "finalAbelPortfolioLatestAvailableRows": len(final_rows),
})
manifest["counts"] = {k: manifest_counts[k] for k in sorted(manifest_counts)}
manifest["generatedAt"] = now
manifest["refreshedAt"] = now
manifest["organizedAt"] = datetime.now(timezone.utc).date().isoformat()
manifest["refreshedFolders"] = ["01_strategy_universe", "02_paper_actuals", "03_market_data", "04_llm_benchmark", "05_comparisons"]
source = manifest.get("source", "router postgres config.local.yaml")
source = re.sub(r"( with paper-close fallback)+", "", source)
source = source.replace("; CAP market data retained from previous export (API key invalid)", "")
source = source.replace("; 03/05 refreshed from router ref_stock_price/ref_crypto_price", "")
manifest["source"] = source + "; 03/05 refreshed from router ref_stock_price/ref_crypto_price with paper-close fallback"
json_write("manifest.json", manifest)

if WRITE_GENERATED_READMES:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme = readme.replace("CAP `/market/day_bar` files are retained from the previous local export because the available CAP API key was rejected during this refresh.", "Market data was refreshed from router `ref_stock_price` / `ref_crypto_price` daily aggregates.")
    readme = readme.replace("- `03_market_data/` and `05_comparisons/` are retained from the previous export because CAP `/api/market/day_bar` returned `API Key Invalid` for the available local key.", "- `03_market_data/` and `05_comparisons/` were refreshed from router `ref_stock_price` / `ref_crypto_price` daily aggregates.")
    readme = readme.replace("- Router-backed folders refreshed in this run: `01_strategy_universe/`, `02_paper_actuals/`, and `04_llm_benchmark/`.", "- Router-backed folders refreshed in this run: `01_strategy_universe/`, `02_paper_actuals/`, `03_market_data/`, `04_llm_benchmark/`, and `05_comparisons/`.")
    readme = re.sub(r"- Buy-and-hold (?:CAP|market) bars: .*", f"- Buy-and-hold market bars: {len(target_bars)}", readme)
    readme = re.sub(r"- Strategy buy-and-hold daily rows: .*", f"- Strategy buy-and-hold daily rows: {len(buy_hold_rows)}", readme)
    readme = re.sub(r"- Core US benchmark ETF day bars: .*", f"- Core US benchmark ETF day bars: {len(core_bars)}", readme)
    readme = re.sub(r"- Refreshed at `[^`]+`\.", f"- Refreshed at `{now}`.", readme)
    readme = re.sub(r"\n- 03/05 refreshed at `[^`]+` from router ref price tables\.\n?", "\n", readme)
    readme = readme.replace("- `03_market_data/buy_hold_cap_day_bars.json`: raw CAP OHLCV bars fetched for strategy target tickers.", "- `03_market_data/buy_hold_cap_day_bars.json`: refreshed target-ticker day bars for paper windows; current-window rows use paper-close fallback when router ref price tables lack 2026-05/06 coverage.")
    readme = readme.replace("- `03_market_data/core_us_benchmark_etf_day_bars.json`: full CAP OHLCV rows for `SPY`, `QQQ`, `DIA`, and `IWM`.", "- `03_market_data/core_us_benchmark_etf_day_bars.json`: router ref-price rows for `SPY`, `QQQ`, `DIA`, and `IWM` when available.")
    readme += f"\n- 03/05 refreshed at `{now}` from router ref price tables with paper-close fallback for current paper windows.\n"
    text_write("README.md", readme)

print(json.dumps({
    "buy_hold_market_bars": len(target_bars),
    "core_etf_bars": len(core_bars),
    "strategy_buy_hold_rows": len(buy_hold_rows),
    "strategy_buy_hold_warnings": len(buy_hold_warnings),
    "comparison_rows": len(comparison_rows),
    "both_benchmark_rows": len(both_rows),
    "final_rows": len(final_rows),
    "refreshed_at": now,
}, ensure_ascii=False, indent=2))
