# Tickers List Workflow

Public workflow repo for generating an Abel strategy, benchmark, and portfolio data pack. The repository keeps the generation/update/verification code and dependency documentation, but real generated data is intentionally ignored and not tracked.

## What Is Tracked

| Path | Role |
| --- | --- |
| `scripts/` | Root-level refresh, fetch, compounding, and verification scripts. |
| `06_portfolio_selection/build_portfolio_selection.py` | Portfolio construction and website curve handoff builder. |
| `06_portfolio_selection/verify_portfolio_selection.py` | Portfolio output verifier. |
| `01_strategy_universe/README.md` through `07_backtest_trade_logs/README.md` | Module contracts and expected generated outputs. |
| `config/*.example.yaml` | Public, non-secret configuration templates. |
| `.env.example` | Public environment variable template. |
| `manifest.example.json` | Shape of the generated runtime manifest. |
| `fixtures/tiny/` | Tiny synthetic fixture data for schema reference only. |

## What Is Not Tracked

Generated artifacts are ignored by `.gitignore`, including real strategy exports, paper rows, market bars, LLM benchmark rows, comparison CSV/HTML/JSON files, portfolio outputs, downloaded trade logs, runtime `manifest.json`, and local credential files.

Local refreshes will recreate ignored outputs under the numbered folders.

## Dependency Layout

| Path | Role |
| --- | --- |
| `01_strategy_universe/` | Generated canonical strategy and ticker universe. |
| `02_paper_actuals/` | Generated paper-trading evidence for comparisons. |
| `03_market_data/` | Generated market and benchmark bars. |
| `04_llm_benchmark/` | Generated LLM benchmark data. |
| `05_comparisons/` | Generated strategy-level paper-vs-benchmark comparison outputs. |
| `06_portfolio_selection/` | Generated portfolio construction and website curve handoff outputs. |
| `07_backtest_trade_logs/` | Generated/downloaded backtest trade-log evidence. |

## Setup

```powershell
uv sync
Copy-Item .env.example .env
Copy-Item config/router.example.yaml config/router.prod.local.yaml
Copy-Item config/chfs.example.yaml config/chfs.sit.local.yaml
```

Fill local config files with operator-provided credentials. Do not commit `.env` or `config/*.local.yaml`.

## Refresh And Verification

- Full refresh order and commands: `UPDATE_WORKFLOW.md`.
- Architecture and dependencies: `architechtrue.md`.
- Module-specific contracts: each numbered folder's `README.md`.
- Tiny synthetic schema reference: `fixtures/tiny/README.md`.

## Public Data Policy

This repo should not publish real strategy IDs, paper runs, market data pulls, LLM benchmark outputs, trade logs, generated portfolio constituents, or runtime manifests. Keep such outputs local, ignored, and regenerated from scripts when needed.
