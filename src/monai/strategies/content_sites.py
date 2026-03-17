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

        # Always check for pending sales first
        pending_sales = self.db.execute(
            "SELECT COUNT(*) as c FROM checkout_links "
            "WHERE strategy_name = ? AND status = 'pending'",
            (self.name,),
        )
        has_pending = pending_sales and pending_sales[0]["c"] > 0

        # Deterministic progression
        if not statuses:
            return ["research_keywords"]
        if statuses.get("researched", 0) > 0:
            return ["create_article"]
        if statuses.get("draft", 0) > 0:
            return ["review_content"]
        if statuses.get("reviewed", 0) > 0:
            return ["publish_article"]
        if statuses.get("published", 0) > 0 and statuses.get("monetized", 0) == 0:
            return ["monetize_article"]
        if has_pending:
            return ["check_sales"]
        if statuses.get("monetized", 0) > 0 and statuses.get("reviewed", 0) == 0:
            return ["find_affiliate_programs"]

        # All content published — research more keywords
        return ["research_keywords"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting content sites cycle")
        steps = self.plan()
        results = {}

        step_methods = {
            "research_keywords": self._research_keywords,
            "create_article": self._create_article,
            "review_content": self._review_content,
            "publish_article": self._publish_article,
            "monetize_article": self._monetize_article,
            "check_sales": self._check_sales,
            "find_affiliate_programs": self._find_affiliate_programs,
            "plan_new_site": self._plan_new_site,
        }

        for step in steps:
            fn = step_methods.get(step)
            if fn:
                results[step] = self.run_step(step, fn)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_keywords(self) -> dict[str, Any]:
        """Find low-competition long-tail keywords using web data.

        Uses a single focused search to minimize LLM calls — previous approach
        burned 3 separate browser sessions (Google Trends, keyword search,
        Ubersuggest) that mostly failed via Tor, wasting 15-30 LLM calls.
        """
        self.log_action("keyword_research", "Searching for keyword opportunities")

        # Single focused search — works via Tor on DuckDuckGo/Bing
        keyword_data = self.search_web(
            "low competition long tail keywords buying intent profitable niches 2026",
            "Extract any keyword ideas, search volume estimates, competition "
            "levels, and monetization opportunities mentioned. Only include REAL "
            "data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"keyword_ideas\": [{\"keyword\": str, "
            "\"volume_estimate\": str, \"competition\": str, \"source\": str}]}"
        )

        # Check if we got real data
        has_real_data = (
            isinstance(keyword_data, dict)
            and keyword_data.get("keyword_ideas")
            and keyword_data.get("status") != "error"
        )

        if not has_real_data:
            self.log_action("research_failed", "No real keyword data from web search")
            path = self.sites_dir / "keywords_researched.json"
            path.write_text(json.dumps({
                "status": "researched",
                "keywords": [],
                "note": "Web search failed — will retry next cycle",
            }, indent=2))
            return {"keywords": [], "status": "no_data"}

        keywords = self.think_json(
            "Based on the following REAL keyword research data from the web, "
            "select the 10 best low-competition long-tail keywords to target.\n\n"
            f"Raw research data:\n{json.dumps(keyword_data, default=str)[:3000]}\n\n"
            "Focus on:\n"
            "- 'How to' queries with buying intent\n"
            "- 'Best X for Y' comparison queries\n"
            "- Problems people search for solutions to\n"
            "- Niches where affiliate programs exist\n\n"
            "IMPORTANT: Only recommend keywords supported by the real data above. "
            "If no real data was found, return an empty list.\n\n"
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

    def _publish_article(self) -> dict[str, Any]:
        """Publish reviewed articles to a real platform where they get indexed.

        Uses dev.to (free, SEO-friendly, no domain needed) for publishing.
        Articles sitting in local JSON files are invisible to the internet
        and generate zero traffic/revenue.
        """
        published = 0

        for path in self.sites_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("status") != "reviewed":
                continue

            title = data.get("title", "untitled")
            content = data.get("content", "")
            keywords = data.get("keywords", [])

            if not content:
                continue

            # Ensure dev.to account
            account = self.ensure_platform_account("devto")
            if account.get("status") in ("blocked", "error"):
                # Fallback to Medium
                account = self.ensure_platform_account("medium")
                if account.get("status") in ("blocked", "error"):
                    return {"status": "blocked", "reason": "No publishing platform available"}
                platform = "medium"
            else:
                platform = "devto"

            try:
                result = self.execute_task(
                    f"Publish an SEO article on {platform}.\n"
                    f"Title: {title}\n"
                    f"Keywords/tags: {', '.join(keywords[:5]) if keywords else 'technology'}\n"
                    f"Content (first 2000 chars):\n{content[:2000]}\n\n"
                    f"Steps:\n"
                    f"1. Navigate to {platform} new post editor\n"
                    f"2. Enter the title and paste the full content\n"
                    f"3. Add tags: {', '.join(keywords[:4]) if keywords else 'technology'}\n"
                    f"4. Set a cover image if possible\n"
                    f"5. Publish (not save as draft)\n"
                    f"6. Return the published URL\n\n"
                    f"Return: {{\"url\": str, \"status\": str}}",
                    f"Publishing article: {title}",
                )

                if result.get("status") == "completed" or result.get("url"):
                    data["status"] = "published"
                    data["published_url"] = result.get("url", "")
                    data["published_platform"] = platform
                    path.write_text(json.dumps(data, indent=2))
                    published += 1
                    self.log_action("article_published", f"{title} → {result.get('url', platform)}")
                else:
                    self.log_action("publish_failed", f"{title}: {result}")

            except Exception as e:
                self.log_action("publish_failed", f"{title}: {e}")
                self.learn_from_error(e, f"Publishing article '{title}' to {platform}")

        return {"published": published}

    def _monetize_article(self) -> dict[str, Any]:
        """Create checkout links for published articles.

        For each published article, create a Ko-fi checkout for a premium
        resource (detailed guide, template, or toolkit) related to the article topic.
        """
        monetized = 0

        for path in self.sites_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("status") != "published":
                continue

            keyword = data.get("target", {}).get("keyword",
                      data.get("target", {}).get("title", path.stem))
            published_url = data.get("published_url", "")

            checkout = self.create_checkout_link(
                amount=7.99,
                product=f"Complete Guide: {keyword}",
                provider="kofi",
                metadata={
                    "content_file": str(path.name),
                    "published_url": published_url,
                    "keyword": keyword,
                },
            )

            if checkout.get("status") == "created":
                data["status"] = "monetized"
                data["checkout_url"] = checkout.get("checkout_url", "")
                data["payment_ref"] = checkout.get("payment_ref", "")
                path.write_text(json.dumps(data, indent=2))
                monetized += 1
                self.log_action(
                    "article_monetized",
                    f"{keyword}: {checkout.get('checkout_url', '')}",
                )
            else:
                data["status"] = "monetized"
                path.write_text(json.dumps(data, indent=2))
                self.log_action(
                    "monetize_skipped",
                    f"{keyword}: no payment provider available",
                )

        return {"monetized": monetized}

    def _check_sales(self) -> dict[str, Any]:
        """Check pending checkout links for completed payments."""
        return self.check_pending_sales()

    def _find_affiliate_programs(self) -> dict[str, Any]:
        """Research affiliate programs using a single focused web search.

        Previous approach burned 3 browser sessions (ShareASale, CJ, web search)
        that mostly failed via Tor. Now uses 1 search.
        """
        self.log_action("affiliate_research", "Searching for affiliate programs")

        search_data = self.search_web(
            "best high commission affiliate programs 2026 content sites recurring",
            "Extract affiliate program names, commission rates, cookie durations, "
            "niches, and signup URLs mentioned. Only include REAL data visible on "
            "the page. Do NOT make up any information. "
            "Return as JSON: {\"programs\": [{\"name\": str, "
            "\"commission_rate\": str, \"cookie_days\": str, \"niche\": str, "
            "\"signup_url\": str}]}"
        )

        has_real_data = (
            isinstance(search_data, dict)
            and search_data.get("programs")
            and search_data.get("status") != "error"
        )

        if not has_real_data:
            self.log_action("affiliate_research_failed", "No real program data")
            return {"programs": [], "status": "no_data"}

        programs = self.think_json(
            "Based on the following REAL affiliate program data from the web, "
            "select the 5 best programs for a content site.\n\n"
            f"Raw research data:\n{json.dumps(search_data, default=str)[:3000]}\n\n"
            "Focus on programs with:\n"
            "- Good commission rates (>5%)\n"
            "- Cookie duration >30 days\n"
            "- Reputable brands\n"
            "- Products people actually buy\n\n"
            "IMPORTANT: Only include programs from the real data above. "
            "If no real data was found, return an empty list.\n\n"
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
