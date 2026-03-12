"""Print on Demand strategy agent.

Generates designs, lists products on POD platforms (Redbubble, TeeSpring, etc.).
Zero inventory risk. Passive income once listed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

POD_SCHEMA = """
CREATE TABLE IF NOT EXISTS pod_designs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    niche TEXT NOT NULL,
    description TEXT,
    design_prompt TEXT,                      -- prompt used to generate the design
    design_path TEXT,                        -- path to design file
    products TEXT,                           -- JSON list: ["t-shirt", "mug", "sticker"]
    platforms TEXT,                          -- JSON list: ["redbubble", "teespring"]
    tags TEXT,                              -- JSON list of search tags
    status TEXT DEFAULT 'concept',           -- concept, designed, listed, selling, retired
    listing_url TEXT,
    total_sales INTEGER DEFAULT 0,
    total_revenue REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class PrintOnDemandAgent(BaseAgent):
    name = "print_on_demand"
    description = (
        "Creates designs and lists products on print-on-demand platforms. "
        "Zero inventory, zero shipping. Generates designs for t-shirts, mugs, "
        "stickers, posters. Passive income once listed."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(POD_SCHEMA)
        self.designs_dir = config.data_dir / "pod_designs"
        self.designs_dir.mkdir(parents=True, exist_ok=True)

    def plan(self) -> list[str]:
        designs = self.db.execute("SELECT status, COUNT(*) as c FROM pod_designs GROUP BY status")
        stats = {r["status"]: r["c"] for r in designs}

        # Deterministic progression
        if not stats:
            return ["research_niches"]
        if stats.get("concept", 0) > 0:
            return ["generate_design_concepts"]
        if stats.get("designed", 0) > 0:
            return ["review_design"]
        if stats.get("reviewed", 0) > 0:
            return ["create_listings"]
        if stats.get("listed", 0) > 0:
            return ["optimize_tags"]
        if stats.get("selling", 0) > 0:
            return ["analyze_sales"]

        # All designs at final stage — research new niches
        return ["research_niches"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting POD cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_niches":
                results["niches"] = self._research_niches()
            elif step == "generate_design_concepts":
                results["concepts"] = self._generate_concepts()
            elif step == "review_design":
                results["review"] = self._review_design()
            elif step == "create_listings":
                results["listings"] = self._create_listings()
            elif step == "find_trending":
                results["trending"] = self._find_trending()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Research REAL trending POD niches by browsing actual POD platforms."""
        all_niches = []

        # Browse Redbubble trending/popular to see what's actually selling
        redbubble_data = self.browse_and_extract(
            "https://www.redbubble.com/shop/trending+t-shirts",
            "Analyze the trending t-shirt designs on Redbubble. For each design extract:\n"
            "- title: the product title\n"
            "- artist: the artist/shop name\n"
            "- price: the listed price\n"
            "- niche: what niche/category it belongs to\n"
            "- design_style: text-based, illustration, graphic, etc.\n"
            "- tags: any visible tags or categories\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"designs\": [...]}"
        )
        for design in redbubble_data.get("designs", []):
            design["source"] = "redbubble"
            all_niches.append(design)

        # Browse TeeSpring/Spring marketplace for bestsellers
        teespring_data = self.browse_and_extract(
            "https://www.spring.com/discover",
            "Analyze the featured and popular products. For each extract:\n"
            "- title: the product name\n"
            "- creator: the shop/creator name\n"
            "- price: the listed price\n"
            "- category: what niche this falls into\n"
            "- product_type: t-shirt, hoodie, mug, etc.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"products\": [...]}"
        )
        for product in teespring_data.get("products", []):
            product["source"] = "teespring"
            all_niches.append(product)

        # Browse Merch by Amazon bestsellers (via Amazon search)
        amazon_data = self.browse_and_extract(
            "https://www.amazon.com/s?k=funny+t-shirt&s=review-rank",
            "Analyze the bestselling funny/novelty t-shirts. For each extract:\n"
            "- title: the product title\n"
            "- price: the listed price\n"
            "- rating: star rating\n"
            "- num_reviews: number of reviews\n"
            "- niche: what niche/theme (profession, hobby, humor style)\n"
            "- design_type: text-based, graphic, or mixed\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"products\": [...]}"
        )
        for product in amazon_data.get("products", []):
            product["source"] = "merch_by_amazon"
            all_niches.append(product)

        # Search for POD niche analysis and data
        niche_research = self.search_web(
            "print on demand best selling niches 2025 2026 redbubble teespring data",
            "Find real articles, blog posts, or data about which POD niches are "
            "currently performing well. For each source extract:\n"
            "- source_url: the URL of the article/data\n"
            "- top_niches: list of niches mentioned as profitable\n"
            "- data_points: any specific sales figures, growth rates, or trends\n"
            "- recommendations: specific design styles or approaches suggested\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"research\": [...]}"
        )
        for item in niche_research.get("research", []):
            item["source"] = "web_research"
            all_niches.append(item)

        # Use LLM to ANALYZE the real data and identify best niches
        analysis = self.think_json(
            f"Based on this REAL market research from POD platforms, identify the "
            f"5 most profitable niches to target:\n\n"
            f"Market data: {json.dumps(all_niches, default=str)[:3000]}\n\n"
            "For each niche explain why the real data supports it.\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"audience\": str, "
            "\"design_styles\": [str], \"best_products\": [str], "
            "\"platforms\": [str], \"evidence\": str, \"competition\": str}]}"
        )

        self.log_action(
            "niches_researched",
            f"Scraped {len(all_niches)} items from real POD platforms, "
            f"identified {len(analysis.get('niches', []))} niches"
        )
        return {
            "market_data_collected": len(all_niches),
            "niches": analysis.get("niches", []),
            "raw_market_data": all_niches[:20],
        }

    def _generate_concepts(self) -> dict[str, Any]:
        """Generate REAL SVG design files for POD products using the coder agent."""
        # First, use LLM to plan what designs to create (planning is legitimate)
        concepts = self.think_json(
            "Generate 5 print-on-demand design concepts. For each:\n"
            "- A catchy text/slogan OR a simple graphic description\n"
            "- Target niche and audience\n"
            "- Which products it works on\n"
            "- Search tags for discoverability\n"
            "- Detailed SVG design specification (colors, fonts, layout)\n\n"
            "Focus on designs that can be created as SVG (text-based, simple graphics).\n"
            "Think: funny quotes, profession pride, hobby references, motivational.\n\n"
            "Return: {\"concepts\": [{\"title\": str, \"niche\": str, "
            "\"design_text\": str, \"design_style\": str, "
            "\"svg_spec\": str, \"colors\": [str], \"font_style\": str, "
            "\"products\": [str], \"tags\": [str], \"audience\": str}]}"
        )

        generated = 0
        for concept in concepts.get("concepts", []):
            title = concept.get("title", "untitled")
            safe_title = "".join(
                c if c.isalnum() or c in " -_" else "" for c in title
            ).strip().replace(" ", "_")

            # Use the coder agent to generate a REAL SVG design file
            svg_spec = (
                f"Generate a production-ready SVG design file for a print-on-demand product.\n\n"
                f"Design title: {title}\n"
                f"Text/slogan: {concept.get('design_text', '')}\n"
                f"Style: {concept.get('design_style', 'bold text')}\n"
                f"Colors: {json.dumps(concept.get('colors', ['#FFFFFF', '#000000']))}\n"
                f"Font style: {concept.get('font_style', 'bold sans-serif')}\n"
                f"SVG specification: {concept.get('svg_spec', '')}\n\n"
                "Requirements:\n"
                "- Canvas size: 4500x5400 pixels (standard POD ratio)\n"
                "- Transparent background\n"
                "- Text must be readable and impactful\n"
                "- Use web-safe fonts or include font paths\n"
                "- High contrast for printing on light and dark garments\n"
                "- Export-ready SVG with all elements properly grouped\n\n"
                f"Save the SVG file to: {self.designs_dir / f'{safe_title}.svg'}"
            )

            try:
                build_result = self.coder.generate_module(svg_spec)
                design_path = str(self.designs_dir / f"{safe_title}.svg")

                # Verify the file was actually created
                if Path(design_path).exists():
                    status = "designed"
                    self.log_action("design_generated", title, f"path={design_path}")
                else:
                    status = "concept"
                    design_path = ""
                    logger.warning("Coder did not create SVG file for %s", title)

            except Exception as e:
                status = "concept"
                design_path = ""
                self.log_action("design_generation_failed", title, str(e)[:300])
                self.learn_from_error(e, f"Generating SVG design for {title}")

            self.db.execute_insert(
                "INSERT INTO pod_designs (title, niche, description, design_prompt, "
                "design_path, products, tags, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, concept.get("niche", ""),
                 concept.get("design_text", ""), concept.get("design_style", ""),
                 design_path,
                 json.dumps(concept.get("products", ["t-shirt"])),
                 json.dumps(concept.get("tags", [])),
                 status),
            )
            if status == "designed":
                generated += 1

        self.log_action(
            "concepts_generated",
            f"{generated}/{len(concepts.get('concepts', []))} designs created as SVG files"
        )
        return {
            "total_concepts": len(concepts.get("concepts", [])),
            "designs_generated": generated,
            "concepts": concepts.get("concepts", []),
        }

    def _review_design(self) -> dict[str, Any]:
        """Quality gate: review design listing text before publishing."""
        designs = self.db.execute(
            "SELECT * FROM pod_designs WHERE status = 'designed' LIMIT 1"
        )
        if not designs:
            return {"status": "no_designs_to_review"}

        design = dict(designs[0])
        product_data = {
            "spec": {
                "title": design["title"],
                "description": design.get("description", ""),
                "niche": design.get("niche", ""),
            },
            "listing": design.get("description", ""),
        }

        result = self.reviewer.review_product(
            strategy=self.name,
            product_name=design["title"],
            product_data=product_data,
            product_type="digital_product",
        )

        if result.verdict == "approved":
            self.db.execute(
                "UPDATE pod_designs SET status = 'reviewed' WHERE id = ?",
                (design["id"],),
            )
            self.log_action("design_reviewed", f"{design['title']}: APPROVED")
        elif result.verdict == "rejected":
            self.db.execute(
                "UPDATE pod_designs SET status = 'concept' WHERE id = ?",
                (design["id"],),
            )
            self.log_action("design_rejected", f"{design['title']}: REJECTED — redesign")
        else:
            revised = self.reviewer.revise_product(product_data, result, "digital_product")
            revised_desc = revised.get("revised_content", design.get("description", ""))
            self.db.execute(
                "UPDATE pod_designs SET status = 'reviewed', description = ? WHERE id = ?",
                (revised_desc, design["id"]),
            )
            self.log_action("design_revised", f"{design['title']}: REVISED")

        return result.to_dict()

    def _create_listings(self) -> dict[str, Any]:
        """ACTUALLY create listings on POD platforms using platform_action."""
        designs = self.db.execute(
            "SELECT * FROM pod_designs WHERE status = 'designed' AND design_path != '' LIMIT 5"
        )

        listed = 0
        for design in designs:
            d = dict(design)
            tags = json.loads(d.get("tags", "[]"))
            products = json.loads(d.get("products", '["t-shirt"]'))
            platforms = json.loads(d.get("platforms", '["redbubble"]'))

            # Use LLM to write compelling listing copy (creative work is legitimate)
            listing_copy = self.think_json(
                f"Write a compelling POD product listing for:\n"
                f"Title: {d['title']}\n"
                f"Niche: {d['niche']}\n"
                f"Design text: {d['description']}\n"
                f"Products: {json.dumps(products)}\n\n"
                "Return: {\"listing_title\": str, \"description\": str, "
                "\"tags\": [str], \"pricing_notes\": str}"
            )

            # Merge LLM-suggested tags with existing tags
            all_tags = list(set(tags + listing_copy.get("tags", [])))

            for platform in platforms:
                # Ensure we have an account on this platform
                self.ensure_platform_account(platform)

                # Actually create the listing via platform_action
                try:
                    result = self.platform_action(
                        platform,
                        f"Create a new product listing:\n"
                        f"Title: {listing_copy.get('listing_title', d['title'])}\n"
                        f"Description: {listing_copy.get('description', d['description'])}\n"
                        f"Tags: {json.dumps(all_tags)}\n"
                        f"Products to enable: {json.dumps(products)}\n"
                        f"Design file path: {d['design_path']}\n\n"
                        f"Upload the SVG design file and enable it on these product types: "
                        f"{', '.join(products)}.\n"
                        f"Set competitive pricing for the {d['niche']} niche.",
                        f"Creating POD listing for '{d['title']}' on {platform}"
                    )

                    listing_url = result.get("url", "")
                    self.db.execute(
                        "UPDATE pod_designs SET status = 'listed', "
                        "platforms = ?, tags = ?, listing_url = ? WHERE id = ?",
                        (json.dumps(platforms), json.dumps(all_tags),
                         listing_url, d["id"]),
                    )
                    listed += 1
                    self.log_action(
                        "listing_created", d["title"],
                        f"platform={platform} url={listing_url}"
                    )

                except Exception as e:
                    self.log_action(
                        "listing_failed", d["title"],
                        f"platform={platform} error={str(e)[:200]}"
                    )
                    self.learn_from_error(
                        e, f"Creating POD listing for '{d['title']}' on {platform}"
                    )

        self.log_action("create_listings_complete", f"{listed} listings created")
        return {"listed": listed}

    def _find_trending(self) -> dict[str, Any]:
        """Find REAL trending topics by browsing actual trend sources."""
        all_trends = []

        # Browse Google Trends for real trending searches
        google_trends = self.browse_and_extract(
            "https://trends.google.com/trending?geo=US",
            "Extract the currently trending search topics and queries. For each extract:\n"
            "- topic: the trending topic or search query\n"
            "- search_volume: approximate search volume if shown\n"
            "- category: what category it falls into\n"
            "- trend_direction: rising, peaked, declining\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"trends\": [...]}"
        )
        for trend in google_trends.get("trends", []):
            trend["source"] = "google_trends"
            all_trends.append(trend)

        # Search social media for viral content and memes relevant to POD
        social_trends = self.search_web(
            "viral memes trending phrases 2025 2026 t-shirt worthy",
            "Find real viral memes, phrases, and cultural moments that would work "
            "as POD designs. For each extract:\n"
            "- trend: the meme, phrase, or cultural moment\n"
            "- source_url: where it was found\n"
            "- virality: how widespread it is\n"
            "- pod_potential: how well it would work as a design\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"trends\": [...]}"
        )
        for trend in social_trends.get("trends", []):
            trend["source"] = "social_media"
            all_trends.append(trend)

        # Check Reddit for trending topics in relevant subreddits
        reddit_trends = self.browse_and_extract(
            "https://www.reddit.com/r/popular/",
            "Extract currently popular/trending posts that could inspire POD designs. "
            "Look for funny phrases, cultural references, hobby content. For each extract:\n"
            "- title: the post title\n"
            "- subreddit: which subreddit it's from\n"
            "- upvotes: approximate upvote count\n"
            "- pod_relevance: how this could translate to a POD design\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"posts\": [...]}"
        )
        for post in reddit_trends.get("posts", []):
            post["source"] = "reddit"
            all_trends.append(post)

        # Use LLM to ANALYZE real trends and generate design ideas from them
        analysis = self.think_json(
            f"Based on these REAL trending topics from Google Trends, social media, "
            f"and Reddit, identify the top 5 trends that would work as POD designs:\n\n"
            f"Trends data: {json.dumps(all_trends, default=str)[:3000]}\n\n"
            "For each trend, suggest a concrete design idea that capitalizes on it.\n\n"
            "Return: {\"trends\": [{\"trend\": str, \"design_idea\": str, "
            "\"urgency\": str, \"products\": [str], \"estimated_demand\": str, "
            "\"evidence\": str}]}"
        )

        self.log_action(
            "trending_researched",
            f"Collected {len(all_trends)} trends from real sources, "
            f"identified {len(analysis.get('trends', []))} design opportunities"
        )
        return {
            "trends_collected": len(all_trends),
            "trends": analysis.get("trends", []),
            "raw_trend_data": all_trends[:20],
        }
