"""Lead generation strategy agent.

Scrapes, qualifies, and sells B2B leads to businesses.
High margins, recurring need, and scales well with automation.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

LEADGEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    niche TEXT NOT NULL,
    source TEXT NOT NULL,                    -- linkedin, google_maps, directories, etc.
    total_leads INTEGER DEFAULT 0,
    qualified_leads INTEGER DEFAULT 0,
    sold_count INTEGER DEFAULT 0,
    price_per_lead REAL DEFAULT 0.0,
    buyer TEXT,                              -- who bought this list
    status TEXT DEFAULT 'building',          -- building, qualifying, ready, sold
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER REFERENCES lead_lists(id),
    company_name TEXT,
    contact_name TEXT,
    email TEXT,
    phone TEXT,
    website TEXT,
    industry TEXT,
    location TEXT,
    company_size TEXT,
    qualification_score REAL DEFAULT 0.0,    -- 0-1 how qualified
    enrichment_data TEXT,                    -- JSON: additional data
    status TEXT DEFAULT 'raw',               -- raw, enriched, qualified, sold
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class LeadGenAgent(BaseAgent):
    name = "lead_gen"
    description = (
        "Generates and sells qualified B2B leads. Scrapes public directories, "
        "enriches with additional data, qualifies by fit score, and packages "
        "into lead lists for sale to businesses."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(LEADGEN_SCHEMA)

    def plan(self) -> list[str]:
        lists = self.db.execute("SELECT status, COUNT(*) as c FROM lead_lists GROUP BY status")
        stats = {r["status"]: r["c"] for r in lists}
        plan = self.think_json(
            f"Lead list stats: {json.dumps(stats)}. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_niches, build_list, enrich_leads, qualify_leads, "
            "package_for_sale, find_buyers, analyze_performance.",
        )
        return plan.get("steps", ["research_niches"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting lead gen cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_niches":
                results["niches"] = self._research_niches()
            elif step == "build_list":
                results["list"] = self._build_list()
            elif step == "enrich_leads":
                results["enriched"] = self._enrich_leads()
            elif step == "qualify_leads":
                results["qualified"] = self._qualify_leads()
            elif step == "find_buyers":
                results["buyers"] = self._find_buyers()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Find niches where businesses actively buy leads."""
        return self.think_json(
            "Research 5 B2B niches where businesses buy leads. Focus on:\n"
            "- High customer lifetime value (so they pay more per lead)\n"
            "- Businesses that rely on outbound sales\n"
            "- Industries where contact info is publicly findable\n"
            "- Not oversaturated with existing lead gen services\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"typical_buyer\": str, "
            "\"lead_value_usd\": float, \"data_sources\": [str], "
            "\"qualification_criteria\": [str], \"estimated_demand\": str}]}"
        )

    def _build_list(self) -> dict[str, Any]:
        """Plan and build a lead list for a specific niche."""
        plan = self.think_json(
            "Design a lead list to build. Specify:\n"
            "- Target niche and buyer persona\n"
            "- Where to find the data (public directories, Google Maps, industry listings)\n"
            "- What data points to collect\n"
            "- Qualification criteria\n"
            "- How many leads to target\n\n"
            "Return: {\"name\": str, \"niche\": str, \"source\": str, "
            "\"data_points\": [str], \"target_count\": int, "
            "\"qualification_criteria\": [str], \"price_per_lead\": float}"
        )

        name = plan.get("name", "untitled_list")
        list_id = self.db.execute_insert(
            "INSERT INTO lead_lists (name, niche, source, price_per_lead) VALUES (?, ?, ?, ?)",
            (name, plan.get("niche", ""), plan.get("source", ""),
             plan.get("price_per_lead", 1.0)),
        )
        self.log_action("list_created", name, f"id={list_id}")
        return {**plan, "list_id": list_id}

    def _enrich_leads(self) -> dict[str, Any]:
        """Enrich raw leads with additional data."""
        raw_count = self.db.execute(
            "SELECT COUNT(*) as c FROM leads WHERE status = 'raw'"
        )
        count = raw_count[0]["c"] if raw_count else 0
        self.log_action("enrich_leads", f"{count} raw leads to enrich")
        return {"raw_leads": count, "status": "enrichment_planned"}

    def _qualify_leads(self) -> dict[str, Any]:
        """Score and qualify enriched leads."""
        enriched = self.db.execute(
            "SELECT COUNT(*) as c FROM leads WHERE status = 'enriched'"
        )
        count = enriched[0]["c"] if enriched else 0
        self.log_action("qualify_leads", f"{count} leads to qualify")
        return {"enriched_leads": count, "status": "qualification_planned"}

    def _find_buyers(self) -> dict[str, Any]:
        """Find businesses that buy leads."""
        return self.think_json(
            "Find 5 types of businesses that actively buy B2B leads. "
            "For each, specify:\n"
            "- What kind of leads they want\n"
            "- How much they typically pay per lead\n"
            "- Where to find and pitch them\n"
            "- What quality standards they expect\n\n"
            "Return: {\"buyers\": [{\"business_type\": str, \"lead_criteria\": str, "
            "\"price_range_per_lead\": str, \"where_to_find\": str, "
            "\"quality_requirements\": [str]}]}"
        )
