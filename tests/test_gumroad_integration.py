"""Contract tests for GumroadIntegration API client.

Verifies correct request construction and response parsing against
Gumroad API v2 contract using httpx transport mocking.
No real API calls — these tests validate our HTTP layer.
"""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest

from monai.integrations.gumroad import GumroadIntegration


ACCESS_TOKEN = "test_gumroad_access_token_abc123"


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.data_dir = MagicMock()
    return config


@pytest.fixture
def gumroad(mock_config, db):
    return GumroadIntegration(mock_config, db, access_token=ACCESS_TOKEN)


class _MockConnection:
    """Mock connection that records calls and returns canned responses."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []  # list of (method, endpoint, kwargs)

    def _handle(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        key = f"{method.upper()} {endpoint}"
        if key in self.responses:
            return self.responses[key]
        return httpx.Response(200, json={"success": True})

    def get(self, endpoint, **kwargs):
        return self._handle("GET", endpoint, **kwargs)

    def post(self, endpoint, **kwargs):
        return self._handle("POST", endpoint, **kwargs)

    def put(self, endpoint, **kwargs):
        return self._handle("PUT", endpoint, **kwargs)

    def delete(self, endpoint, **kwargs):
        return self._handle("DELETE", endpoint, **kwargs)

    def last_call_kwargs(self):
        return self.calls[-1][2] if self.calls else {}


def _mock_connection(gumroad, responses: dict[str, httpx.Response]):
    """Replace the agent connection with a mock that returns canned responses."""
    conn = _MockConnection(responses)
    gumroad.get_connection = MagicMock(return_value=conn)
    return conn


# ── Product CRUD ────────────────────────────────────────────────

class TestListProducts:
    def test_returns_product_list(self, gumroad):
        """list_products parses Gumroad /products response."""
        _mock_connection(gumroad, {
            "GET /products": httpx.Response(200, json={
                "success": True,
                "products": [
                    {
                        "id": "prod_001",
                        "name": "AI Prompt Pack",
                        "price": 1499,
                        "sales_count": 42,
                        "sales_usd_cents": 62958,
                        "short_url": "https://gumroad.com/l/ai-prompts",
                        "published": True,
                    },
                    {
                        "id": "prod_002",
                        "name": "Notion Templates",
                        "price": 999,
                        "sales_count": 15,
                        "sales_usd_cents": 14985,
                        "short_url": "https://gumroad.com/l/notion",
                        "published": True,
                    },
                ],
            }),
        })

        products = gumroad.list_products("digital_products")
        assert len(products) == 2
        assert products[0]["id"] == "prod_001"
        assert products[0]["name"] == "AI Prompt Pack"
        assert products[1]["sales_count"] == 15


class TestCreateProduct:
    def test_create_returns_product_data(self, gumroad):
        """create_product sends correct params and returns product."""
        conn = _mock_connection(gumroad, {
            "POST /products": httpx.Response(200, json={
                "success": True,
                "product": {
                    "id": "new_prod_123",
                    "name": "My New Product",
                    "price": 2499,
                    "short_url": "https://gumroad.com/l/new-product",
                    "published": False,
                },
            }),
        })

        result = gumroad.create_product(
            agent_name="digital_products",
            name="My New Product",
            price=2499,
            description="A great digital product",
        )

        assert result["id"] == "new_prod_123"
        assert result["short_url"] == "https://gumroad.com/l/new-product"

        # Verify correct params sent
        data = conn.last_call_kwargs().get("data", {})
        assert data["name"] == "My New Product"
        assert data["price"] == 2499
        assert data["access_token"] == ACCESS_TOKEN

    def test_create_with_extra_kwargs(self, gumroad):
        """Extra kwargs are passed through to the API."""
        conn = _mock_connection(gumroad, {
            "POST /products": httpx.Response(200, json={
                "success": True,
                "product": {"id": "p1"},
            }),
        })

        gumroad.create_product(
            agent_name="test",
            name="Product",
            price=999,
            description="Desc",
            url_name="custom-slug",
        )

        data = conn.last_call_kwargs().get("data", {})
        assert data.get("url_name") == "custom-slug"


class TestUpdateProduct:
    def test_update_sends_put(self, gumroad):
        conn = _mock_connection(gumroad, {
            "PUT /products/prod_001": httpx.Response(200, json={
                "success": True,
                "product": {
                    "id": "prod_001",
                    "name": "Updated Name",
                    "price": 1999,
                },
            }),
        })

        result = gumroad.update_product("test", "prod_001", name="Updated Name")
        assert result["name"] == "Updated Name"


class TestDeleteProduct:
    def test_delete_returns_success(self, gumroad):
        _mock_connection(gumroad, {
            "DELETE /products/prod_001": httpx.Response(200, json={
                "success": True,
            }),
        })

        assert gumroad.delete_product("test", "prod_001") is True

    def test_delete_failure_returns_false(self, gumroad):
        _mock_connection(gumroad, {
            "DELETE /products/prod_999": httpx.Response(200, json={
                "success": False,
                "message": "Product not found",
            }),
        })

        assert gumroad.delete_product("test", "prod_999") is False


# ── Sales ───────────────────────────────────────────────────────

class TestListSales:
    def test_list_sales_with_filters(self, gumroad):
        conn = _mock_connection(gumroad, {
            "GET /sales": httpx.Response(200, json={
                "success": True,
                "sales": [
                    {"id": "s1", "price": 1499, "email": "a@b.com", "refunded": False},
                    {"id": "s2", "price": 1499, "email": "c@d.com", "refunded": False},
                ],
            }),
        })

        result = gumroad.list_sales(
            "test", product_id="prod_001", after="2024-01-01", page=1,
        )
        assert len(result.get("sales", [])) == 2

        # Verify filter params passed
        params = conn.last_call_kwargs().get("params", {})
        assert params["product_id"] == "prod_001"
        assert params["after"] == "2024-01-01"


class TestGetSale:
    def test_get_sale_by_id(self, gumroad):
        _mock_connection(gumroad, {
            "GET /sales/s1": httpx.Response(200, json={
                "success": True,
                "sale": {
                    "id": "s1",
                    "price": 1499,
                    "email": "buyer@example.com",
                    "product_name": "AI Prompts",
                    "refunded": False,
                },
            }),
        })

        sale = gumroad.get_sale("test", "s1")
        assert sale["id"] == "s1"
        assert sale["email"] == "buyer@example.com"


# ── Revenue Summary ─────────────────────────────────────────────

class TestRevenueSummary:
    def test_revenue_aggregated_from_products(self, gumroad):
        """get_revenue_summary aggregates sales_usd_cents across products."""
        _mock_connection(gumroad, {
            "GET /products": httpx.Response(200, json={
                "success": True,
                "products": [
                    {"id": "p1", "name": "Product A",
                     "sales_usd_cents": 50000, "sales_count": 10,
                     "price": 4999},
                    {"id": "p2", "name": "Product B",
                     "sales_usd_cents": 25000, "sales_count": 5,
                     "price": 4999},
                ],
            }),
        })

        summary = gumroad.get_revenue_summary("digital_products")
        assert summary["total_revenue_usd"] == Decimal("750")
        assert summary["total_sales"] == 15
        assert len(summary["products"]) == 2
        assert summary["products"][0]["name"] == "Product A"
        assert summary["products"][0]["revenue_usd"] == Decimal("500")

    def test_zero_sales_returns_zero_revenue(self, gumroad):
        _mock_connection(gumroad, {
            "GET /products": httpx.Response(200, json={
                "success": True,
                "products": [],
            }),
        })

        summary = gumroad.get_revenue_summary("test")
        assert summary["total_revenue_usd"] == Decimal("0")
        assert summary["total_sales"] == 0


# ── Health Check ────────────────────────────────────────────────

class TestHealthCheck:
    def test_healthy_returns_ok(self, gumroad):
        _mock_connection(gumroad, {
            "GET /user": httpx.Response(200, json={
                "success": True,
                "user": {"name": "TestUser", "email": "test@example.com"},
            }),
        })

        result = gumroad.health_check()
        assert result["status"] == "ok"
        assert result["user"] == "TestUser"

    def test_no_token_returns_not_configured(self, mock_config, db):
        gumroad = GumroadIntegration(mock_config, db, access_token="")
        result = gumroad.health_check()
        assert result["status"] == "not_configured"

    def test_api_error_returns_error(self, gumroad):
        def _raise(*args, **kwargs):
            raise httpx.ConnectError("Connection refused")
        conn = _mock_connection(gumroad, {})
        conn.get = _raise

        result = gumroad.health_check()
        assert result["status"] == "error"
        assert "Connection refused" in result["error"]


# ── Auth Params ─────────────────────────────────────────────────

class TestAuthParams:
    def test_access_token_included_in_params(self, gumroad):
        """All requests include access_token as query param."""
        conn = _mock_connection(gumroad, {
            "GET /products": httpx.Response(200, json={
                "success": True, "products": [],
            }),
        })

        gumroad.list_products("test")

        params = conn.last_call_kwargs().get("params", {})
        assert params["access_token"] == ACCESS_TOKEN

    def test_no_token_empty_params(self, mock_config, db):
        gumroad = GumroadIntegration(mock_config, db, access_token="")
        assert gumroad._get_auth_params() == {}
