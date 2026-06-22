# Repository Instructions

This repository is public workflow code for generating a local Abel strategy, benchmark, and portfolio data pack. Keep the repository safe to publish.

## Public Repo Boundary

- Track workflow code, documentation, config templates, and tiny synthetic fixtures only.
- Do not track real generated data from `01_strategy_universe/` through `07_backtest_trade_logs/`.
- Do not track runtime `manifest.json`; use `manifest.example.json` for the public schema example.
- Do not track `.env`, `config/*.local.yaml`, API keys, database credentials, CHFS credentials, or generated credential-bearing dumps.
- Keep real generated outputs local and ignored by `.gitignore`.

## Data And Output Rules

- Treat numbered folder outputs as generated artifacts.
- Prefer regenerating artifacts with scripts instead of hand-editing CSV, JSON, HTML, or TXT outputs.
- Keep `fixtures/tiny/` synthetic. Do not copy real strategy IDs, paper rows, market bars, LLM outputs, portfolio constituents, or trade logs into fixtures.
- If adding a new generated output path, update `.gitignore`, `README.md`, `UPDATE_WORKFLOW.md`, and the relevant module README.

## Configuration

- Public templates live in `.env.example`, `config/router.example.yaml`, and `config/chfs.example.yaml`.
- Local operator configs should use ignored paths such as `.env`, `config/router.prod.local.yaml`, and `config/chfs.sit.local.yaml`.
- Use environment variables documented in `UPDATE_WORKFLOW.md` instead of hardcoded local absolute paths.

## Verification

- For code changes, run `uv run python -m compileall scripts 06_portfolio_selection`.
- For data refresh changes, run the relevant verification command from `UPDATE_WORKFLOW.md` after generating local outputs.
- Before preparing a public commit, check that tracked files do not include real generated data or secrets.

Useful checks:

```powershell
git ls-files | Where-Object { $_ -match '\.(csv|json|html|txt)$' -and $_ -notmatch '^fixtures/' -and $_ -notmatch '\.example\.json$' }
git grep -n -I -E "<private-path-or-secret-pattern>" -- . ':!fixtures'
```

## Documentation

- Keep `README.md` focused on public setup and repo policy.
- Keep `architechtrue.md` focused on data ownership and dependencies.
- Keep `UPDATE_WORKFLOW.md` as the operational refresh order.
- Keep module README files as output contracts, not generated run reports.
