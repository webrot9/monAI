"""Domain flipping strategy agent.

Finds undervalued or expired domains, acquires them cheaply,
and resells at a markup. Low cost, high margin per flip.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

DOMAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_name TEXT UNIQUE NOT NULL,
    tld TEXT NOT NULL,                       -- com, net, io, etc.
    acquisition_cost REAL DEFAULT 0.0,
    estimated_value REAL DEFAULT 0.0,
    niche TEXT,
    keywords TEXT,                           -- JSON list of keywords
    has_backlinks INTEGER DEFAULT 0,
    has_traffic INTEGER DEFAULT 0,
    domain_age_years REAL DEFAULT 0.0,
    registrar TEXT,
    status TEXT DEFAULT 'prospect',          -- prospect, acquired, listed, sold, expired
    listed_price REAL,
    sold_price REAL,
    listed_on TEXT,                          -- JSON list: ["sedo", "afternic", "dan"]
    acquired_at TIMESTAMP,
    sold_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class DomainFlippingAgent(BaseAgent):
    name = "domain_flipping"
    description = (
        "Finds undervalued and expired domains, acquires them cheaply, "
        "and resells at a markup on domain marketplaces. Low cost per "
        "acquisition, high margin per successful flip."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(DOMAIN_SCHEMA)

    def plan(self) -> list[str]:
        domains = self.db.execute("SELECT status, COUNT(*) as c FROM domains GROUP BY status")
        stats = {r["status"]: r["c"] for r in domains}
        plan = self.think_json(
            f"Domain portfolio: {json.dumps(stats)}. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_expired, evaluate_domains, acquire_domain, "
            "list_for_sale, optimize_listings, analyze_market.",
        )
        return plan.get("steps", ["research_expired"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting domain flipping cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_expired":
                results["expired"] = self._research_expired()
            elif step == "evaluate_domains":
                results["evaluated"] = self._evaluate_domains()
            elif step == "analyze_market":
                results["market"] = self._analyze_market()
            elif step == "list_for_sale":
                results["listed"] = self._list_for_sale()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_expired(self) -> dict[str, Any]:
        """Find expired or expiring domains with resale potential."""
        return self.think_json(
            "Research strategies for finding valuable expired domains. Include:\n"
            "- Where to find expired domain auctions\n"
            "- What makes a domain valuable (length, keywords, backlinks, age)\n"
            "- 10 example domain patterns worth targeting\n"
            "- Budget strategy (how much to spend per domain)\n\n"
            "Return: {\"sources\": [{\"name\": str, \"url\": str, \"type\": str}], "
            "\"valuable_patterns\": [{\"pattern\": str, \"reason\": str, "
            "\"typical_acquisition_cost\": float, \"typical_resale_value\": float}], "
            "\"budget_per_domain\": float}"
        )

    def _evaluate_domains(self) -> dict[str, Any]:
        """Evaluate prospect domains for acquisition."""
        prospects = self.db.execute(
            "SELECT * FROM domains WHERE status = 'prospect' LIMIT 10"
        )
        if not prospects:
            return {"status": "no_prospects"}

        evaluated = 0
        for domain in prospects:
            d = dict(domain)
            evaluation = self.think_json(
                f"Evaluate this domain for flipping potential:\n"
                f"Domain: {d['domain_name']}\n"
                f"TLD: {d['tld']}\n"
                f"Age: {d.get('domain_age_years', 0)} years\n"
                f"Keywords: {d.get('keywords', '[]')}\n\n"
                "Return: {\"estimated_value\": float, \"acquisition_recommended\": bool, "
                "\"max_bid\": float, \"resale_potential\": str, "
                "\"target_buyer\": str, \"reasoning\": str}"
            )

            self.db.execute(
                "UPDATE domains SET estimated_value = ? WHERE id = ?",
                (evaluation.get("estimated_value", 0), d["id"]),
            )
            evaluated += 1

        return {"evaluated": evaluated}

    def _analyze_market(self) -> dict[str, Any]:
        """Analyze current domain market trends."""
        return self.think_json(
            "Analyze current domain market trends:\n"
            "- Which TLDs are gaining value?\n"
            "- What keyword categories are hot?\n"
            "- Average flip margins by category\n"
            "- Best marketplaces for selling\n\n"
            "Return: {\"trends\": [{\"trend\": str, \"opportunity\": str}], "
            "\"hot_categories\": [str], \"best_marketplaces\": [{\"name\": str, "
            "\"commission\": str, \"audience\": str}], "
            "\"avg_flip_margin_pct\": float}"
        )

    def _list_for_sale(self) -> dict[str, Any]:
        """List acquired domains on marketplaces."""
        acquired = self.db.execute(
            "SELECT * FROM domains WHERE status = 'acquired'"
        )
        listed = 0
        for domain in acquired:
            d = dict(domain)
            listing = self.think_json(
                f"Create a sales listing for domain: {d['domain_name']}\n"
                f"Estimated value: ${d.get('estimated_value', 0)}\n"
                f"Niche: {d.get('niche', 'general')}\n\n"
                "Return: {\"listing_title\": str, \"description\": str, "
                "\"asking_price\": float, \"min_offer\": float, "
                "\"marketplaces\": [str], \"target_buyer_profile\": str}"
            )

            self.db.execute(
                "UPDATE domains SET status = 'listed', listed_price = ?, "
                "listed_on = ? WHERE id = ?",
                (listing.get("asking_price", d.get("estimated_value", 100)),
                 json.dumps(listing.get("marketplaces", ["sedo"])),
                 d["id"]),
            )
            listed += 1

        return {"listed": listed}
