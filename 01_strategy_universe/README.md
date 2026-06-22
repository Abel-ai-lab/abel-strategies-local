# 01 Strategy Universe

Generated canonical strategy and ticker universe.

## Depends On

- Router PostgreSQL

## Generated Outputs

- `strategies.json`: canonical strategy list and strategy identity SSOT.
- `strategy_summary.csv`: spreadsheet-friendly view of strategies.
- `tickers.txt`: distinct `target_asset` list.
- `ticker_summary.csv`: target-ticker index.
- `all_strategy_tickers.txt`: union of target and required symbols.
- `strategy_tickers.json`: strategy-to-ticker role mapping.

Generated outputs are ignored in the public repo.
