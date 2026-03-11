"""Lemon Squeezy integration — SaaS and digital product sales.

Lemon Squeezy API: https://docs.lemonsqueezy.com/api
Used by: saas, digital_products, subscription strategies.

Each strategy agent gets its own connection with independent rate limiting.
"""

from __future__ import annotations

import logging
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.integrations.base import PlatformIntegration, RateLimitConfig

logger = logging.getLogger(__name__)


class LemonSqueezyIntegration(PlatformIntegration):
    """Lemon Squeezy API integration for SaaS and digital products."""

    platform_name = "lemonsqueezy"
    base_url = "https://api.lemonsqueezy.com/v1"
    default_rate_limit = RateLimitConfig(
        requests_per_minute=60,
        requests_per_day=5000,
        retry_after_seconds=60,
        max_retries=3,
    )

    def __init__(self, config: Config, db: Database, api_key: str = "",
                 store_id: str = ""):
        super().__init__(config, db)
        self._api_key = api_key
        self._store_id = store_id

    def _get_auth_headers(self) -> dict[str, str]:
        """Lemon Squeezy uses Bearer token in Authorization header."""
        if self._api_key:
            return {
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/vnd.api+json",
            }
        return {}

    def _get_connection(self, agent_name: str):
        """Get a connection with LS-specific auth headers."""
        conn = self.get_connection(agent_name, api_key=self._api_key)
        # Ensure JSON API headers are set on the underlying client.
        if self._api_key:
            conn.client.headers.update({
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/vnd.api+json",
            })
        return conn

    def health_check(self) -> dict[str, Any]:
        """Verify Lemon Squeezy API access."""
        if not self._api_key:
            return {"status": "not_configured", "platform": self.platform_name}
        try:
            conn = self._get_connection("health_check")
            response = conn.get("/users/me")
            data = response.json()
            user_attrs = data.get("data", {}).get("attributes", {})
            return {
                "status": "ok",
                "platform": self.platform_name,
                "user": user_attrs.get("name", "unknown"),
            }
        except Exception as e:
            return {"status": "error", "platform": self.platform_name, "error": str(e)}

    # ── Products ──────────────────────────────────────────────

    def list_products(self, agent_name: str) -> list[dict[str, Any]]:
        """List all products in the store."""
        conn = self._get_connection(agent_name)
        params = {}
        if self._store_id:
            params["filter[store_id]"] = self._store_id
        response = conn.get("/products", params=params)
        self.log_request(agent_name, "/products", status_code=response.status_code)
        data = response.json()
        return data.get("data", [])

    def create_product(self, agent_name: str, name: str, price: int,
                       description: str = "", **kwargs) -> dict[str, Any]:
        """Create a new product with its default variant.

        Args:
            agent_name: Which agent is creating this product.
            name: Product name.
            price: Price in cents (e.g., 999 = $9.99).
            description: Product description (HTML supported).
        """
        store_id = kwargs.pop("store_id", self._store_id)
        product_body = {
            "data": {
                "type": "products",
                "attributes": {
                    "name": name,
                    "description": description or name,
                },
                "relationships": {
                    "store": {
                        "data": {"type": "stores", "id": str(store_id)},
                    },
                },
            },
        }

        conn = self._get_connection(agent_name)
        response = conn.post("/products", json=product_body)
        self.log_request(agent_name, "/products", method="POST",
                         status_code=response.status_code)

        data = response.json()
        product = data.get("data", {})
        product_id = product.get("id", "")

        # Fetch default variant and update its price.
        variant = self._update_default_variant_price(
            agent_name, product_id, price,
        )

        logger.info(
            f"[{agent_name}] Created LS product: {name} (${price / 100:.2f})"
        )
        return {"product": product, "variant": variant}

    def _update_default_variant_price(
        self, agent_name: str, product_id: str, price_cents: int,
    ) -> dict[str, Any]:
        """Fetch the auto-created variant for a product and set its price."""
        conn = self._get_connection(agent_name)

        response = conn.get("/variants", params={"filter[product_id]": product_id})
        self.log_request(agent_name, "/variants", status_code=response.status_code)
        variants = response.json().get("data", [])
        if not variants:
            return {}

        variant = variants[0]
        variant_id = variant.get("id", "")

        if price_cents:
            variant_body = {
                "data": {
                    "type": "variants",
                    "id": str(variant_id),
                    "attributes": {
                        "price": price_cents,
                    },
                },
            }
            response = conn.request("PATCH", f"/variants/{variant_id}",
                                    json=variant_body)
            self.log_request(agent_name, f"/variants/{variant_id}", method="PATCH",
                             status_code=response.status_code)
            variant = response.json().get("data", variant)

        return variant

    def get_product(self, agent_name: str, product_id: str) -> dict[str, Any]:
        """Get details of a specific product."""
        conn = self._get_connection(agent_name)
        response = conn.get(f"/products/{product_id}")
        self.log_request(agent_name, f"/products/{product_id}",
                         status_code=response.status_code)
        return response.json().get("data", {})

    def delete_product(self, agent_name: str, product_id: str) -> bool:
        """Delete a product."""
        conn = self._get_connection(agent_name)
        response = conn.delete(f"/products/{product_id}")
        self.log_request(agent_name, f"/products/{product_id}", method="DELETE",
                         status_code=response.status_code)
        return response.status_code == 204

    # ── Variants ─────────────────────────────────────────────

    def list_variants(self, agent_name: str,
                      product_id: str) -> list[dict[str, Any]]:
        """List variants for a product."""
        conn = self._get_connection(agent_name)
        response = conn.get("/variants", params={"filter[product_id]": product_id})
        self.log_request(agent_name, "/variants", status_code=response.status_code)
        return response.json().get("data", [])

    # ── Orders ───────────────────────────────────────────────

    def list_orders(self, agent_name: str,
                    page: int = 1) -> dict[str, Any]:
        """List orders, paginated."""
        params: dict[str, Any] = {"page[number]": page, "page[size]": 50}
        if self._store_id:
            params["filter[store_id]"] = self._store_id

        conn = self._get_connection(agent_name)
        response = conn.get("/orders", params=params)
        self.log_request(agent_name, "/orders", status_code=response.status_code)
        return response.json()

    def get_order(self, agent_name: str, order_id: str) -> dict[str, Any]:
        """Get details of a specific order."""
        conn = self._get_connection(agent_name)
        response = conn.get(f"/orders/{order_id}")
        self.log_request(agent_name, f"/orders/{order_id}",
                         status_code=response.status_code)
        return response.json().get("data", {})

    # ── Subscriptions ────────────────────────────────────────

    def list_subscriptions(self, agent_name: str,
                           page: int = 1) -> list[dict[str, Any]]:
        """List subscriptions."""
        params: dict[str, Any] = {"page[number]": page, "page[size]": 50}
        if self._store_id:
            params["filter[store_id]"] = self._store_id

        conn = self._get_connection(agent_name)
        response = conn.get("/subscriptions", params=params)
        self.log_request(agent_name, "/subscriptions",
                         status_code=response.status_code)
        return response.json().get("data", [])

    # ── Revenue Summary ──────────────────────────────────────

    def get_revenue_summary(self, agent_name: str) -> dict[str, Any]:
        """Get a revenue summary across all products."""
        products = self.list_products(agent_name)
        total_revenue = 0
        total_sales = 0
        product_stats = []

        for product in products:
            attrs = product.get("attributes", {})
            revenue = attrs.get("total_revenue", 0) / 100
            sales = attrs.get("sales_count", 0)
            total_revenue += revenue
            total_sales += sales
            product_stats.append({
                "name": attrs.get("name", ""),
                "id": product.get("id", ""),
                "revenue_usd": revenue,
                "sales": sales,
                "price_usd": attrs.get("price", 0) / 100,
            })

        return {
            "total_revenue_usd": total_revenue,
            "total_sales": total_sales,
            "products": product_stats,
        }
