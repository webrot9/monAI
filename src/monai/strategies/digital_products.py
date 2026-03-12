"""Digital products strategy agent.

Creates and sells digital products on REAL marketplaces (Gumroad, etc.).
Uses the Gumroad integration for actual product listing and sales tracking.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.integrations.gumroad import GumroadIntegration
from monai.utils.llm import LLM


class DigitalProductsAgent(BaseAgent):
    name = "digital_products"
    description = (
        "Creates and sells digital products — ebooks, templates, prompt packs, "
        "guides, tools. Lists them on REAL marketplaces and tracks real sales."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.products_dir = config.data_dir / "products"
        self.products_dir.mkdir(parents=True, exist_ok=True)
        self._gumroad = None  # Lazy-loaded

    @property
    def gumroad(self) -> GumroadIntegration:
        """Lazy-load Gumroad integration with stored credentials."""
        if self._gumroad is None:
            access_token = self.identity.get_api_key("gumroad") or ""
            self._gumroad = GumroadIntegration(
                self.config, self.db, access_token=access_token,
            )
        return self._gumroad

    def _ensure_gumroad_account(self) -> bool:
        """Ensure we have a Gumroad account with API access.

        Returns True if Gumroad is configured and accessible.
        """
        # Check if we already have a working token
        token = self.identity.get_api_key("gumroad")
        if token:
            health = self.gumroad.health_check()
            if health.get("status") == "ok":
                return True

        # No token — register on Gumroad and get API access
        self.log_action("gumroad_setup", "Setting up Gumroad account and API access")
        account_result = self.ensure_platform_account("gumroad")

        if account_result.get("status") in ("exists", "completed"):
            # Now get the API token via the developer settings
            result = self.execute_task(
                "Navigate to https://app.gumroad.com/settings/advanced#application-form "
                "and create an API application to get an access token. "
                "If already created, go to https://app.gumroad.com/settings/advanced "
                "and copy the access token. "
                "Return the access token in the result.",
            )
            if result.get("status") == "completed":
                token_value = result.get("result", "")
                if isinstance(token_value, dict):
                    token_value = token_value.get("access_token", token_value.get("token", ""))
                if token_value and isinstance(token_value, str) and len(token_value) > 10:
                    self.identity.store_api_key("gumroad", "gumroad_access_token", token_value)
                    # Reset the cached integration to use new token
                    self._gumroad = None
                    self.log_action("gumroad_setup", "Gumroad API token acquired")
                    return True

        self.log_action("gumroad_setup_failed", "Could not set up Gumroad API access")
        return False

    def _get_product_statuses(self) -> dict[str, int]:
        """Count products by status from JSON files."""
        statuses: dict[str, int] = {}
        for path in self.products_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                s = data.get("status", "unknown")
                statuses[s] = statuses.get(s, 0) + 1
            except (json.JSONDecodeError, OSError):
                continue
        return statuses

    def plan(self) -> list[str]:
        """Deterministic product pipeline progression."""
        statuses = self._get_product_statuses()

        # Always advance the pipeline
        if not statuses:
            return ["research_niches"]
        if statuses.get("researched", 0) > 0:
            return ["create_product"]
        if statuses.get("created", 0) > 0:
            return ["review_product"]
        if statuses.get("reviewed", 0) > 0:
            return ["list_product"]
        if statuses.get("listed", 0) > 0:
            return ["check_sales"]

        # All products listed/sold — start new cycle
        self.log_action("plan", "All products at final stage, researching new niches")
        return ["research_niches"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting digital products cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_niches":
                results["research"] = self._research_niches()
            elif step == "create_product":
                results["create"] = self._create_product()
            elif step == "review_product":
                results["review"] = self._review_product()
            elif step == "list_product":
                results["list"] = self._list_product()
            elif step == "check_sales":
                results["sales"] = self._check_sales()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Find profitable niches by browsing REAL marketplace data."""
        # Browse real Gumroad trending/discover to see what sells
        trending_result = self.browse_and_extract(
            "https://gumroad.com/discover",
            "Extract the top trending product categories and specific products.\n"
            "For each, get: category, product name, creator, price, number of ratings/sales if visible.\n"
            "Return JSON: {\"trending\": [{\"category\": str, \"product_name\": str, "
            "\"creator\": str, \"price\": str, \"ratings\": str}]}\n"
            "Only include REAL products visible on the page.",
        )

        # Also check Product Hunt for digital product trends
        ph_result = self.browse_and_extract(
            "https://www.producthunt.com/topics/digital-products",
            "Extract trending digital products. For each get:\n"
            "name, tagline, category, upvotes.\n"
            "Return JSON: {\"products\": [{\"name\": str, \"tagline\": str, "
            "\"category\": str, \"upvotes\": int}]}\n"
            "Only include REAL products visible on the page.",
        )

        # Combine real data and use LLM to identify opportunity gaps
        real_data = {
            "gumroad_trending": trending_result.get("result", {}),
            "product_hunt": ph_result.get("result", {}),
        }

        niches = self.think_json(
            "Based on this REAL marketplace data, identify 5 profitable niches "
            "for digital products I can create and sell. Focus on gaps where "
            "demand exists but competition is moderate.\n\n"
            f"Real marketplace data:\n{json.dumps(real_data, default=str)[:3000]}\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"product_type\": str, "
            "\"estimated_price\": float, \"reasoning\": str}]}",
        )
        # Persist top niche as actionable product file for pipeline progression
        niche_list = niches.get("niches", [])
        saved = 0
        for niche in niche_list[:2]:
            name = niche.get("niche", niche.get("product_type", "untitled"))
            safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip()
            if not safe_name:
                continue
            path = self.products_dir / f"{safe_name}.json"
            if not path.exists():
                path.write_text(json.dumps({
                    "research": niche,
                    "status": "researched",
                }, indent=2))
                saved += 1

        self.log_action("research_niches",
                        f"Found {len(niche_list)} niches, saved {saved} for creation")
        return niches

    def _create_product(self) -> dict[str, Any]:
        """Create a digital product from a researched niche."""
        # Find a researched niche to advance
        research_data = {}
        product_path = None
        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") == "researched":
                research_data = data.get("research", {})
                product_path = path
                break

        product_spec = self.think_json(
            f"Design a specific digital product based on this research:\n"
            f"Niche: {research_data.get('niche', 'unknown')}\n"
            f"Product type: {research_data.get('product_type', 'ebook')}\n"
            f"Estimated price: ${research_data.get('estimated_price', 9.99)}\n"
            f"Reasoning: {research_data.get('reasoning', '')}\n\n"
            "Create something I can generate with AI and sell immediately. "
            "Return: {\"title\": str, \"type\": str, \"description\": str, "
            "\"target_audience\": str, \"price\": float, \"sections\": [str]}"
        )

        title = product_spec.get("title", "Untitled Product")
        sections = product_spec.get("sections", [])

        # Generate the actual product content
        content_parts = []
        for section in sections:
            part = self.llm.chat(
                [
                    {"role": "system", "content": (
                        "You are creating a premium digital product. Write detailed, "
                        "actionable, high-value content that people would gladly pay for. "
                        "The quality must be indistinguishable from expert human work."
                    )},
                    {"role": "user", "content": (
                        f"Product: {title}\nWrite this section: {section}\n"
                        "Make it comprehensive, practical, and professionally written."
                    )},
                ],
                model=self.config.llm.model,
            )
            content_parts.append({"section": section, "content": part})

        # Save product — update existing research file or create new
        if product_path and product_path.exists():
            existing_data = json.loads(product_path.read_text())
            existing_data["spec"] = product_spec
            existing_data["content"] = content_parts
            existing_data["status"] = "created"
            product_path.write_text(json.dumps(existing_data, indent=2))
        else:
            safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip()
            product_path = self.products_dir / f"{safe_title}.json"
            product_data = {
                "spec": product_spec,
                "content": content_parts,
                "status": "created",
            }
            product_path.write_text(json.dumps(product_data, indent=2))

        self.record_expense(
            0.10, "api_cost", f"Created product: {title}",
        )
        self.log_action("create_product", f"Created: {title}")
        return {"product": title, "sections": len(sections)}

    def _review_product(self) -> dict[str, Any]:
        """Quality gate: review created product before listing."""
        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "created":
                continue

            spec = data.get("spec", {})
            name = spec.get("title", path.stem)

            result = self.reviewer.review_product(
                strategy=self.name,
                product_name=name,
                product_data=data,
                product_type="digital_product",
            )

            if result.verdict == "approved":
                data["status"] = "reviewed"
                data["review"] = result.to_dict()
                # Apply humanized content if available
                if result.improved_content.get("humanized"):
                    # Replace content sections with humanized versions
                    data["humanized_listing"] = result.improved_content["humanized"]
                path.write_text(json.dumps(data, indent=2))
                self.log_action("product_reviewed", f"{name}: APPROVED (score={result.quality_score:.2f})")
            elif result.verdict == "rejected":
                data["status"] = "researched"  # Send back to creation
                data["review"] = result.to_dict()
                path.write_text(json.dumps(data, indent=2))
                self.log_action("product_review_rejected", f"{name}: REJECTED — {'; '.join(result.issues[:3])}")
            else:
                data["status"] = "reviewed"  # Proceed with notes
                data["review"] = result.to_dict()
                path.write_text(json.dumps(data, indent=2))
                self.log_action("product_reviewed", f"{name}: PASSED WITH NOTES (score={result.quality_score:.2f})")

            return result.to_dict()

        return {"status": "no_products_to_review"}

    def _list_product(self) -> dict[str, Any]:
        """List products on REAL Gumroad marketplace."""
        # Ensure Gumroad is set up
        gumroad_ready = self._ensure_gumroad_account()

        products = list(self.products_dir.glob("*.json"))
        listed = 0

        for product_path in products:
            data = json.loads(product_path.read_text())
            if data.get("status") not in ("created", "reviewed"):
                continue

            spec = data["spec"]
            title = spec.get("title", "Untitled")
            price_usd = spec.get("price", 9.99)
            price_cents = int(price_usd * 100)
            description = spec.get("description", "")

            # Generate a compelling marketplace description
            listing = self.think(
                f"Write a compelling Gumroad product listing description for:\n"
                f"Product: {title}\n"
                f"Type: {spec.get('type', 'digital product')}\n"
                f"Target audience: {spec.get('target_audience', 'professionals')}\n\n"
                "Write a description that sells. Include benefits, what's included, "
                "and a clear value proposition. Use markdown formatting."
            )

            if gumroad_ready:
                # List on REAL Gumroad via API
                try:
                    gumroad_product = self.gumroad.create_product(
                        agent_name=self.name,
                        name=title,
                        price=price_cents,
                        description=listing,
                    )
                    data["gumroad_id"] = gumroad_product.get("id", "")
                    data["gumroad_url"] = gumroad_product.get("short_url", "")
                    data["listing"] = listing
                    data["status"] = "listed"
                    product_path.write_text(json.dumps(data, indent=2))
                    listed += 1
                    self.log_action("list_product",
                                    f"Listed on Gumroad: {title} (${price_usd:.2f})")
                except Exception as e:
                    self.log_action("list_product_failed",
                                    f"Gumroad API error for {title}: {e}")
                    self.learn_from_error(e, f"Listing {title} on Gumroad")
            else:
                # Fallback: use browser to list on Gumroad
                result = self.platform_action(
                    "gumroad",
                    f"Create a new product listing:\n"
                    f"Name: {title}\n"
                    f"Price: ${price_usd:.2f}\n"
                    f"Description: {listing[:1000]}\n\n"
                    "Navigate to https://app.gumroad.com/products/new and fill in the form.",
                    context=listing,
                )
                if result.get("status") == "completed":
                    data["listing"] = listing
                    data["status"] = "listed"
                    product_path.write_text(json.dumps(data, indent=2))
                    listed += 1

        return {"products_listed": listed}

    def _check_sales(self) -> dict[str, Any]:
        """Check real sales data from Gumroad."""
        if not self._ensure_gumroad_account():
            return {"status": "gumroad_not_configured"}

        try:
            summary = self.gumroad.get_revenue_summary(self.name)

            # Record any new revenue
            for product in summary.get("products", []):
                if product.get("revenue_usd", 0) > 0:
                    self.log_action("sales_data",
                                    f"{product['name']}: ${product['revenue_usd']:.2f} "
                                    f"({product['sales']} sales)")

            if summary.get("total_revenue_usd", 0) > 0:
                self.record_revenue(
                    summary["total_revenue_usd"],
                    "product_sales",
                    f"Gumroad sales: {summary['total_sales']} total",
                )

            return summary
        except Exception as e:
            self.log_action("check_sales_failed", str(e))
            return {"status": "error", "error": str(e)}
