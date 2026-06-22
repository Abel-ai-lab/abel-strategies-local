from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "07_backtest_trade_logs"
TRADE_LOG_DIR = OUT / "trade_logs"
DEFAULT_CHFS_CONFIG = ROOT / "config" / "chfs.sit.local.yaml"
DEFAULT_ROUTER_CONFIG = ROOT / "config" / "router.prod.local.yaml"
WRITE_GENERATED_READMES = os.environ.get("WRITE_GENERATED_READMES") == "1"
STRATEGIES_PATH = ROOT / "01_strategy_universe" / "strategies.json"
SELECTED_PORTFOLIO_PATHS = [
    ROOT / "06_portfolio_selection" / "stock_equal_weight_portfolio.csv",
    ROOT / "06_portfolio_selection" / "crypto_equal_weight_portfolio.csv",
]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_chfs_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"CHFS config does not exist: {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    chfs = cfg.get("chfs") or {}
    required = ["base_url", "username", "password"]
    missing = [key for key in required if not chfs.get(key)]
    if missing:
        raise SystemExit(f"CHFS config missing required keys: {', '.join(missing)}")
    return chfs


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


def selected_portfolio_strategy_ids() -> set[str]:
    strategy_ids: set[str] = set()
    for path in SELECTED_PORTFOLIO_PATHS:
        for row in read_csv_rows(path):
            strategy_id = str(row.get("strategy_id") or "").strip()
            if strategy_id:
                strategy_ids.add(strategy_id)
    return strategy_ids


