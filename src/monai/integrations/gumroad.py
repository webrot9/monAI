"""Gumroad integration — sell digital products.

Gumroad API v2: https://app.gumroad.com/api
Used by: digital_products, course_creation, newsletter strategies.

Each strategy agent gets its own connection with independent rate limiting.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.integrations.base import PlatformIntegration, RateLimitConfig

logger = logging.getLogger(__name__)


class GumroadIntegration(PlatformIntegration):
    """Gumroad API integration for selling digital products."""

    platform_name = "gumroad"
    base_url = "https://api.gumroad.com/v2"
    default_rate_limit = RateLimitConfig(
        requests_per_minute=30,
        requests_per_day=5000,
        retry_after_seconds=60,
        max_retries=3,
    )

    def __init__(self, config: Config, db: Database, access_token: str = ""):
        super().__init__(config, db)
        self._access_token = access_token

    def _get_auth_params(self) -> dict[str, str]:
        """Gumroad uses access_token as query param."""
        if self._access_token:
            return {"access_token": self._access_token}
        return {}

    def health_check(self) -> dict[str, Any]:
        """Verify Gumroad API access."""
        if not self._access_token:
            return {"status": "not_configured", "platform": self.platform_name}
        try:
            conn = self.get_connection("health_check")
            response = conn.get("/user", params=self._get_auth_params())
            data = response.json()
            return {
                "status": "ok",
                "platform": self.platform_name,
                "user": data.get("user", {}).get("name", "unknown"),
            }
        except Exception as e:
            return {"status": "error", "platform": self.platform_name, "error": str(e)}

    # ── Products ──────────────────────────────────────────────

    def list_products(self, agent_name: str) -> list[dict[str, Any]]:
        """List all products on the Gumroad account."""
        conn = self.get_connection(agent_name)
        response = conn.get("/products", params=self._get_auth_params())
        self.log_request(agent_name, "/products", status_code=response.status_code)
        data = response.json()
        return data.get("products", [])

    def create_product(self, agent_name: str, name: str, price: int,
                       description: str = "", product_type: str = "digital",
                       **kwargs) -> dict[str, Any]:
        """Create a new product.

        Args:
            agent_name: Which agent is creating this product
            name: Product name
            price: Price in cents (e.g., 999 = $9.99)
            description: Product description (supports markdown)
            product_type: digital, subscription, or bundle
        """
        params = {
            **self._get_auth_params(),
            "name": name,
            "price": price,
            "description": description,
        }
        params.update(kwargs)

        conn = self.get_connection(agent_name)
        response = conn.post("/products", data=params)
        self.log_request(agent_name, "/products", method="POST",
                         status_code=response.status_code)

        data = response.json()
        product = data.get("product", {})

        logger.info(f"[{agent_name}] Created Gumroad product: {name} (${price/100:.2f})")
        return product

    def update_product(self, agent_name: str, product_id: str,
                       **updates) -> dict[str, Any]:
        """Update an existing product."""
        params = {**self._get_auth_params(), **updates}
        conn = self.get_connection(agent_name)
        response = conn.put(f"/products/{product_id}", data=params)
        self.log_request(agent_name, f"/products/{product_id}", method="PUT",
                         status_code=response.status_code)
        return response.json().get("product", {})

    def delete_product(self, agent_name: str, product_id: str) -> bool:
        """Delete a product."""
        conn = self.get_connection(agent_name)
        response = conn.delete(
            f"/products/{product_id}",
            params=self._get_auth_params(),
        )
        self.log_request(agent_name, f"/products/{product_id}", method="DELETE",
                         status_code=response.status_code)
        return response.json().get("success", False)

    # ── Sales ─────────────────────────────────────────────────

    def list_sales(self, agent_name: str, product_id: str = "",
                   after: str = "", before: str = "",
                   page: int = 1) -> dict[str, Any]:
        """List sales, optionally filtered by product and date range."""
        params = {**self._get_auth_params(), "page": page}
        if product_id:
            params["product_id"] = product_id
        if after:
            params["after"] = after
        if before:
            params["before"] = before

        conn = self.get_connection(agent_name)
        response = conn.get("/sales", params=params)
        self.log_request(agent_name, "/sales", status_code=response.status_code)
        return response.json()

    def get_sale(self, agent_name: str, sale_id: str) -> dict[str, Any]:
        """Get details of a specific sale."""
        conn = self.get_connection(agent_name)
        response = conn.get(f"/sales/{sale_id}", params=self._get_auth_params())
        self.log_request(agent_name, f"/sales/{sale_id}",
                         status_code=response.status_code)
        return response.json().get("sale", {})

    # ── Subscribers ───────────────────────────────────────────

    def list_subscribers(self, agent_name: str,
                         product_id: str) -> list[dict[str, Any]]:
        """List subscribers for a subscription product."""
        conn = self.get_connection(agent_name)
        response = conn.get(
            f"/products/{product_id}/subscribers",
            params=self._get_auth_params(),
        )
        self.log_request(agent_name, f"/products/{product_id}/subscribers",
                         status_code=response.status_code)
        return response.json().get("subscribers", [])

    # ── Revenue Summary ───────────────────────────────────────

    def get_revenue_summary(self, agent_name: str) -> dict[str, Any]:
        """Get a revenue summary across all products."""
        products = self.list_products(agent_name)
        total_revenue = Decimal("0")
        total_sales = 0
        product_stats = []

        for product in products:
            revenue = Decimal(str(product.get("sales_usd_cents", 0))) / 100
            sales = product.get("sales_count", 0)
            total_revenue += revenue
            total_sales += sales
            product_stats.append({
                "name": product.get("name", ""),
                "id": product.get("id", ""),
                "revenue_usd": revenue,
                "sales": sales,
                "price_usd": Decimal(str(product.get("price", 0))) / 100,
            })

        return {
            "total_revenue_usd": total_revenue,
            "total_sales": total_sales,
            "products": product_stats,
        }
