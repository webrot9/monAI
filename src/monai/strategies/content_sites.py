"""Content Sites strategy agent — SEO blogs and affiliate content.

Creates and manages content websites targeting low-competition long-tail keywords.
Revenue via ads and affiliate links. Slow burn but high scalability.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class ContentSiteAgent(BaseAgent):
    name = "content_sites"
    description = (
        "Creates and manages SEO content sites. Targets low-competition "
        "long-tail keywords, produces high-quality articles, and monetizes "
        "via affiliate links and ad networks."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.sites_dir = config.data_dir / "content_sites"
        self.sites_dir.mkdir(parents=True, exist_ok=True)

    def _get_content_statuses(self) -> dict[str, int]:
        """Count content pieces by status."""
        statuses: dict[str, int] = {}
        for path in self.sites_dir.glob("*.json"):
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
            return ["research_keywords"]
        if statuses.get("researched", 0) > 0:
            return ["create_article"]
        if statuses.get("draft", 0) > 0:
            return ["review_content"]
        if statuses.get("reviewed", 0) > 0:
            return ["find_affiliate_programs"]

        # All content published — research more keywords
        return ["research_keywords"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting content sites cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_keywords":
                results["keywords"] = self._research_keywords()
            elif step == "create_article":
                results["article"] = self._create_article()
            elif step == "review_content":
                results["content_review"] = self._review_content()
            elif step == "find_affiliate_programs":
                results["affiliates"] = self._find_affiliate_programs()
            elif step == "plan_new_site":
                results["new_site"] = self._plan_new_site()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_keywords(self) -> dict[str, Any]:
        """Find low-competition long-tail keywords using REAL web data."""
        self.log_action("keyword_research", "Fetching real keyword data from the web")

        # Pull trending topics from Google Trends
        trends_data = self.browse_and_extract(
            "https://trends.google.com/trending?geo=US",
            "Extract all trending search topics and queries visible on this page. "
            "For each, include the topic name/query and any category or volume "
            "indicators shown. Only include REAL data visible on the page. "
            "Do NOT make up any information. "
            "Return as JSON: {\"trends\": [{\"query\": str, \"category\": str, "
            "\"volume_indicator\": str}]}"
        )

        # Search for low-competition keyword opportunities via free SEO tools
        keyword_data = self.search_web(
            "low competition long tail keywords with buying intent 2026",
            "Extract any keyword ideas, search volume estimates, and competition "
            "levels mentioned. Only include REAL data visible on the page. "
            "Do NOT make up any information. "
            "Return as JSON: {\"keyword_ideas\": [{\"keyword\": str, "
            "\"volume_estimate\": str, \"competition\": str, \"source\": str}]}"
        )

        # Try Ubersuggest for additional keyword data
        ubersuggest_data = self.browse_and_extract(
            "https://neilpatel.com/ubersuggest/",
            "Extract any keyword suggestions, search volume data, SEO difficulty "
            "scores, and content ideas shown on this page. Only include REAL data "
            "visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"suggestions\": [{\"keyword\": str, "
            "\"volume\": str, \"seo_difficulty\": str}]}"
        )

        # Now use LLM to PLAN which keywords to target based on the real data
        raw_data = {
            "trends": trends_data,
            "keyword_ideas": keyword_data,
            "ubersuggest": ubersuggest_data,
        }
        keywords = self.think_json(
            "Based on the following REAL keyword research data from the web, "
            "select the 10 best low-competition long-tail keywords to target.\n\n"
            f"Raw research data:\n{json.dumps(raw_data, default=str)[:3000]}\n\n"
            "Focus on:\n"
            "- 'How to' queries with buying intent\n"
            "- 'Best X for Y' comparison queries\n"
            "- Problems people search for solutions to\n"
            "- Niches where affiliate programs exist\n\n"
            "IMPORTANT: Only recommend keywords that are supported by the real "
            "data above. Do not invent keywords.\n\n"
            "Return: {\"keywords\": [{\"keyword\": str, \"source\": str, "
            "\"search_volume_estimate\": str, \"competition\": \"low\"|\"medium\", "
            "\"monetization\": str, \"article_angle\": str}]}"
        )
        self.share_knowledge(
            "opportunity", "keyword_research",
            json.dumps(keywords.get("keywords", []))[:1000],
            tags=["seo", "keywords", "content"],
        )
        return keywords

    def _create_article(self) -> dict[str, Any]:
        """Create an SEO-optimized article for a target keyword."""
        # Pick a keyword to write about
        target = self.think_json(
            "Pick the best keyword to write an article about right now. "
            "Consider search volume, competition, and monetization potential. "
            "Return: {\"keyword\": str, \"title\": str, \"outline\": [str], "
            "\"target_word_count\": int, \"affiliate_opportunities\": [str]}"
        )

        keyword = target.get("keyword", "")
        outline = target.get("outline", [])

        # Write each section
        sections = []
        for heading in outline:
            section = self.llm.chat(
                [
                    {"role": "system", "content": (
                        "You are an expert content writer specializing in SEO. "
                        "Write detailed, helpful content that genuinely answers "
                        "the reader's question. Include specific examples, data points, "
                        "and actionable advice. NO filler. NO generic statements."
                    )},
                    {"role": "user", "content": (
                        f"Article keyword: {keyword}\n"
                        f"Write this section: {heading}\n"
                        "Make it detailed, specific, and valuable."
                    )},
                ],
                model=self.config.llm.model,
            )
            sections.append({"heading": heading, "content": section})

        # Save article
        article_data = {
            "target": target,
            "sections": sections,
            "status": "draft",
        }
        safe_title = "".join(
            c if c.isalnum() or c in " -_" else "" for c in keyword
        ).strip()[:50]
        article_path = self.sites_dir / f"article_{safe_title}.json"
        article_path.write_text(json.dumps(article_data, indent=2))

        self.log_action("article_created", keyword, f"{len(sections)} sections")
        return {"keyword": keyword, "sections": len(sections)}

    def _review_content(self) -> dict[str, Any]:
        """Quality gate: review draft articles before publishing."""
        for path in self.sites_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("status") != "draft":
                continue

            name = data.get("target", {}).get("keyword",
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

    def _find_affiliate_programs(self) -> dict[str, Any]:
        """Research affiliate programs using REAL data from affiliate networks."""
        self.log_action("affiliate_research", "Browsing real affiliate networks")

        # Browse ShareASale for real programs
        shareasale_data = self.browse_and_extract(
            "https://www.shareasale.com/info/",
            "Extract any affiliate program listings, merchant names, commission "
            "rates, categories, and program details shown on this page. "
            "Only include REAL data visible on the page. Do NOT make up any "
            "information. Return as JSON: {\"programs\": [{\"name\": str, "
            "\"commission_rate\": str, \"category\": str, \"details\": str}]}"
        )

        # Browse CJ Affiliate for real programs
        cj_data = self.browse_and_extract(
            "https://www.cj.com/",
            "Extract any affiliate program listings, advertiser names, commission "
            "structures, and categories shown on this page. Only include REAL data "
            "visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"programs\": [{\"name\": str, "
            "\"commission_rate\": str, \"category\": str, \"details\": str}]}"
        )

        # Search for high-commission affiliate programs
        search_data = self.search_web(
            "best high commission affiliate programs 2026 content sites",
            "Extract affiliate program names, commission rates, cookie durations, "
            "niches, and signup URLs mentioned. Only include REAL data visible on "
            "the page. Do NOT make up any information. "
            "Return as JSON: {\"programs\": [{\"name\": str, "
            "\"commission_rate\": str, \"cookie_days\": str, \"niche\": str, "
            "\"signup_url\": str}]}"
        )

        # Use LLM to select the best programs from real data
        raw_data = {
            "shareasale": shareasale_data,
            "cj": cj_data,
            "web_search": search_data,
        }
        programs = self.think_json(
            "Based on the following REAL affiliate program data from the web, "
            "select the 5 best programs for a content site.\n\n"
            f"Raw research data:\n{json.dumps(raw_data, default=str)[:3000]}\n\n"
            "Focus on programs with:\n"
            "- Good commission rates (>5%)\n"
            "- Cookie duration >30 days\n"
            "- Reputable brands\n"
            "- Products people actually buy\n\n"
            "IMPORTANT: Only include programs that appeared in the real data above. "
            "Do not invent programs.\n\n"
            "Return: {\"programs\": [{\"name\": str, \"commission_rate\": str, "
            "\"cookie_days\": int, \"niche\": str, \"signup_url\": str, "
            "\"avg_order_value\": str, \"source\": str}]}"
        )
        self.share_knowledge(
            "opportunity", "affiliate_programs",
            json.dumps(programs.get("programs", []))[:1000],
            tags=["affiliate", "monetization"],
        )
        return programs

    def _plan_new_site(self) -> dict[str, Any]:
        """Plan a new content site using real keyword data for validation."""
        # Gather real market data to inform the plan
        market_data = self.search_web(
            "profitable blog niches low competition 2026",
            "Extract any niche ideas, traffic estimates, monetization strategies, "
            "and competition assessments mentioned. Only include REAL data visible "
            "on the page. Do NOT make up any information. "
            "Return as JSON: {\"niches\": [{\"niche\": str, \"competition\": str, "
            "\"monetization\": str, \"notes\": str}]}"
        )

        plan = self.think_json(
            "Plan a new content site. Use the following REAL market research "
            "data to inform your plan:\n\n"
            f"Market data:\n{json.dumps(market_data, default=str)[:2000]}\n\n"
            "Consider:\n"
            "- What niche has demand but low competition?\n"
            "- Can it be monetized with affiliates + ads?\n"
            "- Can we produce 20+ articles for it?\n"
            "- What's the estimated monthly traffic potential?\n\n"
            "IMPORTANT: Base your niche selection on the real data above.\n\n"
            "Return: {\"niche\": str, \"domain_suggestions\": [str], "
            "\"content_pillars\": [str], \"monetization_plan\": str, "
            "\"initial_articles\": int, \"estimated_monthly_traffic\": str, "
            "\"time_to_first_revenue_months\": int}"
        )
        return plan
