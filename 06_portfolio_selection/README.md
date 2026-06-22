# 06 Portfolio Selection

Generated portfolio construction and website curve handoff layer.

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
- `verify_portfolio_selection.py`: verifies generated 06 outputs.

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
