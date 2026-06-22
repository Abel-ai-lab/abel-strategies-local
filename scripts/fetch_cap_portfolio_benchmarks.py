from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests


ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ["QQQ", "BTCUSD"]
DEFAULT_BASE_URLS = [
    "https://cap-sit.abel.ai/api",
    "https://cap.abel.ai/api",
]


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


def read_json(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def read_json_optional(path: str, default):
    p = ROOT / path
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def json_write(path: str, data) -> None:
    p = ROOT / path
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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


def infer_window() -> tuple[str, str]:
    rows = read_json("03_market_data/buy_hold_cap_day_bars.json")
    dates = [row["trading_date"] for row in rows if row.get("trading_date")]
    if not dates:
        rows = read_json("02_paper_actuals/paper_daily_rows.json")
        dates = [row["trading_date"] for row in rows if row.get("trading_date")]
    if not dates:
        raise SystemExit("Cannot infer benchmark date window")
    return min(dates), max(dates)


def normalize_row(symbol: str, row: dict) -> dict | None:
    trading_date = row.get("trading_date") or row.get("date") or row.get("day")
    timestamp = row.get("timestamp") or row.get("time") or row.get("datetime")
    if trading_date and "T" in str(trading_date):
        trading_date = str(trading_date)[:10]
    if not trading_date and timestamp:
        trading_date = str(timestamp)[:10]
    close = row.get("close") or row.get("c")
    if not trading_date or close is None:
        return None
    return {
        "ticker": row.get("ticker") or row.get("symbol") or symbol,
        "trading_date": str(trading_date),
        "timestamp": timestamp or f"{trading_date}T00:00:00Z",
        "open": row.get("open", row.get("o", close)),
        "high": row.get("high", row.get("h", close)),
        "low": row.get("low", row.get("l", close)),
        "close": close,
        "volume": row.get("volume", row.get("v")),
        "source": "cap_market_day_bar",
    }


def extract_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "rows", "items", "result", "bars"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_rows(value)
            if nested:
                return nested
    return []


def request_attempts(base_url: str, symbol: str, start_date: str, end_date: str, api_key: str):
    url = urljoin(base_url.rstrip("/") + "/", "market/day_bar")
    valid_payload = {
        "symbols": [symbol],
        "timeframe": "1d",
        "limit": 5000,
        "fields": ["open", "high", "low", "close", "volume"],
        "start_date": start_date,
        "end_date": end_date,
    }
    yield "POST", url, None, valid_payload, {"Authorization": f"Bearer {api_key}"}
    payload_variants = [
        {"ticker": symbol, "start_date": start_date, "end_date": end_date},
        {"symbol": symbol, "start_date": start_date, "end_date": end_date},
        {"symbols": symbol, "start_date": start_date, "end_date": end_date},
        {"symbols": [symbol], "start_date": start_date, "end_date": end_date},
        {"ticker": symbol, "start": start_date, "end": end_date},
        {"symbol": symbol, "start": start_date, "end": end_date},
    ]
    headers_variants = [
        {"Authorization": f"Bearer {api_key}"},
        {"X-API-Key": api_key},
        {"x-api-key": api_key},
        {"Api-Key": api_key},
    ]
    for params in payload_variants:
        for headers in headers_variants:
            yield "GET", url, params, None, headers
            yield "POST", url, None, params, headers
            yield "POST_FORM", url, None, params, headers
        with_key = dict(params)
        with_key["api_key"] = api_key
        yield "GET", url, with_key, None, {}


def fetch_symbol(symbol: str, start_date: str, end_date: str, api_key: str, base_urls: list[str]) -> tuple[list[dict], list[dict]]:
    warnings = []
    session = requests.Session()
    for base_url in base_urls:
        for method, url, params, json_body, headers in request_attempts(base_url, symbol, start_date, end_date, api_key):
            raw_payload = params if params is not None else json_body if json_body is not None else {}
            safe_params = {k: ("***" if "key" in k.lower() else v) for k, v in raw_payload.items()}
            safe_headers = {k: "***" for k in headers}
            try:
                if method == "GET":
                    response = session.get(url, params=params, headers=headers, timeout=30)
                elif method == "POST_FORM":
                    response = session.post(url, data=json_body, headers=headers, timeout=30)
                else:
                    response = session.post(url, json=json_body, headers=headers, timeout=30)
            except requests.RequestException as exc:
                warnings.append({"symbol": symbol, "base_url": base_url, "method": method, "params": safe_params, "headers": safe_headers, "error": str(exc)})
                continue
            if response.status_code >= 400:
                warnings.append({"symbol": symbol, "base_url": base_url, "method": method, "params": safe_params, "headers": safe_headers, "status_code": response.status_code, "body_preview": response.text[:300]})
                continue
            try:
                payload = response.json()
            except ValueError:
                warnings.append({"symbol": symbol, "base_url": base_url, "method": method, "params": safe_params, "headers": safe_headers, "status_code": response.status_code, "body_preview": response.text[:300]})
                continue
            rows = [r for r in (normalize_row(symbol, item) for item in extract_rows(payload)) if r]
            rows = [r for r in rows if start_date <= r["trading_date"] <= end_date]
            if rows:
                rows.sort(key=lambda r: r["trading_date"])
                return rows, warnings
            warnings.append({"symbol": symbol, "base_url": base_url, "method": method, "params": safe_params, "headers": safe_headers, "status_code": response.status_code, "reason": "response had no parsable rows", "body_preview": response.text[:300]})
    return [], warnings


def summarize(symbol: str, rows: list[dict], start_date: str, end_date: str) -> dict:
    if not rows:
        return {"symbol": symbol, "row_count": 0, "requested_start_date": start_date, "requested_end_date": end_date, "warning": True}
    first = rows[0]
    last = rows[-1]
    first_close = float(first["close"])
    last_close = float(last["close"])
    return {
        "symbol": symbol,
        "row_count": len(rows),
        "requested_start_date": start_date,
        "requested_end_date": end_date,
        "first_trading_date": first["trading_date"],
        "last_trading_date": last["trading_date"],
        "first_close": first_close,
        "last_close": last_close,
        "total_return": last_close / first_close - 1.0 if first_close else "",
        "warning": False,
    }


def main() -> None:
    load_repo_env()
    api_key = os.environ.get("CAP_API_KEY")
    if not api_key:
        raise SystemExit("CAP_API_KEY environment variable is required")
    base_urls = [os.environ.get("ABEL_CAP_BASE_URL") or os.environ.get("CAP_BASE_URL") or ""]
    base_urls = [url for url in base_urls if url] or DEFAULT_BASE_URLS
    start_date = os.environ.get("BENCHMARK_START_DATE")
    end_date = os.environ.get("BENCHMARK_END_DATE")
    if not start_date or not end_date:
        start_date, end_date = infer_window()

    all_rows = []
    all_warnings = []
    summaries = []
    for symbol in SYMBOLS:
        rows, warnings = fetch_symbol(symbol, start_date, end_date, api_key, base_urls)
        all_rows.extend(rows)
        all_warnings.extend(warnings)
        if not rows:
            all_warnings.append({"symbol": symbol, "reason": "no benchmark rows fetched", "requested_start_date": start_date, "requested_end_date": end_date})
        summaries.append(summarize(symbol, rows, start_date, end_date))

    all_rows.sort(key=lambda r: (r["ticker"], r["trading_date"]))
    json_write("03_market_data/portfolio_benchmark_day_bars.json", all_rows)
    csv_write("03_market_data/portfolio_benchmark_day_bars.csv", ["ticker", "trading_date", "timestamp", "open", "high", "low", "close", "volume", "source"], all_rows)
    csv_write("03_market_data/portfolio_benchmark_summary.csv", ["symbol", "row_count", "requested_start_date", "requested_end_date", "first_trading_date", "last_trading_date", "first_close", "last_close", "total_return", "warning"], summaries)
    json_write("03_market_data/portfolio_benchmark_warnings.json", all_warnings)

    manifest = read_json_optional("manifest.json", {})
    counts = dict(manifest.get("counts", {}))
    counts["portfolioBenchmarkDayBars"] = len(all_rows)
    counts["portfolioBenchmarkWarnings"] = len(all_warnings)
    manifest["counts"] = {k: counts[k] for k in sorted(counts)}
    now = datetime.now(timezone.utc).isoformat()
    manifest["generatedAt"] = now
    manifest["refreshedAt"] = now
    source = manifest.get("source", "")
    source = re.sub(r"( with paper-close fallback)+", "", source)
    source = source.replace("; 03/05 refreshed from router ref_stock_price/ref_crypto_price", "; 03/05 refreshed from router ref_stock_price/ref_crypto_price with paper-close fallback")
    source = source.replace("; portfolio benchmarks refreshed from CAP market/day_bar", "")
    manifest["source"] = source + "; portfolio benchmarks refreshed from CAP market/day_bar"
    if "03_market_data/portfolio_benchmark_day_bars.json" not in manifest.get("files", []):
        manifest.setdefault("files", []).extend([
            "03_market_data/portfolio_benchmark_day_bars.json",
            "03_market_data/portfolio_benchmark_day_bars.csv",
            "03_market_data/portfolio_benchmark_summary.csv",
            "03_market_data/portfolio_benchmark_warnings.json",
        ])
    json_write("manifest.json", manifest)

    print(json.dumps({
        "requested_start_date": start_date,
        "requested_end_date": end_date,
        "rows": len(all_rows),
        "warnings": len(all_warnings),
        "summary": summaries,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
