"""Digital products strategy agent.

Creates and sells digital products: ebooks, templates, prompt packs, tools, guides.
Low risk, passive income potential.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class DigitalProductsAgent(BaseAgent):
    name = "digital_products"
    description = (
        "Creates and sells digital products — ebooks, templates, prompt packs, "
        "guides, tools. Identifies trending niches, creates products, and lists them."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.products_dir = config.data_dir / "products"
        self.products_dir.mkdir(parents=True, exist_ok=True)

    def plan(self) -> list[str]:
        """Plan product creation and listing cycle."""
        existing = list(self.products_dir.glob("*.json"))
        plan = self.think_json(
            f"I have {len(existing)} digital products. Plan my next actions. "
            "Return: {\"steps\": [str]}. "
            "Possible: research_niches, create_product, list_product, optimize_listings, "
            "analyze_sales, create_bundle.",
        )
        steps = plan.get("steps", ["research_niches"])
        self.log_action("plan", f"Planned {len(steps)} steps")
        return steps

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting digital products cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_niches":
                results["research"] = self._research_niches()
            elif step == "create_product":
                results["create"] = self._create_product()
            elif step == "list_product":
                results["list"] = self._list_product()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Find profitable niches for digital products."""
        niches = self.think_json(
            "Research and suggest 5 profitable niches for digital products "
            "(ebooks, templates, prompt packs, Notion templates, spreadsheet tools, etc). "
            "Focus on niches with proven demand and low competition. "
            "Return: {\"niches\": [{\"niche\": str, \"product_type\": str, "
            "\"estimated_price\": float, \"demand_score\": int, \"reasoning\": str}]}"
        )
        self.log_action("research_niches", json.dumps(niches.get("niches", []))[:500])
        return niches

    def _create_product(self) -> dict[str, Any]:
        """Create a digital product based on research."""
        # First, decide what to create
        product_spec = self.think_json(
            "Design a specific digital product to create right now. "
            "It should be something I can generate with AI and sell immediately. "
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
                        "actionable, high-value content that people would gladly pay for."
                    )},
                    {"role": "user", "content": (
                        f"Product: {title}\nWrite this section: {section}\n"
                        "Make it comprehensive, practical, and professionally written."
                    )},
                ],
                model=self.config.llm.model,
            )
            content_parts.append({"section": section, "content": part})

        # Save product
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

    def _list_product(self) -> dict[str, Any]:
        """Prepare product listing for marketplaces."""
        # Find unlisted products
        products = list(self.products_dir.glob("*.json"))
        listed = 0
        for product_path in products:
            data = json.loads(product_path.read_text())
            if data.get("status") == "created":
                spec = data["spec"]
                listing = self.think_json(
                    f"Create a compelling marketplace listing for this product. "
                    f"Product: {json.dumps(spec)}\n"
                    "Return: {\"title\": str, \"tagline\": str, \"description\": str, "
                    "\"tags\": [str], \"platforms\": [str]}"
                )
                data["listing"] = listing
                data["status"] = "listed"
                product_path.write_text(json.dumps(data, indent=2))
                listed += 1
                self.log_action("list_product", f"Listed: {spec.get('title')}")

        return {"products_listed": listed}
