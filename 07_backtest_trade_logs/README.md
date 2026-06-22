# 07 Backtest Trade Logs

Generated/downloaded backtest trade-log evidence used by portfolio backtest curves.

## Depends On

- `01_strategy_universe/`
- CHFS local config

## Generated Outputs

- `trade_logs/<strategy_id>.csv`: downloaded trade log per strategy.
- `backtest_trade_log_index.csv`: strategy-to-local-file index with status and row counts.
- `backtest_trade_log_summary.json`: aggregate counts.
- `backtest_trade_log_warnings.json`: missing/unavailable URI details.
- `backtest_compounded_curves.csv`
- `backtest_compounded_summary.csv`

Generated outputs are ignored in the public repo.
