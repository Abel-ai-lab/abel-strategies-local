# 06 Portfolio Selection

Generated portfolio construction and app/website curve handoff layer.

## Product Role

This folder is the portfolio feature workbench. It is used to prototype and verify portfolio construction logic before deciding what belongs in the app.

The current app portfolio surface is intentionally light. New ideas such as scanning Abel account strategies into portfolio candidates, changing selection rules, producing benchmark comparisons, or preparing market-requested charts should be implemented and validated here first. Once a workflow is stable, migrate only the data contract and product behavior needed by the app.

## Depends On

- `01_strategy_universe/`
- `02_paper_actuals/`
- `03_market_data/`
- `05_comparisons/`
- `07_backtest_trade_logs/`
- Router PostgreSQL
- CAP `/market/day_bar`

## Tracked Process Files

- `build_portfolio_selection.py`: builds generated 06 outputs.
- `publish_admin_portfolio.py`: optionally publishes selected 06 portfolios to Abel admin.
- `verify_portfolio_selection.py`: verifies generated 06 outputs.

## Optional Abel Admin Publish

After `build_portfolio_selection.py` has generated local outputs, publish a selected portfolio explicitly:

```powershell
uv run python 06_portfolio_selection/publish_admin_portfolio.py --portfolio stock --title "<admin portfolio title>"
uv run python 06_portfolio_selection/publish_admin_portfolio.py --portfolio crypto --title "<admin portfolio title>"
uv run python 06_portfolio_selection/publish_admin_portfolio.py --portfolio all --stock-title "<stock title>" --crypto-title "<crypto title>"
```

Set `ABEL_ADMIN_BASE_URL` and `ABEL_ADMIN_API_KEY` in ignored local environment before publishing. The script sends the key as `api-key: <key>`, validates local selected rows against the Abel official account's active rows in `02_paper_actuals/paper_subscriptions.json`, creates a same-title portfolio when none exists, and replaces members when exactly one active same-title portfolio already exists. Use `--dry-run` to validate local inputs without calling Abel admin.

## Generated Outputs

- `stock_equal_weight_portfolio.csv`
- `crypto_equal_weight_portfolio.csv`
- `portfolio_component_returns.csv`
- `portfolio_backtest_components.csv`
- `portfolio_backtest_curves.csv`
- `portfolio_backtest_metrics.csv`
- `portfolio_equity_curves.json`
- `portfolio_equity_curves.csv`
- `portfolio_curve_metrics.csv`
- `portfolio_vs_benchmark_summary.csv`
- `selection_audit.json`
- `summary.html`

Generated outputs are ignored in the public repo.
