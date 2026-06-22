from __future__ import annotations

import csv
import json
import os
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
EXTRA_STRATEGY_COUNT = int(os.environ.get("EXTRA_STRATEGY_COUNT", "0") or "0")
BASE_STRATEGY_IDS_PATH = os.environ.get("BASE_STRATEGY_IDS_PATH")
EXTRA_TARGET_ASSETS = [s.strip().upper() for s in os.environ.get("EXTRA_TARGET_ASSETS", "").split(",") if s.strip()]
WRITE_GENERATED_READMES = os.environ.get("WRITE_GENERATED_READMES") == "1"
USER_ID = 318274928728084480
USER_ID_STR = str(USER_ID)
CORE_ETFS = ["SPY", "QQQ", "DIA", "IWM"]

ID_KEYS = {
    "strategy_id", "session_id", "owner_user_id", "subscription_id", "user_id",
    "run_id", "generated_by_run_id", "previous_run_id", "paired_strategy_run_id",
    "source_llm_run_id", "api_company_config_id", "create_user", "modify_user",
}


def normalize_value(value):
    if isinstance(value, Decimal):
        if value.is_nan():
            return None
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_value(v) for v in value]
    if isinstance(value, tuple):
        return [normalize_value(v) for v in value]
    return value


def export_row(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if value is None:
            out[key] = None
        elif key in ID_KEYS:
            out[key] = str(value)
        else:
            out[key] = normalize_value(value)
    return out


def safe_float(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def json_write(path: str, data):
    p = ROOT / path
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def text_write(path: str, text: str):
    p = ROOT / path
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, p)


def csv_write(path: str, fieldnames: list[str], rows: list[dict]):
    p = ROOT / path
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    os.replace(tmp, p)


def first_non_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def read_json_count(path):
    return len(json.loads((ROOT / path).read_text(encoding="utf-8")))


def csv_data_count(path):
    with (ROOT / path).open("r", encoding="utf-8", newline="") as f:
        return max(sum(1 for _ in f) - 1, 0)


def load_selection_symbols(path: str | None) -> list[str]:
    if not path:
        return []
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        rows = csv.DictReader(f)
        symbols = {row["symbol"].strip() for row in rows if row.get("symbol") and row["symbol"].strip()}
    return sorted(symbols)


def load_strategy_ids(path: str | None) -> list[int]:
    if not path:
        return []
    p = Path(path)
    if p.suffix.lower() == ".csv":
        with p.open("r", encoding="utf-8", newline="") as f:
            rows = csv.DictReader(f)
            return [int(row["strategy_id"]) for row in rows if row.get("strategy_id")]
    return [int(line.strip()) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
selection_symbols = load_selection_symbols(SELECTION_CSV)
base_strategy_ids = load_strategy_ids(BASE_STRATEGY_IDS_PATH)
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
    strategy_filter_sql = ""
    strategy_params = [USER_ID]
    if base_strategy_ids:
        strategy_filter_sql = "\n          and s.strategy_id = any(%s)"
        strategy_params.append(base_strategy_ids)
    elif selection_symbols:
        strategy_filter_sql = "\n          and s.target_asset = any(%s)"
        strategy_params.append(selection_symbols)

    strategy_select_sql = f"""
        select
          s.asset_type,
          b.created_at as backtest_created_at,
          b.dsr as backtest_dsr,
          b.k as backtest_k,
          b.lo_adjusted as backtest_lo_adjusted,
          b.loss_years as backtest_loss_years,
          b.max_dd as backtest_max_dd,
          b.omega as backtest_omega,
          b.position_hit_rate as backtest_position_hit_rate,
          b.position_ic as backtest_position_ic,
          b.position_ic_stability as backtest_position_ic_stability,
          b.score as backtest_score,
          b.sharpe as backtest_sharpe,
          b.total_return as backtest_total_return,
          b.trade_log_uri as backtest_trade_log_uri,
          b.updated_at as backtest_updated_at,
          b.verdict as backtest_verdict,
          s.created_at,
          s.cron_expr,
          s.display_name,
          s.is_active,
          s.is_session_primary,
          s.last_run_at,
          s.last_run_status,
          s.next_run_at,
          s.owner_user_id,
          s.profile,
          s.required_symbols,
          s.selection,
          s.selection_rank,
          s.selection_reason,
          s.session_id,
          s.status,
          s.strategy_id,
          s.target_asset,
          s.timeframe,
          s.updated_at,
          s.visibility
        from public.skill_dashboard_strategy s
        left join public.skill_dashboard_strategy_backtest b on b.strategy_id = s.strategy_id
        where s.owner_user_id = %s
          and s.status = 'ready'
          and s.is_active = true{strategy_filter_sql}
        order by s.created_at desc, s.strategy_id desc
        """
    cur.execute(strategy_select_sql, tuple(strategy_params))
    strategy_rows_raw = cur.fetchall()

    if EXTRA_STRATEGY_COUNT > 0:
        seen_targets = {row["target_asset"] for row in strategy_rows_raw if row.get("target_asset")}
        seen_strategy_ids = {row["strategy_id"] for row in strategy_rows_raw}
        cur.execute(
            """
            select
              s.asset_type,
              b.created_at as backtest_created_at,
              b.dsr as backtest_dsr,
              b.k as backtest_k,
              b.lo_adjusted as backtest_lo_adjusted,
              b.loss_years as backtest_loss_years,
              b.max_dd as backtest_max_dd,
              b.omega as backtest_omega,
              b.position_hit_rate as backtest_position_hit_rate,
              b.position_ic as backtest_position_ic,
              b.position_ic_stability as backtest_position_ic_stability,
              b.score as backtest_score,
              b.sharpe as backtest_sharpe,
              b.total_return as backtest_total_return,
              b.trade_log_uri as backtest_trade_log_uri,
              b.updated_at as backtest_updated_at,
              b.verdict as backtest_verdict,
              s.created_at,
              s.cron_expr,
              s.display_name,
              s.is_active,
              s.is_session_primary,
              s.last_run_at,
              s.last_run_status,
              s.next_run_at,
              s.owner_user_id,
              s.profile,
              s.required_symbols,
              s.selection,
              s.selection_rank,
              s.selection_reason,
              s.session_id,
              s.status,
              s.strategy_id,
              s.target_asset,
              s.timeframe,
              s.updated_at,
              s.visibility
            from public.skill_dashboard_strategy s
            left join public.skill_dashboard_strategy_backtest b on b.strategy_id = s.strategy_id
            where s.owner_user_id = %s
              and s.status = 'ready'
              and s.is_active = true
              and s.target_asset is not null
            order by s.created_at desc, s.strategy_id desc
            """,
            (USER_ID,),
        )
        extra_rows = []
        for row in cur.fetchall():
            target = row.get("target_asset")
            if row["strategy_id"] in seen_strategy_ids or not target or target in seen_targets:
                continue
            extra_rows.append(row)
            seen_strategy_ids.add(row["strategy_id"])
            seen_targets.add(target)
            if len(extra_rows) == EXTRA_STRATEGY_COUNT:
                break
        if len(extra_rows) != EXTRA_STRATEGY_COUNT:
            raise RuntimeError(f"requested {EXTRA_STRATEGY_COUNT} extra strategies, found {len(extra_rows)}")
        strategy_rows_raw.extend(extra_rows)

    if EXTRA_TARGET_ASSETS:
        seen_targets = {str(row["target_asset"]).strip().upper() for row in strategy_rows_raw if row.get("target_asset")}
        seen_strategy_ids = {row["strategy_id"] for row in strategy_rows_raw}
        needed_targets = [target for target in EXTRA_TARGET_ASSETS if target not in seen_targets]
        exact_extra_rows = []
        if needed_targets:
            cur.execute(
                """
                select
                  s.asset_type,
                  b.created_at as backtest_created_at,
                  b.dsr as backtest_dsr,
                  b.k as backtest_k,
                  b.lo_adjusted as backtest_lo_adjusted,
                  b.loss_years as backtest_loss_years,
                  b.max_dd as backtest_max_dd,
                  b.omega as backtest_omega,
                  b.position_hit_rate as backtest_position_hit_rate,
                  b.position_ic as backtest_position_ic,
                  b.position_ic_stability as backtest_position_ic_stability,
                  b.score as backtest_score,
                  b.sharpe as backtest_sharpe,
                  b.total_return as backtest_total_return,
                  b.trade_log_uri as backtest_trade_log_uri,
                  b.updated_at as backtest_updated_at,
                  b.verdict as backtest_verdict,
                  s.created_at,
                  s.cron_expr,
                  s.display_name,
                  s.is_active,
                  s.is_session_primary,
                  s.last_run_at,
                  s.last_run_status,
                  s.next_run_at,
                  s.owner_user_id,
                  s.profile,
                  s.required_symbols,
                  s.selection,
                  s.selection_rank,
                  s.selection_reason,
                  s.session_id,
                  s.status,
                  s.strategy_id,
                  s.target_asset,
                  s.timeframe,
                  s.updated_at,
                  s.visibility
                from public.skill_dashboard_strategy s
                left join public.skill_dashboard_strategy_backtest b on b.strategy_id = s.strategy_id
                where s.owner_user_id = %s
                  and s.status = 'ready'
                  and s.is_active = true
                  and upper(s.target_asset) = any(%s)
                order by s.created_at desc, s.strategy_id desc
                """,
                (USER_ID, needed_targets),
            )
            added_targets = set()
            for row in cur.fetchall():
                target = str(row.get("target_asset") or "").strip().upper()
                if not target or target in seen_targets or target in added_targets or row["strategy_id"] in seen_strategy_ids:
                    continue
                exact_extra_rows.append(row)
                added_targets.add(target)
                seen_targets.add(target)
                seen_strategy_ids.add(row["strategy_id"])
            missing_targets = sorted(set(needed_targets) - added_targets)
            if missing_targets:
                raise RuntimeError(f"requested exact extra target assets not found: {', '.join(missing_targets)}")
            strategy_rows_raw.extend(exact_extra_rows)
    strategy_ids_int = [r["strategy_id"] for r in strategy_rows_raw]

    def fetch_table(sql):
        cur.execute(sql, (strategy_ids_int,))
        return cur.fetchall()

    subscription_rows_raw = fetch_table(
        """
        select created_at, custom_display_name, read_status, status, strategy_id, subscription_id, updated_at, user_id
        from public.skill_dashboard_paper_subscription
        where strategy_id = any(%s)
        order by created_at asc, subscription_id asc
        """
    )
    daily_rows_raw = fetch_table(
        """
        select asset_return, benchmark_return, close, created_at, generated_by_run_id, next_position, pnl,
               position, signal_payload, strategy_id, trading_date, updated_at
        from public.skill_dashboard_paper_strategy_daily_row
        where strategy_id = any(%s)
        order by strategy_id asc, trading_date asc
        """
    )
    run_rows_raw = fetch_table(
        """
        select attempt_no, created_at, data_status, error_code, error_message, finished_at, is_canonical,
               logs_uri, previous_run_id, result_data, run_id, run_kind, started_at, status,
               strategy_id, trading_date, updated_at, worker_id
        from public.skill_dashboard_paper_strategy_run
        where strategy_id = any(%s)
        order by strategy_id asc, trading_date asc, run_id asc
        """
    )
    baseline_rows_raw = fetch_table(
        """
        select backtest_end_date, backtest_first_close, backtest_last_asset_value, backtest_last_close,
               backtest_last_strategy_value, backtest_start_date, computed_at, created_at, paper_start_at,
               paper_start_policy, paper_start_trading_date, source_row_count, source_sha256, source_uri,
               strategy_id, updated_at
        from public.skill_dashboard_strategy_performance_baseline
        where strategy_id = any(%s)
        order by strategy_id asc
        """
    )
    point_rows_raw = fetch_table(
        """
        select chart_asset_value, chart_strategy_value, computed_at, created_at, generated_by_run_id,
               paper_asset_cum_return, paper_day_index, paper_strategy_cum_return,
               strategy_id, trading_date, updated_at
        from public.skill_dashboard_paper_strategy_performance_point
        where strategy_id = any(%s)
        order by strategy_id asc, trading_date asc
        """
    )

strategy_rows = [export_row(r) for r in strategy_rows_raw]
subscription_rows = [export_row(r) for r in subscription_rows_raw]
daily_rows = [export_row(r) for r in daily_rows_raw]
run_rows = [export_row(r) for r in run_rows_raw]
baseline_rows = [export_row(r) for r in baseline_rows_raw]
point_rows = [export_row(r) for r in point_rows_raw]

strategy_by_id = {str(r["strategy_id"]): r for r in strategy_rows_raw}
paper_rows_by_sid = defaultdict(list)
paper_by_sid_date = {}
for row in daily_rows_raw:
    sid = str(row["strategy_id"])
    paper_rows_by_sid[sid].append(row)
    paper_by_sid_date[(sid, row["trading_date"])] = row

paper_window = {}
latest_paper = {}
for sid, rows in paper_rows_by_sid.items():
    rows_sorted = sorted(rows, key=lambda r: r["trading_date"])
    paper_window[sid] = {"from": rows_sorted[0]["trading_date"], "to": rows_sorted[-1]["trading_date"], "count": len(rows_sorted)}
    latest_paper[sid] = rows_sorted[-1]

subscription_by_sid = {}
for row in subscription_rows_raw:
    subscription_by_sid.setdefault(str(row["strategy_id"]), row)

baseline_by_sid = {str(r["strategy_id"]): r for r in baseline_rows_raw}
point_by_sid_date = {(str(r["strategy_id"]), r["trading_date"]): r for r in point_rows_raw}

strategy_tickers = []
target_tickers_set = set()
all_tickers_set = set()
for s_raw in strategy_rows_raw:
    sid = str(s_raw["strategy_id"])
    target = (s_raw.get("target_asset") or "").strip()
    required = s_raw.get("required_symbols") or []
    if not isinstance(required, list):
        required = []
    required_set = {str(x).strip() for x in required if str(x).strip()}
    tickers = set(required_set)
    if target:
        target_tickers_set.add(target)
        tickers.add(target)
    all_tickers_set.update(tickers)
    for ticker in sorted(tickers):
        roles = []
        if ticker in required_set:
            roles.append("required")
        if target and ticker == target:
            roles.append("target")
        w = paper_window.get(sid)
        strategy_tickers.append({
            "display_name": s_raw.get("display_name"),
            "paper_daily_rows_count": w["count"] if w else 0,
            "paper_from_date": normalize_value(w["from"]) if w else None,
            "paper_to_date": normalize_value(w["to"]) if w else None,
            "roles": roles,
            "strategy_id": sid,
            "target_asset": target or None,
            "ticker": ticker,
        })

target_tickers = sorted(target_tickers_set)
all_strategy_tickers = sorted(all_tickers_set)

all_llm_decision_rows_raw = []
if paper_window and all_strategy_tickers:
    min_paper_date = min(w["from"] for w in paper_window.values())
    max_paper_date = max(w["to"] for w in paper_window.values())
    with conn.cursor() as cur:
        cur.execute(
            """
            select d.confidence, d.previous_target_exposure, r.prompt_version, d.risk,
                   r.run_config_hash, d.run_id, d.sources, d.target_exposure, d.thesis,
                   d.ticker, r.trading_date
            from public.llm_paper_trade_decision d
            join public.llm_paper_trade_run r on r.run_id = d.run_id
            where d.ticker = any(%s)
              and r.trading_date between %s and %s
              and r.status = 'succeeded'
            order by d.ticker asc, r.trading_date asc, d.run_id asc, d.decision_order asc
            """,
            (all_strategy_tickers, min_paper_date, max_paper_date),
        )
        all_llm_decision_rows_raw = cur.fetchall()

conn.close()

all_decisions_by_ticker = defaultdict(list)
for row in all_llm_decision_rows_raw:
    all_decisions_by_ticker[row["ticker"]].append(row)

target_windows_by_ticker = defaultdict(list)
for sid, s_raw in strategy_by_id.items():
    ticker = s_raw.get("target_asset")
    w = paper_window.get(sid)
    if ticker and w:
        target_windows_by_ticker[ticker].append((w["from"], w["to"]))

target_decision_keys = set()
target_decision_rows = []
for ticker in sorted(target_windows_by_ticker):
    windows = target_windows_by_ticker[ticker]
    for d in all_decisions_by_ticker.get(ticker, []):
        dt = d["trading_date"]
        if any(start <= dt <= end for start, end in windows):
            key = (str(d["run_id"]), ticker, dt)
            if key not in target_decision_keys:
                target_decision_keys.add(key)
                target_decision_rows.append(export_row(d))
target_decision_rows.sort(key=lambda r: (r["ticker"], r["trading_date"], r["run_id"]))

strategy_ticker_decision_rows = []
strategy_ticker_summary_rows = []
llm_decision_count_by_strategy_target = defaultdict(int)
for mapping in strategy_tickers:
    sid = mapping["strategy_id"]
    ticker = mapping["ticker"]
    w = paper_window.get(sid)
    matched = []
    if w:
        for d in all_decisions_by_ticker.get(ticker, []):
            if w["from"] <= d["trading_date"] <= w["to"]:
                item = export_row(d)
                item["paper_from_date"] = normalize_value(w["from"])
                item["paper_to_date"] = normalize_value(w["to"])
                item["strategy_id"] = sid
                matched.append(item)
    matched.sort(key=lambda r: (r["trading_date"], r["run_id"]))
    strategy_ticker_decision_rows.extend(matched)
    dates = [r["trading_date"] for r in matched]
    strategy_ticker_summary_rows.append({
        "strategy_id": sid,
        "display_name": mapping["display_name"],
        "target_asset": mapping["target_asset"],
        "ticker": ticker,
        "roles": "|".join(mapping["roles"]),
        "paper_from_date": mapping["paper_from_date"] or "",
        "paper_to_date": mapping["paper_to_date"] or "",
        "paper_daily_rows_count": mapping["paper_daily_rows_count"],
        "llm_decision_count": len(matched),
        "first_llm_decision_date": dates[0] if dates else "",
        "last_llm_decision_date": dates[-1] if dates else "",
    })

llm_benchmark_rows = []
llm_warnings = []
llm_rows_count_by_strategy = defaultdict(int)


def decision_payload(decision):
    return {
        "confidence": normalize_value(decision.get("confidence")),
        "previousTargetExposure": normalize_value(decision.get("previous_target_exposure")),
        "risk": normalize_value(decision.get("risk")),
        "sources": normalize_value(decision.get("sources") or []),
        "targetExposure": normalize_value(decision.get("target_exposure")),
        "thesis": normalize_value(decision.get("thesis")),
    }


for s_raw in strategy_rows_raw:
    sid = str(s_raw["strategy_id"])
    ticker = s_raw.get("target_asset")
    w = paper_window.get(sid)
    baseline = baseline_by_sid.get(sid)
    if not ticker or not w or not baseline:
        llm_warnings.append({"reason": "missing ticker, paper daily rows, or performance baseline", "strategy_id": sid, "ticker": ticker})
        continue
    decisions = [d for d in all_decisions_by_ticker.get(ticker, []) if w["from"] <= d["trading_date"] <= w["to"]]
    decisions.sort(key=lambda d: (d["trading_date"], d["run_id"]))
    llm_decision_count_by_strategy_target[sid] = len(decisions)
    if not decisions:
        llm_warnings.append({"reason": "no LLM decision on paper dates", "strategy_id": sid, "ticker": ticker})
        continue
    first_decision = decisions[0]
    first_date = first_decision["trading_date"]
    prior_paper_dates = [r["trading_date"] for r in paper_rows_by_sid[sid] if r["trading_date"] < first_date]
    anchor_date = max(prior_paper_dates) if prior_paper_dates else baseline.get("backtest_end_date")
    anchor_point = point_by_sid_date.get((sid, anchor_date)) if anchor_date else None
    anchor_value = first_non_none(anchor_point.get("chart_strategy_value") if anchor_point else None, baseline.get("backtest_last_strategy_value"))
    if anchor_date is None or anchor_value is None:
        llm_warnings.append({"reason": "missing benchmark anchor date or value", "strategy_id": sid, "ticker": ticker})
        continue
    anchor_value_float = safe_float(anchor_value)
    current_position = safe_float(first_decision.get("previous_target_exposure"))
    anchor_date_export = normalize_value(anchor_date)
    llm_benchmark_rows.append({
        "anchor_chart_strategy_value": anchor_value_float,
        "anchor_trading_date": anchor_date_export,
        "asset_return": 0.0,
        "benchmark_chart_value": anchor_value_float,
        "benchmark_cum_return": 0.0,
        "benchmark_kind": "llm",
        "benchmark_return": 0.0,
        "close": None,
        "next_position": current_position,
        "paired_strategy_run_id": None,
        "position": current_position,
        "signal_payload": {},
        "source_llm_prompt_version": None,
        "source_llm_run_config_hash": None,
        "source_llm_run_id": None,
        "strategy_id": sid,
        "ticker": ticker,
        "trading_date": anchor_date_export,
    })
    llm_rows_count_by_strategy[sid] += 1
    cumulative_factor = 1.0
    seen_dates = set()
    for decision in decisions:
        trading_date = decision["trading_date"]
        if trading_date in seen_dates:
            continue
        paper = paper_by_sid_date.get((sid, trading_date))
        if not paper:
            continue
        asset_return = safe_float(paper.get("asset_return"))
        benchmark_return = current_position * asset_return
        cumulative_factor *= 1.0 + benchmark_return
        next_position = safe_float(decision.get("target_exposure"))
        llm_benchmark_rows.append({
            "anchor_chart_strategy_value": anchor_value_float,
            "anchor_trading_date": anchor_date_export,
            "asset_return": asset_return,
            "benchmark_chart_value": anchor_value_float * cumulative_factor,
            "benchmark_cum_return": cumulative_factor - 1.0,
            "benchmark_kind": "llm",
            "benchmark_return": benchmark_return,
            "close": normalize_value(paper.get("close")),
            "next_position": next_position,
            "paired_strategy_run_id": str(paper["generated_by_run_id"]) if paper.get("generated_by_run_id") is not None else None,
            "position": current_position,
            "signal_payload": {"decision": decision_payload(decision)},
            "source_llm_prompt_version": normalize_value(decision.get("prompt_version")),
            "source_llm_run_config_hash": normalize_value(decision.get("run_config_hash")),
            "source_llm_run_id": str(decision["run_id"]) if decision.get("run_id") is not None else None,
            "strategy_id": sid,
            "ticker": ticker,
            "trading_date": normalize_value(trading_date),
        })
        llm_rows_count_by_strategy[sid] += 1
        current_position = next_position
        seen_dates.add(trading_date)

strategy_summary_fields = [
    "strategy_id", "session_id", "owner_user_id", "display_name", "target_asset", "asset_type", "profile", "timeframe",
    "required_symbols", "strategy_status", "is_active", "is_session_primary", "selection_rank", "created_at",
    "subscription_id", "subscription_status", "latest_paper_trading_date", "latest_paper_position",
    "latest_paper_next_position", "latest_paper_pnl", "latest_paper_benchmark_return", "paper_daily_rows_count",
    "llm_decisions_count_for_ticker", "llm_benchmark_rows_count",
]
strategy_summary_rows = []
for s_raw in strategy_rows_raw:
    sid = str(s_raw["strategy_id"])
    sub = subscription_by_sid.get(sid)
    latest = latest_paper.get(sid)
    w = paper_window.get(sid)
    strategy_summary_rows.append({
        "strategy_id": sid,
        "session_id": str(s_raw["session_id"]) if s_raw.get("session_id") is not None else "",
        "owner_user_id": str(s_raw["owner_user_id"]) if s_raw.get("owner_user_id") is not None else "",
        "display_name": s_raw.get("display_name"),
        "target_asset": s_raw.get("target_asset"),
        "asset_type": s_raw.get("asset_type"),
        "profile": s_raw.get("profile"),
        "timeframe": s_raw.get("timeframe"),
        "required_symbols": "|".join(str(x) for x in (s_raw.get("required_symbols") or [])),
        "strategy_status": s_raw.get("status"),
        "is_active": s_raw.get("is_active"),
        "is_session_primary": s_raw.get("is_session_primary"),
        "selection_rank": s_raw.get("selection_rank"),
        "created_at": normalize_value(s_raw.get("created_at")),
        "subscription_id": str(sub["subscription_id"]) if sub else "",
        "subscription_status": sub["status"] if sub else "",
        "latest_paper_trading_date": normalize_value(latest["trading_date"]) if latest else "",
        "latest_paper_position": normalize_value(latest["position"]) if latest else "",
        "latest_paper_next_position": normalize_value(latest["next_position"]) if latest else "",
        "latest_paper_pnl": normalize_value(latest["pnl"]) if latest else "",
        "latest_paper_benchmark_return": normalize_value(latest["benchmark_return"]) if latest else "",
        "paper_daily_rows_count": w["count"] if w else 0,
        "llm_decisions_count_for_ticker": llm_decision_count_by_strategy_target.get(sid, 0),
        "llm_benchmark_rows_count": llm_rows_count_by_strategy.get(sid, 0),
    })

ticker_summary_fields = [
    "ticker", "strategy_count", "strategy_ids", "first_paper_trading_date", "last_paper_trading_date",
    "llm_decision_count", "first_llm_decision_date", "last_llm_decision_date", "llm_benchmark_rows_count",
]
strategies_by_target = defaultdict(list)
for s_raw in strategy_rows_raw:
    if s_raw.get("target_asset"):
        strategies_by_target[s_raw["target_asset"]].append(str(s_raw["strategy_id"]))

ticker_summary_rows = []
for ticker in target_tickers:
    sids = strategies_by_target[ticker]
    paper_dates = []
    llm_count = 0
    llm_dates = []
    llm_rows_count = 0
    for sid in sids:
        w = paper_window.get(sid)
        if w:
            paper_dates.extend([w["from"], w["to"]])
            dates = [d["trading_date"] for d in all_decisions_by_ticker.get(ticker, []) if w["from"] <= d["trading_date"] <= w["to"]]
            llm_dates.extend(dates)
        llm_count += llm_decision_count_by_strategy_target.get(sid, 0)
        llm_rows_count += llm_rows_count_by_strategy.get(sid, 0)
    ticker_summary_rows.append({
        "ticker": ticker,
        "strategy_count": len(sids),
        "strategy_ids": "|".join(sids),
        "first_paper_trading_date": normalize_value(min(paper_dates)) if paper_dates else "",
        "last_paper_trading_date": normalize_value(max(paper_dates)) if paper_dates else "",
        "llm_decision_count": llm_count,
        "first_llm_decision_date": normalize_value(min(llm_dates)) if llm_dates else "",
        "last_llm_decision_date": normalize_value(max(llm_dates)) if llm_dates else "",
        "llm_benchmark_rows_count": llm_rows_count,
    })

strategy_ticker_summary_fields = [
    "strategy_id", "display_name", "target_asset", "ticker", "roles", "paper_from_date", "paper_to_date",
    "paper_daily_rows_count", "llm_decision_count", "first_llm_decision_date", "last_llm_decision_date",
]

json_write("01_strategy_universe/strategies.json", strategy_rows)
csv_write("01_strategy_universe/strategy_summary.csv", strategy_summary_fields, strategy_summary_rows)
text_write("01_strategy_universe/tickers.txt", "\n".join(target_tickers) + "\n")
csv_write("01_strategy_universe/ticker_summary.csv", ticker_summary_fields, ticker_summary_rows)
text_write("01_strategy_universe/all_strategy_tickers.txt", "\n".join(all_strategy_tickers) + "\n")
json_write("01_strategy_universe/strategy_tickers.json", strategy_tickers)
if WRITE_GENERATED_READMES:
    text_write("01_strategy_universe/README.md", """# 01 Strategy Universe SSOT

`strategies.json` is the single source of truth for the strategy universe.

Use `strategy_id` from `strategies.json` as the canonical strategy key across all downstream folders.

## Files

- `strategies.json`: canonical strategy list. One row per strategy, including `strategy_id`, `session_id`, `display_name`, `target_asset`, `asset_type`, `profile`, `timeframe`, `required_symbols`, and backtest fields.
- `strategy_summary.csv`: derived spreadsheet view of `strategies.json`, enriched with paper/LLM summary fields for quick filtering. Do not treat this as the source for strategy identity.
- `tickers.txt`: derived distinct `target_asset` list from `strategies.json`.
- `ticker_summary.csv`: derived target-ticker index. `strategy_ids` is a pipe-delimited list of strategy IDs for that ticker.
- `all_strategy_tickers.txt`: derived distinct union of each strategy's `target_asset` and `required_symbols`.
- `strategy_tickers.json`: derived strategy-to-ticker mapping with ticker role metadata.

## Answer

The strategy list with `strategy_id` is `01_strategy_universe/strategies.json`.

For humans/spreadsheets, use `01_strategy_universe/strategy_summary.csv`, but treat it as a derived view.
""")

json_write("02_paper_actuals/paper_subscriptions.json", subscription_rows)
json_write("02_paper_actuals/paper_subscriptions_pageSize200.json", subscription_rows)
json_write("02_paper_actuals/paper_daily_rows.json", daily_rows)
json_write("02_paper_actuals/paper_runs.json", run_rows)
json_write("02_paper_actuals/performance_baselines.json", baseline_rows)
json_write("02_paper_actuals/performance_points.json", point_rows)

json_write("04_llm_benchmark/llm_benchmark_decisions.json", target_decision_rows)
json_write("04_llm_benchmark/llm_benchmark_rows.json", llm_benchmark_rows)
json_write("04_llm_benchmark/llm_benchmark_warnings.json", llm_warnings)
json_write("04_llm_benchmark/strategy_ticker_llm_benchmark_decisions.json", strategy_ticker_decision_rows)
csv_write("04_llm_benchmark/strategy_ticker_llm_benchmark_summary.csv", strategy_ticker_summary_fields, strategy_ticker_summary_rows)

manifest_path = ROOT / "manifest.json"
old_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
counts = dict(old_manifest.get("counts", {}))
counts.update({
    "allStrategyTickers": len(all_strategy_tickers),
    "llmBenchmarkDecisions": len(target_decision_rows),
    "llmBenchmarkRows": len(llm_benchmark_rows),
    "llmBenchmarkWarnings": len(llm_warnings),
    "paperDailyRows": len(daily_rows),
    "paperRuns": len(run_rows),
    "paperSubscriptions": len(subscription_rows),
    "performanceBaselines": len(baseline_rows),
    "performancePoints": len(point_rows),
    "strategies": len(strategy_rows),
    "strategyTickerLlmBenchmarkDecisions": len(strategy_ticker_decision_rows),
    "strategyTickerLlmBenchmarkSummaryRows": len(strategy_ticker_summary_rows),
    "strategyTickerMappings": len(strategy_tickers),
    "targetTickers": len(target_tickers),
})
counts["buyHoldCapBars"] = read_json_count("03_market_data/buy_hold_cap_day_bars.json")
counts["coreUsBenchmarkEtfDayBars"] = read_json_count("03_market_data/core_us_benchmark_etf_day_bars.json")
counts["coreUsBenchmarkEtfSymbols"] = len(CORE_ETFS)
counts["coreUsBenchmarkEtfWarnings"] = read_json_count("03_market_data/core_us_benchmark_etf_warnings.json")
counts["strategyBuyHoldDailyRows"] = read_json_count("05_comparisons/strategy_buy_hold_daily_rows.json")
counts["strategyBuyHoldSummaryRows"] = csv_data_count("05_comparisons/strategy_buy_hold_summary.csv")
counts["strategyBuyHoldWarnings"] = read_json_count("05_comparisons/strategy_buy_hold_warnings.json")
counts["paperMinusBenchmarksRows"] = csv_data_count("05_comparisons/paper_minus_benchmarks_sorted.csv")
counts["paperMinusBothBenchmarksRows"] = csv_data_count("05_comparisons/paper_minus_both_benchmarks_sorted.csv")
counts["finalAbelPortfolioLatestAvailableRows"] = csv_data_count("05_comparisons/final_abel_portfolio_selection_latest_available.csv")

now = datetime.now(timezone.utc).isoformat()
base_target_count = len(target_tickers) - EXTRA_STRATEGY_COUNT if EXTRA_STRATEGY_COUNT else len(target_tickers)
selection_source = (
    f"; fixed base strategy IDs from {BASE_STRATEGY_IDS_PATH}"
    if base_strategy_ids else
    f"; filtered by target_asset symbols from {SELECTION_CSV}"
    if selection_symbols else ""
)
extra_source = f"; plus {EXTRA_STRATEGY_COUNT} non-overlapping target_asset strategies" if EXTRA_STRATEGY_COUNT else ""
exact_extra_source = f"; plus exact target_asset strategies {','.join(EXTRA_TARGET_ASSETS)}" if EXTRA_TARGET_ASSETS else ""
selection_readme_line = (
    f"Fixed base from `{BASE_STRATEGY_IDS_PATH}` ({len(base_strategy_ids)} strategy IDs; {base_target_count} target tickers)"
    f" and supplemented with {EXTRA_STRATEGY_COUNT} non-overlapping target_asset strategies."
    if base_strategy_ids and EXTRA_STRATEGY_COUNT else
    f"Fixed base from `{BASE_STRATEGY_IDS_PATH}` ({len(base_strategy_ids)} strategy IDs; {len(target_tickers)} target tickers)."
    if base_strategy_ids else
    f"Filtered by `{SELECTION_CSV}` ({len(selection_symbols)} input symbols; {base_target_count} matched target tickers)"
    f" and supplemented with {EXTRA_STRATEGY_COUNT} non-overlapping target_asset strategies."
    if selection_symbols and EXTRA_STRATEGY_COUNT else
    f"Filtered by `{SELECTION_CSV}` ({len(selection_symbols)} input symbols; {len(target_tickers)} matched target tickers)."
    if selection_symbols else "No selection CSV filter was applied."
)
if EXTRA_TARGET_ASSETS:
    selection_readme_line = f"{selection_readme_line} Supplemented exact target assets: `{', '.join(EXTRA_TARGET_ASSETS)}`."
manifest = dict(old_manifest)
manifest["counts"] = {k: counts[k] for k in sorted(counts)}
manifest["generatedAt"] = now
manifest["refreshedAt"] = now
manifest["organizedAt"] = datetime.now(timezone.utc).date().isoformat()
manifest["refreshedFolders"] = ["01_strategy_universe", "02_paper_actuals", "04_llm_benchmark"]
manifest["environment"] = "prod"
manifest["source"] = f"router postgres config/router.prod.local.yaml{selection_source}{extra_source}{exact_extra_source}; CAP market data retained from previous export (API key invalid)"
manifest["userId"] = USER_ID_STR
json_write("manifest.json", manifest)

readme = f"""# Prod Strategy / Paper / Benchmark Export

Generated for user `{USER_ID_STR}` from the reachable prod router PostgreSQL database. {selection_readme_line} CAP `/market/day_bar` files are retained from the previous local export because the available CAP API key was rejected during this refresh.

## Dependency Layout

Read the folders in numeric order:

- `01_strategy_universe/`: strategy universe and ticker mappings. Everything else depends on these strategy IDs and target tickers.
- `02_paper_actuals/`: actual paper-trading outputs for those strategies.
- `03_market_data/`: CAP market data inputs used for benchmark and buy-and-hold comparisons.
- `04_llm_benchmark/`: LLM benchmark decisions and target-ticker benchmark rows, dependent on strategy/ticker universe plus paper date windows.
- `05_comparisons/`: derived comparison outputs, dependent on paper actuals plus market/LLM benchmark data.

## Counts

- Strategies: {len(strategy_rows)}
- Target tickers: {len(target_tickers)}
- All strategy tickers (`target_asset` plus `required_symbols`): {len(all_strategy_tickers)}
- Paper subscriptions: {len(subscription_rows)}
- Paper daily rows: {len(daily_rows)}
- Paper runs: {len(run_rows)}
- Target-ticker LLM benchmark decisions: {len(target_decision_rows)}
- Target-ticker computed LLM benchmark rows: {len(llm_benchmark_rows)}
- Strategy+ticker LLM benchmark decisions for all strategy tickers: {len(strategy_ticker_decision_rows)}
- Buy-and-hold CAP bars: {counts['buyHoldCapBars']} (retained)
- Strategy buy-and-hold daily rows: {counts['strategyBuyHoldDailyRows']} (retained)
- Core US benchmark ETF symbols (`SPY`, `QQQ`, `DIA`, `IWM`): {counts['coreUsBenchmarkEtfSymbols']}
- Core US benchmark ETF day bars: {counts['coreUsBenchmarkEtfDayBars']} (retained)

## Files By Dependency

### 01 Strategy Universe

- `01_strategy_universe/README.md`: SSOT contract for strategy identity and derived 01 views.
- `01_strategy_universe/strategies.json`: canonical strategy list with `strategy_id` and backtest metrics.
- `01_strategy_universe/strategy_summary.csv`: derived strategy-level spreadsheet view with latest paper fields.
- `01_strategy_universe/tickers.txt`: distinct strategy `target_asset` tickers only.
- `01_strategy_universe/ticker_summary.csv`: target ticker summary.
- `01_strategy_universe/all_strategy_tickers.txt`: distinct union of `target_asset` and `required_symbols`.
- `01_strategy_universe/strategy_tickers.json`: strategy-to-ticker mapping, including ticker role (`target`, `required`).

### 02 Paper Actuals

- `02_paper_actuals/paper_subscriptions.json`: paper subscription rows for exported strategies.
- `02_paper_actuals/paper_subscriptions_pageSize200.json`: page-size-200 subscription export retained for traceability.
- `02_paper_actuals/paper_daily_rows.json`: all paper daily rows for exported strategies.
- `02_paper_actuals/paper_runs.json`: paper run rows for exported strategies.
- `02_paper_actuals/performance_baselines.json`: paper chart baseline rows.
- `02_paper_actuals/performance_points.json`: paper chart performance point rows.

### 03 Market Data

- `03_market_data/buy_hold_cap_day_bars.json`: raw CAP OHLCV bars fetched for strategy target tickers.
- `03_market_data/core_us_benchmark_etf_day_bars.json`: full CAP OHLCV rows for `SPY`, `QQQ`, `DIA`, and `IWM`.
- `03_market_data/core_us_benchmark_etf_day_bars.csv`: CSV version of those benchmark ETF rows.
- `03_market_data/core_us_benchmark_etf_summary.csv`: coverage and total-return summary for each benchmark ETF.
- `03_market_data/core_us_benchmark_etf_warnings.json`: request warnings for those benchmark ETF pulls.

### 04 LLM Benchmark

- `04_llm_benchmark/llm_benchmark_decisions.json`: raw LLM paper-trade decisions for target tickers within each strategy paper date range.
- `04_llm_benchmark/llm_benchmark_rows.json`: computed target-ticker LLM benchmark rows using router target-asset benchmark logic.
- `04_llm_benchmark/llm_benchmark_warnings.json`: strategies where target-ticker benchmark rows could not be computed.
- `04_llm_benchmark/strategy_ticker_llm_benchmark_decisions.json`: raw LLM decisions for every strategy ticker within that strategy's paper date range.
- `04_llm_benchmark/strategy_ticker_llm_benchmark_summary.csv`: strategy+ticker coverage summary.

### 05 Comparisons

- `05_comparisons/strategy_buy_hold_daily_rows.json`: per-strategy target-ticker buy-and-hold rows aligned to each strategy's paper date window, with matching paper daily row fields included.
- `05_comparisons/strategy_buy_hold_daily_rows.csv`: CSV version of the aligned buy-and-hold rows for spreadsheet comparison.
- `05_comparisons/strategy_buy_hold_summary.csv`: per-strategy buy-and-hold summary.
- `05_comparisons/strategy_buy_hold_warnings.json`: strategies that could not produce buy-and-hold rows.
- `05_comparisons/paper_minus_benchmarks_sorted.csv`: combined paper-vs-LLM and paper-vs-buy-and-hold comparison, treating both benchmarks as peers.
- `05_comparisons/paper_minus_benchmarks_summary.md`: Markdown summary for the combined benchmark comparison.
- `05_comparisons/paper_minus_benchmarks_summary.html`: HTML summary for the combined benchmark comparison.
- `05_comparisons/paper_minus_both_benchmarks_sorted.csv`: strict comparison subset containing only rows with both LLM bench and buy-and-hold.
- `05_comparisons/paper_minus_both_benchmarks_summary.md`: strict winners summary for strategies that beat both LLM bench and buy-and-hold.
- `05_comparisons/final_abel_portfolio_selection_latest_available.csv`: final latest-available 30-ticker selection using non-USD equities, `benchmark_pass = Y`, and `paper_return >= -2%`.
- `05_comparisons/final_abel_portfolio_selection_latest_available.md`: Markdown summary of that final latest-available selection and refresh status.
- `05_comparisons/paper_minus_llm_sorted.csv`: prior paper-vs-LLM sorted comparison output retained with comparisons.
- `05_comparisons/paper_minus_llm_summary.html`: prior paper-vs-LLM HTML summary retained with comparisons.
- `05_comparisons/sorted_paper_minus_llm.txt`: prior paper-vs-LLM text ranking retained with comparisons.

## Notes

- Refreshed at `{now}`.
- {selection_readme_line}
- Router-backed folders refreshed in this run: `01_strategy_universe/`, `02_paper_actuals/`, and `04_llm_benchmark/`.
- `03_market_data/` and `05_comparisons/` are retained from the previous export because CAP `/api/market/day_bar` returned `API Key Invalid` for the available local key.
- `llm_benchmark_rows.json` is only meaningful for target tickers because it replays benchmark returns against each strategy's own target-asset paper returns.
- `strategy_ticker_llm_benchmark_decisions.json` is raw LLM decision coverage for all required symbols; it does not compute synthetic benchmark returns for non-target input symbols.
- Buy-and-hold starts at each strategy's first paper daily row date and ends at its latest paper daily row date. The first buy-and-hold row has `buy_hold_daily_return = 0` and `buy_hold_chart_value = 1`.
"""
if WRITE_GENERATED_READMES:
    text_write("README.md", readme)

print(json.dumps({
    "strategies": len(strategy_rows),
    "target_tickers": len(target_tickers),
    "all_strategy_tickers": len(all_strategy_tickers),
    "paper_subscriptions": len(subscription_rows),
    "paper_daily_rows": len(daily_rows),
    "paper_runs": len(run_rows),
    "performance_baselines": len(baseline_rows),
    "performance_points": len(point_rows),
    "llm_benchmark_decisions": len(target_decision_rows),
    "llm_benchmark_rows": len(llm_benchmark_rows),
    "llm_benchmark_warnings": len(llm_warnings),
    "strategy_ticker_llm_benchmark_decisions": len(strategy_ticker_decision_rows),
    "strategy_ticker_mappings": len(strategy_tickers),
    "refreshed_at": now,
}, ensure_ascii=False, indent=2))
