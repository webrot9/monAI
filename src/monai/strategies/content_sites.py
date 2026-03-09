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

    def plan(self) -> list[str]:
        existing = list(self.sites_dir.glob("*.json"))
        plan = self.think_json(
            f"I manage {len(existing)} content sites. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_keywords, create_article, optimize_seo, "
            "find_affiliate_programs, analyze_performance, plan_new_site.",
        )
        return plan.get("steps", ["research_keywords"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting content sites cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_keywords":
                results["keywords"] = self._research_keywords()
            elif step == "create_article":
                results["article"] = self._create_article()
            elif step == "find_affiliate_programs":
                results["affiliates"] = self._find_affiliate_programs()
            elif step == "plan_new_site":
                results["new_site"] = self._plan_new_site()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_keywords(self) -> dict[str, Any]:
        """Find low-competition long-tail keywords to target."""
        keywords = self.think_json(
            "Research 10 low-competition long-tail keywords for content sites. "
            "Focus on:\n"
            "- 'How to' queries with buying intent\n"
            "- 'Best X for Y' comparison queries\n"
            "- Problems people search for solutions to\n"
            "- Niches where affiliate programs exist\n\n"
            "Return: {\"keywords\": [{\"keyword\": str, \"search_volume_estimate\": str, "
            "\"competition\": \"low\"|\"medium\", \"monetization\": str, "
            "\"article_angle\": str}]}"
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

    def _find_affiliate_programs(self) -> dict[str, Any]:
        """Research affiliate programs to monetize content."""
        programs = self.think_json(
            "Research 5 affiliate programs suitable for a content site. "
            "Focus on programs with:\n"
            "- Good commission rates (>5%)\n"
            "- Cookie duration >30 days\n"
            "- Reputable brands\n"
            "- Products people actually buy\n\n"
            "Return: {\"programs\": [{\"name\": str, \"commission_rate\": str, "
            "\"cookie_days\": int, \"niche\": str, \"signup_url\": str, "
            "\"avg_order_value\": str}]}"
        )
        self.share_knowledge(
            "opportunity", "affiliate_programs",
            json.dumps(programs.get("programs", []))[:1000],
            tags=["affiliate", "monetization"],
        )
        return programs

    def _plan_new_site(self) -> dict[str, Any]:
        """Plan a new content site from scratch."""
        plan = self.think_json(
            "Plan a new content site. Consider:\n"
            "- What niche has demand but low competition?\n"
            "- Can it be monetized with affiliates + ads?\n"
            "- Can we produce 20+ articles for it?\n"
            "- What's the estimated monthly traffic potential?\n\n"
            "Return: {\"niche\": str, \"domain_suggestions\": [str], "
            "\"content_pillars\": [str], \"monetization_plan\": str, "
            "\"initial_articles\": int, \"estimated_monthly_traffic\": str, "
            "\"time_to_first_revenue_months\": int}"
        )
        return plan
