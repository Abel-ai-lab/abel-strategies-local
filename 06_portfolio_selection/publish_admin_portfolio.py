from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "06_portfolio_selection"
PAPER_SUBSCRIPTIONS_PATH = ROOT / "02_paper_actuals" / "paper_subscriptions.json"
ADMIN_API_PREFIX = "web/skill-dashboard/admin"
CALCULATION_METHOD = "equal_weight"
OFFICIAL_ABEL_USER_ID = 318274928728084480

PORTFOLIO_CSV = {
    "stock": OUT / "stock_equal_weight_portfolio.csv",
    "crypto": OUT / "crypto_equal_weight_portfolio.csv",
}


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishContext:
    portfolio: str
    title: str
    selected_strategy_ids: list[int]
    member_subscription_ids: list[int]
    payload: dict[str, Any]


class AbelAdminClient:
    def __init__(self, *, base_url: str, api_key: str, timeout: float = 60.0):
        self.base_url = require_text(base_url, "ABEL_ADMIN_BASE_URL")
        self.timeout = timeout
        self.session = requests.Session()
        self.headers = {
            "api-key": normalize_api_key(api_key),
            "Content-Type": "application/json",
        }

    def list_portfolios(self) -> list[dict[str, Any]]:
        data = self._request_json("GET", "portfolios", params={"includeArchived": "false"})
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PublishError("Admin list portfolios response missing data.items")
        return [item for item in items if isinstance(item, dict)]

    def create_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request_json("POST", "portfolios", json_payload=payload)
        portfolio = data.get("portfolio") if isinstance(data, dict) else None
        if not isinstance(portfolio, dict):
            raise PublishError("Admin create portfolio response missing data.portfolio")
        return portfolio

    def replace_members(self, portfolio_id: str | int, members: list[dict[str, int]]) -> dict[str, Any]:
        data = self._request_json(
            "PATCH",
            f"portfolios/{portfolio_id}/members",
            json_payload={"members": members},
        )
        portfolio = data.get("portfolio") if isinstance(data, dict) else None
        if not isinstance(portfolio, dict):
            raise PublishError("Admin replace members response missing data.portfolio")
        return portfolio

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                admin_url(self.base_url, path),
                headers=self.headers,
                params=params,
                json=json_payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PublishError(f"Admin request failed: {exc}") from exc

        if response.status_code >= 400:
            raise PublishError(
                f"Admin request returned HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise PublishError(f"Admin request returned non-JSON body: {response.text[:300]}") from exc
        if not isinstance(body, dict):
            raise PublishError("Admin request returned non-object JSON")
        code = body.get("code")
        if code not in {None, 200}:
            raise PublishError(str(body.get("message") or f"Admin request failed with code {code}"))
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise PublishError("Admin response data must be an object")
        return data


def load_repo_env(root: Path = ROOT) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise PublishError(f"Missing selected portfolio CSV: {path}")
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_json(path: Path) -> Any:
    if not path.exists():
        raise PublishError(f"Missing paper subscriptions JSON: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def require_text(value: str | None, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PublishError(f"{name} is required")
    return text


def normalize_api_key(value: str | None) -> str:
    api_key = require_text(value, "ABEL_ADMIN_API_KEY")
    if api_key.lower().startswith("bearer "):
        raise PublishError("ABEL_ADMIN_API_KEY must be the raw API key, not a Bearer token")
    return api_key


def admin_api_key_from_env() -> str | None:
    return os.environ.get("ABEL_ADMIN_API_KEY") or os.environ.get("ABEL_ADMIN_AUTH_TOKEN")


def admin_url(base_url: str, path: str) -> str:
    endpoint = f"{ADMIN_API_PREFIX.rstrip('/')}/{path.lstrip('/')}"
    return urljoin(require_text(base_url, "ABEL_ADMIN_BASE_URL").rstrip("/") + "/", endpoint)


def selected_strategy_ids(portfolio: str, *, root: Path = ROOT) -> list[int]:
    csv_path = portfolio_csv_path(portfolio, root=root)
    rows = read_csv_rows(csv_path)
    if not rows:
        raise PublishError(f"{csv_path} has no selected rows")

    strategy_ids: list[int] = []
    seen: set[int] = set()
    duplicate_ids: set[int] = set()
    missing_rows: list[int] = []
    for index, row in enumerate(rows, 2):
        raw_strategy_id = str(row.get("strategy_id") or "").strip()
        if not raw_strategy_id:
            missing_rows.append(index)
            continue
        try:
            strategy_id = int(raw_strategy_id)
        except ValueError as exc:
            raise PublishError(f"Invalid strategy_id on {csv_path}:{index}: {raw_strategy_id}") from exc
        if strategy_id in seen:
            duplicate_ids.add(strategy_id)
        seen.add(strategy_id)
        strategy_ids.append(strategy_id)

    if missing_rows:
        raise PublishError(f"Selected portfolio rows missing strategy_id at CSV rows: {missing_rows}")
    if duplicate_ids:
        raise PublishError(f"Selected portfolio has duplicate strategy_id values: {sorted(duplicate_ids)}")
    return strategy_ids


def portfolio_csv_path(portfolio: str, *, root: Path = ROOT) -> Path:
    if portfolio not in PORTFOLIO_CSV:
        raise PublishError(f"Unsupported portfolio: {portfolio}")
    return root / "06_portfolio_selection" / PORTFOLIO_CSV[portfolio].name


def active_subscription_ids_for_strategies(
    strategy_ids: list[int],
    *,
    subscriptions_path: Path = PAPER_SUBSCRIPTIONS_PATH,
    owner_user_id: int = OFFICIAL_ABEL_USER_ID,
) -> list[int]:
    selected = set(strategy_ids)
    active_by_strategy: dict[int, list[int]] = {strategy_id: [] for strategy_id in strategy_ids}
    rows = read_json(subscriptions_path)
    if not isinstance(rows, list):
        raise PublishError(f"{subscriptions_path} must contain a JSON list")

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        try:
            strategy_id = int(str(row.get("strategy_id") or "").strip())
        except ValueError:
            continue
        if strategy_id not in selected:
            continue
        raw_user_id = str(row.get("user_id") or "").strip()
        if raw_user_id:
            try:
                user_id = int(raw_user_id)
            except ValueError:
                continue
            if user_id != owner_user_id:
                continue
        if str(row.get("status") or "").strip().lower() != "active":
            continue
        raw_subscription_id = str(row.get("subscription_id") or "").strip()
        if not raw_subscription_id:
            raise PublishError(
                f"Active subscription row {index} for strategy {strategy_id} is missing subscription_id"
            )
        try:
            subscription_id = int(raw_subscription_id)
        except ValueError as exc:
            raise PublishError(
                f"Invalid subscription_id in row {index} for strategy {strategy_id}: {raw_subscription_id}"
            ) from exc
        active_by_strategy[strategy_id].append(subscription_id)

    missing = [strategy_id for strategy_id in strategy_ids if not active_by_strategy[strategy_id]]
    duplicates = {
        strategy_id: ids
        for strategy_id, ids in active_by_strategy.items()
        if len(ids) > 1
    }
    if missing:
        raise PublishError(f"Selected strategies missing active paper subscription: {missing}")
    if duplicates:
        raise PublishError(f"Selected strategies have duplicate active paper subscriptions: {duplicates}")

    subscription_ids = [active_by_strategy[strategy_id][0] for strategy_id in strategy_ids]
    duplicate_subscription_ids = sorted(
        subscription_id
        for subscription_id in set(subscription_ids)
        if subscription_ids.count(subscription_id) > 1
    )
    if duplicate_subscription_ids:
        raise PublishError(f"Selected portfolio has duplicate subscription_id values: {duplicate_subscription_ids}")
    return subscription_ids


def portfolio_payload(title: str, subscription_ids: list[int]) -> dict[str, Any]:
    return {
        "title": require_text(title, "title"),
        "portfolioType": "manual",
        "calculationMethod": CALCULATION_METHOD,
        "members": [{"subscriptionId": subscription_id} for subscription_id in subscription_ids],
    }


def build_publish_context(portfolio: str, title: str, *, root: Path = ROOT) -> PublishContext:
    strategy_ids = selected_strategy_ids(portfolio, root=root)
    subscription_ids = active_subscription_ids_for_strategies(
        strategy_ids,
        subscriptions_path=root / "02_paper_actuals" / "paper_subscriptions.json",
    )
    payload = portfolio_payload(title, subscription_ids)
    return PublishContext(
        portfolio=portfolio,
        title=payload["title"],
        selected_strategy_ids=strategy_ids,
        member_subscription_ids=subscription_ids,
        payload=payload,
    )


def find_exact_active_title_match(portfolios: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    matches = [
        portfolio
        for portfolio in portfolios
        if portfolio.get("title") == title
        and str(portfolio.get("status") or "active").lower() == "active"
    ]
    if len(matches) > 1:
        ids = [str(portfolio.get("portfolioId") or "") for portfolio in matches]
        raise PublishError(f"Multiple active admin portfolios already use title {title!r}: {ids}")
    return matches[0] if matches else None


def publish_context(client: AbelAdminClient, context: PublishContext) -> dict[str, Any]:
    existing = find_exact_active_title_match(client.list_portfolios(), context.title)
    if existing is None:
        portfolio = client.create_portfolio(context.payload)
        return publish_result("created", context, portfolio)

    portfolio_id = str(existing.get("portfolioId") or "").strip()
    if not portfolio_id:
        raise PublishError(f"Matched portfolio {context.title!r} is missing portfolioId")
    portfolio = client.replace_members(portfolio_id, context.payload["members"])
    return publish_result("updated", context, portfolio)


def publish_result(action: str, context: PublishContext, portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "action": action,
        "portfolio": context.portfolio,
        "title": context.title,
        "calculationMethod": CALCULATION_METHOD,
        "selectedStrategyCount": len(context.selected_strategy_ids),
        "memberCount": len(context.member_subscription_ids),
    }
    if portfolio:
        result["adminPortfolioId"] = portfolio.get("portfolioId")
        result["adminStatus"] = portfolio.get("status")
    return result


def requested_jobs(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.portfolio == "all":
        if args.title:
            raise PublishError("--title is only valid with --portfolio stock or --portfolio crypto")
        if not args.stock_title or not args.crypto_title:
            raise PublishError("--stock-title and --crypto-title are required with --portfolio all")
        return [("stock", args.stock_title), ("crypto", args.crypto_title)]

    if args.stock_title or args.crypto_title:
        raise PublishError("--stock-title and --crypto-title are only valid with --portfolio all")
    if not args.title:
        raise PublishError("--title is required with --portfolio stock or --portfolio crypto")
    return [(args.portfolio, args.title)]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish selected 06 portfolio rows to Abel admin portfolio management.",
    )
    parser.add_argument("--portfolio", choices=["stock", "crypto", "all"], required=True)
    parser.add_argument("--title", help="Portfolio title for --portfolio stock or --portfolio crypto")
    parser.add_argument("--stock-title", help="Portfolio title for stock when --portfolio all")
    parser.add_argument("--crypto-title", help="Portfolio title for crypto when --portfolio all")
    parser.add_argument("--dry-run", action="store_true", help="Validate local inputs without calling Abel admin")
    parser.add_argument("--timeout", type=float, default=60.0, help="Admin API request timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> None:
    load_repo_env()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        jobs = requested_jobs(args)
        contexts = [build_publish_context(portfolio, title) for portfolio, title in jobs]
        if args.dry_run:
            print(json.dumps([publish_result("dry_run_upsert", context) for context in contexts], indent=2))
            return

        client = AbelAdminClient(
            base_url=os.environ.get("ABEL_ADMIN_BASE_URL"),
            api_key=admin_api_key_from_env(),
            timeout=args.timeout,
        )
        print(json.dumps([publish_context(client, context) for context in contexts], indent=2))
    except PublishError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