def fetch_router_strategies(strategy_ids: set[str], config_path: Path) -> list[dict]:
    if not strategy_ids:
        return []
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
            select
              s.strategy_id,
              s.session_id,
              s.target_asset,
              s.asset_type,
              s.display_name,
              b.score as backtest_score,
              b.sharpe as backtest_sharpe,
              b.total_return as backtest_total_return,
              b.trade_log_uri as backtest_trade_log_uri
            from public.skill_dashboard_strategy s
            left join public.skill_dashboard_strategy_backtest b on b.strategy_id = s.strategy_id
            where s.strategy_id = any(%s)
            order by s.strategy_id
            """,
            (list(strategy_ids),),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def augment_with_selected_portfolio_strategies(strategies: list[dict], router_config: Path) -> list[dict]:
    seen = {str(strategy.get("strategy_id") or "") for strategy in strategies}
    extra_ids = selected_portfolio_strategy_ids() - seen
    extra = fetch_router_strategies(extra_ids, router_config)
    return strategies + extra


def efs_uri_to_chfs_path(uri: str) -> str:
    prefix = "efs://"
    if not uri.startswith(prefix):
        raise ValueError(f"unsupported URI scheme: {uri}")
    path = uri[len(prefix) :]
    parts = [quote(part, safe="") for part in path.split("/") if part]
    return "/" + "/".join(parts)


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def update_manifest(summary: dict) -> None:
    manifest_path = ROOT / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = read_json(manifest_path)
    counts = dict(manifest.get("counts", {}))
    counts.update({
        "backtestTradeLogFiles": summary["downloaded_files"],
        "backtestTradeLogMissing": summary["missing_files"],
        "backtestTradeLogRows": summary["downloaded_rows"],
        "backtestTradeLogStrategies": summary["strategy_count"],
    })
    manifest["counts"] = {key: counts[key] for key in sorted(counts)}

    now = summary["generated_at"]
    manifest["generatedAt"] = now
    manifest["refreshedAt"] = now
    manifest["organizedAt"] = datetime.now(timezone.utc).date().isoformat()

    files = manifest.setdefault("files", [])
    for file_name in [
        "07_backtest_trade_logs/README.md",
        "07_backtest_trade_logs/backtest_trade_log_index.csv",
        "07_backtest_trade_logs/backtest_trade_log_summary.json",
        "07_backtest_trade_logs/backtest_trade_log_warnings.json",
    ]:
        if file_name not in files:
            files.append(file_name)

    refreshed = manifest.setdefault("refreshedFolders", [])
    if "07_backtest_trade_logs" not in refreshed:
        refreshed.append("07_backtest_trade_logs")

    layout = manifest.setdefault("layout", [])
    if not any(entry.get("directory") == "07_backtest_trade_logs" for entry in layout):
        layout.append({
            "dependsOn": ["01_strategy_universe"],
            "description": "Backtest trade logs downloaded from CHFS using each strategy backtest_trade_log_uri.",
            "directory": "07_backtest_trade_logs",
        })

    source_note = "07 backtest trade logs refreshed from CHFS using ignored local config"
    source = manifest.get("source", "").replace("; 07 backtest trade logs refreshed from CHFS SIT using ignored local config", "")
    if source_note not in source:
        manifest["source"] = f"{source}; {source_note}" if source else source_note

    write_json(manifest_path, manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download backtest trade logs from CHFS for strategies in 01_strategy_universe.")
    parser.add_argument("--config", default=str(os.environ.get("CHFS_CONFIG") or DEFAULT_CHFS_CONFIG), help="Path to ignored CHFS local config")
    parser.add_argument("--router-config", default=str(os.environ.get("ROUTER_CONFIG") or DEFAULT_ROUTER_CONFIG), help="Path to router config used for 06 selected strategy metadata")
    parser.add_argument("--limit", type=int, default=0, help="Optional max strategies to process for smoke tests")
    parser.add_argument("--force", action="store_true", help="Re-download files that already exist")
    args = parser.parse_args()

    load_repo_env()
    chfs = load_chfs_config(Path(args.config))
    base_url = chfs["base_url"].rstrip("/")
    auth = (chfs["username"], chfs["password"])
    strategies = augment_with_selected_portfolio_strategies(read_json(STRATEGIES_PATH), Path(args.router_config))
    if args.limit > 0:
        strategies = strategies[: args.limit]

    OUT.mkdir(exist_ok=True)
    TRADE_LOG_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    index_rows = []
    warnings = []
    downloaded_rows = 0

    for strategy in strategies:
        strategy_id = str(strategy.get("strategy_id") or "")
        uri = strategy.get("backtest_trade_log_uri") or ""
        output_path = TRADE_LOG_DIR / f"{strategy_id}.csv"
        row = {
            "strategy_id": strategy_id,
            "session_id": strategy.get("session_id", ""),
            "target_asset": strategy.get("target_asset", ""),
            "asset_type": strategy.get("asset_type", ""),
            "display_name": strategy.get("display_name", ""),
            "backtest_score": strategy.get("backtest_score", ""),
            "backtest_sharpe": strategy.get("backtest_sharpe", ""),
            "backtest_total_return": strategy.get("backtest_total_return", ""),
            "backtest_trade_log_uri": uri,
            "local_path": str(output_path.relative_to(ROOT)).replace("\\", "/") if strategy_id else "",
            "status": "pending",
            "http_status": "",
            "bytes": "",
            "row_count": "",
        }
        if not strategy_id or not uri:
            row["status"] = "missing_uri"
            warnings.append({"strategy_id": strategy_id, "reason": row["status"], "uri": uri})
            index_rows.append(row)
            continue
        if output_path.exists() and not args.force:
            row_count = count_csv_rows(output_path)
            row.update({"status": "cached", "bytes": output_path.stat().st_size, "row_count": row_count})
            downloaded_rows += row_count
            index_rows.append(row)
            continue

        try:
            chfs_path = efs_uri_to_chfs_path(uri)
        except ValueError as exc:
            row["status"] = "invalid_uri"
            warnings.append({"strategy_id": strategy_id, "reason": str(exc), "uri": uri})
            index_rows.append(row)
            continue

        url = f"{base_url}/chfs/shared{chfs_path}"
        try:
            response = session.get(url, auth=auth, timeout=60)
        except requests.RequestException as exc:
            row["status"] = "request_error"
            warnings.append({"strategy_id": strategy_id, "reason": str(exc), "uri": uri})
            index_rows.append(row)
            continue

        row["http_status"] = response.status_code
        if response.status_code != 200:
            row["status"] = "missing"
            warnings.append({"strategy_id": strategy_id, "reason": "http_status", "status_code": response.status_code, "uri": uri})
            index_rows.append(row)
            continue
        if not response.content.startswith(b"date,"):
            row["status"] = "unexpected_content"
            warnings.append({"strategy_id": strategy_id, "reason": "unexpected_content", "uri": uri})
            index_rows.append(row)
            continue

        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp.write_bytes(response.content)
        os.replace(tmp, output_path)
        row_count = count_csv_rows(output_path)
        row.update({"status": "downloaded", "bytes": output_path.stat().st_size, "row_count": row_count})
        downloaded_rows += row_count
        index_rows.append(row)

    index_fields = [
        "strategy_id", "session_id", "target_asset", "asset_type", "display_name", "backtest_score",
        "backtest_sharpe", "backtest_total_return", "backtest_trade_log_uri", "local_path", "status",
        "http_status", "bytes", "row_count",
    ]
    write_csv(OUT / "backtest_trade_log_index.csv", index_fields, index_rows)
    write_json(OUT / "backtest_trade_log_warnings.json", warnings)

    generated_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "generated_at": generated_at,
        "source": "CHFS",
        "strategy_count": len(index_rows),
        "downloaded_files": sum(1 for row in index_rows if row["status"] in {"downloaded", "cached"}),
        "missing_files": sum(1 for row in index_rows if row["status"] not in {"downloaded", "cached"}),
        "downloaded_rows": downloaded_rows,
        "warnings": len(warnings),
        "output_dir": str(OUT),
    }
    write_json(OUT / "backtest_trade_log_summary.json", summary)

    readme = f"""# 07 Backtest Trade Logs

Generated at `{generated_at}` from CHFS using ignored local config `config/chfs.sit.local.yaml`.

## Source

- Strategy metadata: `01_strategy_universe/strategies.json`.
- URI field: `backtest_trade_log_uri`.
- URI mapping: `efs://...` to CHFS `/chfs/shared/...`.

## Counts

- Strategies processed: {summary['strategy_count']}
- Trade logs downloaded or cached: {summary['downloaded_files']}
- Missing/unavailable logs: {summary['missing_files']}
- Trade log data rows: {summary['downloaded_rows']}

## Files

- `trade_logs/<strategy_id>.csv`: downloaded trade log per strategy.
- `backtest_trade_log_index.csv`: strategy-to-local-file index with status and row counts.
- `backtest_trade_log_summary.json`: aggregate counts.
- `backtest_trade_log_warnings.json`: missing/unavailable URI details.
"""
    if WRITE_GENERATED_READMES:
        (OUT / "README.md").write_text(readme, encoding="utf-8")
    update_manifest(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
