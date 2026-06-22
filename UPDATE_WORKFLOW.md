# Update Workflow

This repo keeps the public workflow for producing two final generated outputs: `05_comparisons/` for strategy-level benchmark review and `06_portfolio_selection/` for portfolio construction plus app/website handoff. The workflow can also serve as a reference for future app portfolio features and for fast market-requested chart/result generation. Real outputs are ignored and should be regenerated locally.

## Configuration

- Copy `.env.example` to `.env` for local environment overrides.
- Copy `config/router.example.yaml` to `config/router.prod.local.yaml` or another `*.local.yaml` file.
- Copy `config/chfs.example.yaml` to `config/chfs.sit.local.yaml` for trade-log downloads.
- Use `ROUTER_CONFIG=<path>` to select a router config.
- Use `CHFS_CONFIG=<path>` to select a CHFS config.
- Use `CAP_API_KEY` and optionally `CAP_BASE_URL` for CAP benchmark bars.
- Local configs and `.env` are ignored because they contain credentials.
- Python dependencies are managed by `uv`; run `uv sync` before refresh or verification on a new machine.

## Refresh Order

Run from the repo root.

1. `01_strategy_universe/`, `02_paper_actuals/`, `04_llm_benchmark/`

   Source: router PostgreSQL.

   ```powershell
   uv run python scripts/refresh_router_raw_data_tmp.py
   ```

   Optional filters:

   ```powershell
   $env:SELECTION_CSV = 'references/preferred_selection.csv'; uv run python scripts/refresh_router_raw_data_tmp.py
   $env:BASE_STRATEGY_IDS_PATH = 'references/base_strategy_ids.csv'; $env:EXTRA_STRATEGY_COUNT = '10'; uv run python scripts/refresh_router_raw_data_tmp.py
   ```

2. `07_backtest_trade_logs/`

   Source: CHFS trade logs using each strategy's `backtest_trade_log_uri` from `01_strategy_universe/strategies.json`.

   ```powershell
   uv run python scripts/fetch_chfs_backtest_trade_logs.py
   ```

3. Scripted paper/backtest compounded returns

   Source: `02_paper_actuals/paper_daily_rows.json` and `07_backtest_trade_logs/trade_logs/<strategy_id>.csv`.

   ```powershell
   uv run python scripts/build_compounded_returns.py
   ```

4. `03_market_data/` and `05_comparisons/`

   Source: refreshed paper/LLM rows plus router `ref_stock_price` / `ref_crypto_price`; current paper-window target bars may use paper-close fallback when router ref tables do not cover required dates.

   ```powershell
   uv run python scripts/refresh_market_and_comparisons.py
   ```

5. Portfolio benchmark bars for `QQQ` and `BTCUSD`

   Source: CAP `/market/day_bar`.

   ```powershell
   uv run python scripts/fetch_cap_portfolio_benchmarks.py
   ```

6. `06_portfolio_selection/`

   Source: full active router ticker universe plus CAP returns, `05_comparisons` evidence, and `07_backtest_trade_logs/`.

   ```powershell
   uv run python 06_portfolio_selection/build_portfolio_selection.py
   ```

## Verification

Run after refresh:

```powershell
uv run python scripts/verify_refreshed_outputs.py
uv run python scripts/verify_backtest_trade_logs.py
uv run python scripts/verify_compounded_returns.py
uv run python scripts/verify_portfolio_benchmarks.py
uv run python 06_portfolio_selection/verify_portfolio_selection.py
```

## Public Repo Rule

Generated output files remain ignored. Commit workflow changes, docs, templates, and synthetic fixtures only.
