"""Print on Demand strategy agent.

Generates designs, lists products on POD platforms (Redbubble, TeeSpring, etc.).
Zero inventory risk. Passive income once listed.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

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

    def plan(self) -> list[str]:
        designs = self.db.execute("SELECT status, COUNT(*) as c FROM pod_designs GROUP BY status")
        stats = {r["status"]: r["c"] for r in designs}
        plan = self.think_json(
            f"POD portfolio: {json.dumps(stats)}. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_niches, generate_design_concepts, create_listings, "
            "optimize_tags, analyze_sales, find_trending.",
        )
        return plan.get("steps", ["research_niches"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting POD cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_niches":
                results["niches"] = self._research_niches()
            elif step == "generate_design_concepts":
                results["concepts"] = self._generate_concepts()
            elif step == "create_listings":
                results["listings"] = self._create_listings()
            elif step == "find_trending":
                results["trending"] = self._find_trending()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Find profitable POD niches."""
        return self.think_json(
            "Research 5 profitable print-on-demand niches. Focus on:\n"
            "- Passionate communities (hobbies, professions, fandoms)\n"
            "- Evergreen demand (not just seasonal)\n"
            "- Low competition on specific sub-niches\n"
            "- Design-friendly (text-based or simple graphics work)\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"audience\": str, "
            "\"design_styles\": [str], \"best_products\": [str], "
            "\"platforms\": [str], \"estimated_monthly_sales\": int, "
            "\"competition\": str}]}"
        )

    def _generate_concepts(self) -> dict[str, Any]:
        """Generate design concepts for POD products."""
        concepts = self.think_json(
            "Generate 10 print-on-demand design concepts. For each:\n"
            "- A catchy text/slogan OR a simple graphic description\n"
            "- Target niche and audience\n"
            "- Which products it works on\n"
            "- Search tags for discoverability\n\n"
            "Focus on designs that can be TEXT-BASED (no complex illustrations needed).\n"
            "Think: funny quotes, profession pride, hobby references, motivational.\n\n"
            "Return: {\"concepts\": [{\"title\": str, \"niche\": str, "
            "\"design_text\": str, \"design_style\": str, "
            "\"products\": [str], \"tags\": [str], \"audience\": str}]}"
        )

        for concept in concepts.get("concepts", []):
            self.db.execute_insert(
                "INSERT INTO pod_designs (title, niche, description, design_prompt, "
                "products, tags, status) VALUES (?, ?, ?, ?, ?, ?, 'concept')",
                (concept.get("title", ""), concept.get("niche", ""),
                 concept.get("design_text", ""), concept.get("design_style", ""),
                 json.dumps(concept.get("products", ["t-shirt"])),
                 json.dumps(concept.get("tags", []))),
            )

        self.log_action("concepts_generated", f"{len(concepts.get('concepts', []))} concepts")
        return concepts

    def _create_listings(self) -> dict[str, Any]:
        """Create marketplace listings for designed products."""
        concepts = self.db.execute(
            "SELECT * FROM pod_designs WHERE status = 'concept' LIMIT 5"
        )
        listed = 0
        for design in concepts:
            d = dict(design)
            listing = self.think_json(
                f"Create a Redbubble/TeeSpring listing for:\n"
                f"Title: {d['title']}\n"
                f"Niche: {d['niche']}\n"
                f"Design: {d['description']}\n"
                f"Products: {d['products']}\n\n"
                "Return: {\"listing_title\": str, \"description\": str, "
                "\"tags\": [str], \"platforms\": [str], \"pricing_strategy\": str}"
            )

            self.db.execute(
                "UPDATE pod_designs SET status = 'listed', "
                "platforms = ?, tags = ? WHERE id = ?",
                (json.dumps(listing.get("platforms", ["redbubble"])),
                 json.dumps(listing.get("tags", [])), d["id"]),
            )
            listed += 1

        return {"listed": listed}

    def _find_trending(self) -> dict[str, Any]:
        """Find trending topics and themes for POD."""
        return self.think_json(
            "What's currently trending that could work for POD designs?\n"
            "Think about: memes, cultural moments, seasonal events, "
            "new hobbies, viral phrases, profession trends.\n\n"
            "Return: {\"trends\": [{\"trend\": str, \"design_idea\": str, "
            "\"urgency\": str, \"products\": [str], \"estimated_demand\": str}]}"
        )
