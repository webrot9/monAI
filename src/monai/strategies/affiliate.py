"""Affiliate strategy agent — review and comparison content for commissions.

Creates review articles, comparison guides, and recommendation content
with affiliate links. Targets high-commission niches.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class AffiliateAgent(BaseAgent):
    name = "affiliate"
    description = (
        "Creates review and comparison content for affiliate marketing. "
        "Targets high-commission niches with genuine, detailed reviews "
        "that help people make buying decisions."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.content_dir = config.data_dir / "affiliate_content"
        self.content_dir.mkdir(parents=True, exist_ok=True)

    def plan(self) -> list[str]:
        existing = list(self.content_dir.glob("*.json"))
        plan = self.think_json(
            f"I have {len(existing)} affiliate content pieces. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_programs, research_products, write_review, "
            "write_comparison, optimize_existing, analyze_performance.",
        )
        return plan.get("steps", ["research_programs"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting affiliate cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_programs":
                results["programs"] = self._research_programs()
            elif step == "research_products":
                results["products"] = self._research_products()
            elif step == "write_review":
                results["review"] = self._write_review()
            elif step == "write_comparison":
                results["comparison"] = self._write_comparison()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_programs(self) -> dict[str, Any]:
        """Find high-commission affiliate programs."""
        programs = self.think_json(
            "Research 5 high-commission affiliate programs. Focus on:\n"
            "- Commission rate >10% or >$20 per sale\n"
            "- Recurring commissions (SaaS products)\n"
            "- Products with genuine value (no scams)\n"
            "- Growing markets\n\n"
            "Categories: SaaS tools, hosting, VPNs, online courses, "
            "financial products, productivity tools, design software.\n\n"
            "Return: {\"programs\": [{\"name\": str, \"product\": str, "
            "\"commission\": str, \"cookie_days\": int, \"recurring\": bool, "
            "\"avg_sale_value\": float, \"niche\": str, \"signup_url\": str}]}"
        )
        self.share_knowledge(
            "opportunity", "affiliate_programs",
            json.dumps(programs.get("programs", []))[:1000],
            tags=["affiliate", "programs"],
        )
        return programs

    def _research_products(self) -> dict[str, Any]:
        """Research specific products to review."""
        products = self.think_json(
            "Pick a niche and research 3-5 products to create comparison content for.\n"
            "The products should be:\n"
            "- Real, existing products\n"
            "- In the same category (so we can compare them)\n"
            "- Have affiliate programs\n"
            "- Products people actively search for reviews of\n\n"
            "Return: {\"niche\": str, \"products\": [{\"name\": str, "
            "\"category\": str, \"price\": str, \"key_features\": [str], "
            "\"affiliate_program\": str, \"commission\": str}]}"
        )
        return products

    def _write_review(self) -> dict[str, Any]:
        """Write a detailed product review."""
        # Decide what to review
        target = self.think_json(
            "Pick a product to write an in-depth review of. "
            "Choose something with high commission and search demand.\n\n"
            "Return: {\"product_name\": str, \"category\": str, "
            "\"target_keyword\": str, \"review_angle\": str, "
            "\"sections\": [str]}"
        )

        product = target.get("product_name", "Unknown")
        sections = target.get("sections", [])

        # Write each section
        parts = []
        for section in sections:
            content = self.llm.chat(
                [
                    {"role": "system", "content": (
                        "You are an expert product reviewer. Write honest, detailed reviews "
                        "that genuinely help people decide. Include specific pros AND cons. "
                        "Be opinionated — don't just list features. Say what's actually good "
                        "and what's not. Readers trust honesty over promotion."
                    )},
                    {"role": "user", "content": (
                        f"Product: {product}\n"
                        f"Section: {section}\n"
                        "Write this section of the review. Be specific and honest."
                    )},
                ],
                model=self.config.llm.model,
            )
            parts.append({"section": section, "content": content})

        # Save
        safe_name = "".join(
            c if c.isalnum() or c in " -_" else "" for c in product
        ).strip()[:50]
        path = self.content_dir / f"review_{safe_name}.json"
        path.write_text(json.dumps({
            "type": "review", "target": target,
            "sections": parts, "status": "draft",
        }, indent=2))

        self.log_action("review_written", product)
        return {"product": product, "sections": len(parts)}

    def _write_comparison(self) -> dict[str, Any]:
        """Write a product comparison (e.g., 'X vs Y vs Z')."""
        target = self.think_json(
            "Design a product comparison article. Pick 3-4 products in the same "
            "category and plan a detailed comparison.\n\n"
            "Return: {\"title\": str, \"products\": [str], \"category\": str, "
            "\"comparison_criteria\": [str], \"target_keyword\": str, "
            "\"winner_and_why\": str}"
        )

        products = target.get("products", [])
        criteria = target.get("comparison_criteria", [])

        # Write comparison content
        comparison = self.llm.chat(
            [
                {"role": "system", "content": (
                    "You are a tech reviewer writing a thorough product comparison. "
                    "Be objective and honest. Include a clear recommendation at the end "
                    "with reasoning. Use tables where appropriate."
                )},
                {"role": "user", "content": (
                    f"Write a comparison: {target.get('title', '')}\n"
                    f"Products: {', '.join(products)}\n"
                    f"Compare on: {', '.join(criteria)}\n"
                    "Include: intro, individual summaries, comparison table, "
                    "and final recommendation."
                )},
            ],
            model=self.config.llm.model,
        )

        title = target.get("title", "Comparison")
        safe_name = "".join(
            c if c.isalnum() or c in " -_" else "" for c in title
        ).strip()[:50]
        path = self.content_dir / f"comparison_{safe_name}.json"
        path.write_text(json.dumps({
            "type": "comparison", "target": target,
            "content": comparison, "status": "draft",
        }, indent=2))

        self.log_action("comparison_written", title)
        return {"title": title, "products_compared": len(products)}
