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

    def _get_content_statuses(self) -> dict[str, int]:
        """Count content pieces by status."""
        statuses: dict[str, int] = {}
        for path in self.content_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                s = data.get("status", "unknown")
                statuses[s] = statuses.get(s, 0) + 1
            except (json.JSONDecodeError, OSError):
                continue
        return statuses

    def plan(self) -> list[str]:
        statuses = self._get_content_statuses()

        # Deterministic progression
        if not statuses:
            return ["research_programs"]
        if statuses.get("researched", 0) > 0:
            return ["write_review"]
        if statuses.get("draft", 0) > 0:
            return ["review_content"]
        if statuses.get("reviewed", 0) > 0:
            return ["write_comparison"]  # Create more content types

        # All content published — research new programs
        return ["research_programs"]

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
            elif step == "review_content":
                results["content_review"] = self._review_content()
            elif step == "write_comparison":
                results["comparison"] = self._write_comparison()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_programs(self) -> dict[str, Any]:
        """Find high-commission affiliate programs using REAL web data."""
        self.log_action("program_research", "Browsing real affiliate networks")

        # Browse ShareASale for real high-commission programs
        shareasale_data = self.browse_and_extract(
            "https://www.shareasale.com/info/",
            "Extract any affiliate program listings, merchant names, commission "
            "rates, categories, cookie durations, and program details shown on "
            "this page. Only include REAL data visible on the page. Do NOT make "
            "up any information. Return as JSON: {\"programs\": [{\"name\": str, "
            "\"commission\": str, \"category\": str, \"cookie_days\": str, "
            "\"details\": str}]}"
        )

        # Browse CJ Affiliate for real programs
        cj_data = self.browse_and_extract(
            "https://www.cj.com/",
            "Extract any affiliate program listings, advertiser names, commission "
            "structures, categories, and details shown on this page. Only include "
            "REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"programs\": [{\"name\": str, "
            "\"commission\": str, \"category\": str, \"details\": str}]}"
        )

        # Search for high-commission programs in profitable niches
        search_data = self.search_web(
            "highest commission affiliate programs SaaS hosting VPN 2026",
            "Extract affiliate program names, commission rates, whether commissions "
            "are recurring, cookie durations, average sale values, and signup URLs. "
            "Only include REAL data visible on the page. Do NOT make up any "
            "information. Return as JSON: {\"programs\": [{\"name\": str, "
            "\"product\": str, \"commission\": str, \"recurring\": str, "
            "\"cookie_days\": str, \"avg_sale_value\": str, \"signup_url\": str}]}"
        )

        # Search specifically for recurring commission programs
        recurring_data = self.search_web(
            "best recurring commission affiliate programs 2026",
            "Extract program names, products, recurring commission rates, and "
            "any details about payment structure. Only include REAL data visible "
            "on the page. Do NOT make up any information. "
            "Return as JSON: {\"programs\": [{\"name\": str, \"product\": str, "
            "\"commission\": str, \"recurring\": str, \"niche\": str}]}"
        )

        # Use LLM to select the best programs from real data
        raw_data = {
            "shareasale": shareasale_data,
            "cj": cj_data,
            "web_search": search_data,
            "recurring": recurring_data,
        }
        programs = self.think_json(
            "Based on the following REAL affiliate program data from the web, "
            "select the 5 best high-commission programs.\n\n"
            f"Raw research data:\n{json.dumps(raw_data, default=str)[:4000]}\n\n"
            "Focus on:\n"
            "- Commission rate >10% or >$20 per sale\n"
            "- Recurring commissions (SaaS products)\n"
            "- Products with genuine value (no scams)\n"
            "- Growing markets\n\n"
            "Categories: SaaS tools, hosting, VPNs, online courses, "
            "financial products, productivity tools, design software.\n\n"
            "IMPORTANT: Only include programs that appeared in the real data "
            "above. Do not invent programs.\n\n"
            "Return: {\"programs\": [{\"name\": str, \"product\": str, "
            "\"commission\": str, \"cookie_days\": int, \"recurring\": bool, "
            "\"avg_sale_value\": float, \"niche\": str, \"signup_url\": str, "
            "\"source\": str}]}"
        )
        self.share_knowledge(
            "opportunity", "affiliate_programs",
            json.dumps(programs.get("programs", []))[:1000],
            tags=["affiliate", "programs"],
        )
        return programs

    def _research_products(self) -> dict[str, Any]:
        """Research specific products to review using REAL web data."""
        self.log_action("product_research", "Browsing real product listings")

        # First, decide what niche to research based on existing knowledge
        niche_pick = self.think_json(
            "Pick a profitable niche for affiliate product reviews. "
            "Choose from: SaaS tools, web hosting, VPNs, online course platforms, "
            "productivity software, design tools, email marketing tools.\n\n"
            "Return: {\"niche\": str, \"search_queries\": [str]}"
        )
        niche = niche_pick.get("niche", "SaaS tools")
        search_queries = niche_pick.get("search_queries", [f"best {niche} 2026"])

        # Search for real products in this niche
        product_search = self.search_web(
            search_queries[0] if search_queries else f"best {niche} 2026",
            "Extract product names, pricing, key features, and any affiliate "
            "program details mentioned. Only include REAL data visible on the "
            "page. Do NOT make up any information. "
            "Return as JSON: {\"products\": [{\"name\": str, \"price\": str, "
            "\"key_features\": [str], \"affiliate_info\": str}]}"
        )

        # Browse Amazon for real product listings in this niche
        amazon_data = self.browse_and_extract(
            f"https://www.amazon.com/s?k={niche.replace(' ', '+')}",
            "Extract product names, prices, ratings, number of reviews, and any "
            "key features shown. Only include REAL data visible on the page. "
            "Do NOT make up any information. "
            "Return as JSON: {\"products\": [{\"name\": str, \"price\": str, "
            "\"rating\": str, \"num_reviews\": str, \"features\": [str]}]}"
        )

        # Browse a review/comparison site for expert opinions
        review_data = self.search_web(
            f"{niche} comparison review 2026",
            "Extract product names, ratings, pros, cons, and pricing mentioned "
            "in product reviews and comparisons. Only include REAL data visible "
            "on the page. Do NOT make up any information. "
            "Return as JSON: {\"products\": [{\"name\": str, \"rating\": str, "
            "\"pros\": [str], \"cons\": [str], \"price\": str}]}"
        )

        # Use LLM to synthesize real data into actionable product list
        raw_data = {
            "niche": niche,
            "product_search": product_search,
            "amazon": amazon_data,
            "reviews": review_data,
        }
        products = self.think_json(
            "Based on the following REAL product research data, select 3-5 "
            "products to create comparison content for.\n\n"
            f"Raw research data:\n{json.dumps(raw_data, default=str)[:4000]}\n\n"
            "The products should be:\n"
            "- Real products that appeared in the data above\n"
            "- In the same category (so we can compare them)\n"
            "- Have affiliate programs or are on Amazon\n"
            "- Products people actively search for reviews of\n\n"
            "IMPORTANT: Only include products that appeared in the real data "
            "above. Do not invent products.\n\n"
            "Return: {\"niche\": str, \"products\": [{\"name\": str, "
            "\"category\": str, \"price\": str, \"key_features\": [str], "
            "\"affiliate_program\": str, \"commission\": str, \"source\": str}]}"
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

    def _review_content(self) -> dict[str, Any]:
        """Quality gate: review draft content before publishing."""
        for path in self.content_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("status") != "draft":
                continue

            name = data.get("target", {}).get("product_name",
                   data.get("target", {}).get("title", path.stem))

            result = self.reviewer.review_product(
                strategy=self.name,
                product_name=name,
                product_data=data,
                product_type="content",
            )

            if result.verdict == "approved":
                data["status"] = "reviewed"
                data["review"] = result.to_dict()
                path.write_text(json.dumps(data, indent=2))
                self.log_action("content_reviewed", f"{name}: APPROVED")
            elif result.verdict == "rejected":
                data["status"] = "researched"
                data["review"] = result.to_dict()
                path.write_text(json.dumps(data, indent=2))
                self.log_action("content_rejected", f"{name}: REJECTED")
            else:
                revised = self.reviewer.revise_product(data, result, "content")
                data["status"] = "reviewed"
                data["review"] = result.to_dict()
                data["revised_content"] = revised
                path.write_text(json.dumps(data, indent=2))
                self.log_action("content_revised", f"{name}: REVISED")

            return result.to_dict()

        return {"status": "no_content_to_review"}

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
