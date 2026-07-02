from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("publish_admin_portfolio.py")
SPEC = importlib.util.spec_from_file_location("publish_admin_portfolio", MODULE_PATH)
publish = importlib.util.module_from_spec(SPEC)
sys.modules["publish_admin_portfolio"] = publish
assert SPEC.loader is not None
SPEC.loader.exec_module(publish)


class FakeAdminClient:
    def __init__(self, portfolios: list[dict]):
        self.portfolios = portfolios
        self.calls: list[tuple] = []

    def list_portfolios(self) -> list[dict]:
        self.calls.append(("list",))
        return self.portfolios

    def create_portfolio(self, payload: dict) -> dict:
        self.calls.append(("create", payload))
        return {"portfolioId": "7001", "status": "active", "title": payload["title"]}

    def replace_members(self, portfolio_id: str, members: list[dict]) -> dict:
        self.calls.append(("replace", portfolio_id, members))
        return {"portfolioId": portfolio_id, "status": "active", "memberCount": len(members)}


class PublishAdminPortfolioTests(unittest.TestCase):
    def test_stock_publish_payload_generation(self) -> None:
        with self.synthetic_root() as root:
            write_selection(root, "stock", [1001, 1002])
            write_subscriptions(root, [(1001, 9001, "active"), (1002, 9002, "active")])

            context = publish.build_publish_context("stock", "Core Stock", root=root)

            self.assertEqual(context.portfolio, "stock")
            self.assertEqual(context.selected_strategy_ids, [1001, 1002])
            self.assertEqual(context.payload, {
                "title": "Core Stock",
                "portfolioType": "manual",
                "calculationMethod": "equal_weight",
                "members": [{"subscriptionId": 9001}, {"subscriptionId": 9002}],
            })

    def test_crypto_publish_payload_generation(self) -> None:
        with self.synthetic_root() as root:
            write_selection(root, "crypto", [2001])
            write_subscriptions(root, [(2001, 9901, "active")])

            context = publish.build_publish_context("crypto", "Core Crypto", root=root)

            self.assertEqual(context.portfolio, "crypto")
            self.assertEqual(context.payload["members"], [{"subscriptionId": 9901}])
            self.assertEqual(context.payload["calculationMethod"], "equal_weight")

    def test_same_title_portfolio_updates_members(self) -> None:
        context = context_with_members("Core", [9001, 9002])
        client = FakeAdminClient([
            {"portfolioId": "7001", "title": "Core", "status": "active"},
        ])

        result = publish.publish_context(client, context)

        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["adminPortfolioId"], "7001")
        self.assertEqual(client.calls, [
            ("list",),
            ("replace", "7001", [{"subscriptionId": 9001}, {"subscriptionId": 9002}]),
        ])

    def test_create_path_when_no_same_title_portfolio_exists(self) -> None:
        context = context_with_members("Core", [9001])
        client = FakeAdminClient([
            {"portfolioId": "7000", "title": "Other", "status": "active"},
        ])

        result = publish.publish_context(client, context)

        self.assertEqual(result["action"], "created")
        self.assertEqual(result["adminPortfolioId"], "7001")
        self.assertEqual(client.calls, [
            ("list",),
            ("create", context.payload),
        ])

    def test_missing_active_subscription_fails_before_api_call(self) -> None:
        with self.synthetic_root() as root:
            write_selection(root, "stock", [1001])
            write_subscriptions(root, [(1001, 9001, "archived")])

            with self.assertRaisesRegex(publish.PublishError, "missing active paper subscription"):
                publish.build_publish_context("stock", "Core", root=root)

    def test_ambiguous_duplicate_title_fails(self) -> None:
        context = context_with_members("Core", [9001])
        client = FakeAdminClient([
            {"portfolioId": "7001", "title": "Core", "status": "active"},
            {"portfolioId": "7002", "title": "Core", "status": "active"},
        ])

        with self.assertRaisesRegex(publish.PublishError, "Multiple active admin portfolios"):
            publish.publish_context(client, context)
        self.assertEqual(client.calls, [("list",)])

    def test_duplicate_active_subscription_for_strategy_fails(self) -> None:
        with self.synthetic_root() as root:
            write_selection(root, "stock", [1001])
            write_subscriptions(root, [(1001, 9001, "active"), (1001, 9002, "active")])

            with self.assertRaisesRegex(publish.PublishError, "duplicate active paper subscriptions"):
                publish.build_publish_context("stock", "Core", root=root)

    def test_subscriptions_are_filtered_to_official_abel_user(self) -> None:
        with self.synthetic_root() as root:
            write_selection(root, "crypto", [2001])
            write_subscriptions(root, [
                (2001, 9901, "active", publish.OFFICIAL_ABEL_USER_ID),
                (2001, 9902, "active", 42),
            ])

            context = publish.build_publish_context("crypto", "Core Crypto", root=root)

            self.assertEqual(context.payload["members"], [{"subscriptionId": 9901}])

    def test_admin_client_uses_api_key_header_without_bearer(self) -> None:
        client = publish.AbelAdminClient(
            base_url="https://api-sit.abel.ai/router",
            api_key="local-api-key",
        )

        self.assertEqual(client.headers["api-key"], "local-api-key")
        self.assertNotIn("Authorization", client.headers)

    @staticmethod
    @contextmanager
    def synthetic_root():
        with tempfile.TemporaryDirectory() as root:
            yield Path(root)


def context_with_members(title: str, subscription_ids: list[int]):
    return publish.PublishContext(
        portfolio="stock",
        title=title,
        selected_strategy_ids=[index + 1000 for index in range(len(subscription_ids))],
        member_subscription_ids=subscription_ids,
        payload=publish.portfolio_payload(title, subscription_ids),
    )


def write_selection(root_value, portfolio: str, strategy_ids: list[int]) -> None:
    root = Path(root_value)
    output_dir = root / "06_portfolio_selection"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{portfolio}_equal_weight_portfolio.csv"
    if portfolio == "stock":
        path = output_dir / "stock_equal_weight_portfolio.csv"
    elif portfolio == "crypto":
        path = output_dir / "crypto_equal_weight_portfolio.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["selection_rank", "symbol", "strategy_id"])
        writer.writeheader()
        for index, strategy_id in enumerate(strategy_ids, 1):
            writer.writerow({
                "selection_rank": index,
                "symbol": f"T{index}",
                "strategy_id": strategy_id,
            })


def write_subscriptions(root_value, rows) -> None:
    root = Path(root_value)
    output_dir = root / "02_paper_actuals"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = []
    for row in rows:
        strategy_id, subscription_id, status, *rest = row
        item = {
            "strategy_id": strategy_id,
            "subscription_id": subscription_id,
            "status": status,
        }
        if rest:
            item["user_id"] = rest[0]
        payload.append(item)
    (output_dir / "paper_subscriptions.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
