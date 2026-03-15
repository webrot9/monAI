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

        # Deterministic progression
        if not stats:
            return ["research_expired"]
        if stats.get("prospect", 0) > 0:
            return ["evaluate_domains"]
        if stats.get("acquired", 0) > 0:
            return ["list_for_sale"]
        if stats.get("listed", 0) > 0:
            return ["optimize_listings"]

        # All domains sold — research new expired ones
        return ["research_expired"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting domain flipping cycle")
        steps = self.plan()
        results = {}

        step_methods = {
            "research_expired": self._research_expired,
            "evaluate_domains": self._evaluate_domains,
            "analyze_market": self._analyze_market,
            "list_for_sale": self._list_for_sale,
        }
        for step in steps:
            fn = step_methods.get(step)
            if fn:
                results[step] = self.run_step(step, fn)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_expired(self) -> dict[str, Any]:
        """Find REAL expired or expiring domains from actual auction sites."""
        # Browse real expired domain sources
        expired_data = self.browse_and_extract(
            "https://www.expireddomains.net/deleted-domains/",
            "Extract recently deleted/expired domains listed on this page. "
            "For each domain, extract: domain name, TLD, backlinks count, "
            "domain age, Archive.org entries, and any traffic/rank data shown. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"domains\": [{\"name\": str, \"tld\": str, "
            "\"backlinks\": int, \"age_years\": float, \"archive_entries\": int, "
            "\"rank\": str}]}"
        )

        namejet_data = self.browse_and_extract(
            "https://www.namejet.com/",
            "Extract domains currently up for auction on NameJet. "
            "For each domain, extract: domain name, current bid price, "
            "number of bidders, auction end time, and any metrics shown. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"auctions\": [{\"domain\": str, \"current_bid\": float, "
            "\"bidders\": int, \"ends\": str}]}"
        )

        godaddy_data = self.browse_and_extract(
            "https://auctions.godaddy.com/trpItemListingList.aspx",
            "Extract domains currently listed in GoDaddy Auctions. "
            "For each domain, extract: domain name, price or current bid, "
            "auction type (bid/buy now), traffic, and valuation if shown. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"domains\": [{\"name\": str, \"price\": float, "
            "\"auction_type\": str, \"traffic\": int, \"valuation\": float}]}"
        )

        # Store discovered domains as prospects
        all_domains = []
        for d in expired_data.get("domains", []):
            domain_name = d.get("name", "")
            if not domain_name:
                continue
            tld = d.get("tld", domain_name.rsplit(".", 1)[-1] if "." in domain_name else "com")
            self.db.execute_insert(
                "INSERT OR IGNORE INTO domains (domain_name, tld, has_backlinks, "
                "domain_age_years, status) VALUES (?, ?, ?, ?, 'prospect')",
                (domain_name, tld, 1 if d.get("backlinks", 0) > 0 else 0,
                 d.get("age_years", 0)),
            )
            all_domains.append(d)

        for d in namejet_data.get("auctions", []):
            domain_name = d.get("domain", "")
            if not domain_name:
                continue
            tld = domain_name.rsplit(".", 1)[-1] if "." in domain_name else "com"
            self.db.execute_insert(
                "INSERT OR IGNORE INTO domains (domain_name, tld, acquisition_cost, "
                "status) VALUES (?, ?, ?, 'prospect')",
                (domain_name, tld, d.get("current_bid", 0)),
            )
            all_domains.append(d)

        for d in godaddy_data.get("domains", []):
            domain_name = d.get("name", "")
            if not domain_name:
                continue
            tld = domain_name.rsplit(".", 1)[-1] if "." in domain_name else "com"
            self.db.execute_insert(
                "INSERT OR IGNORE INTO domains (domain_name, tld, acquisition_cost, "
                "estimated_value, has_traffic, status) VALUES (?, ?, ?, ?, ?, 'prospect')",
                (domain_name, tld, d.get("price", 0), d.get("valuation", 0),
                 1 if d.get("traffic", 0) > 0 else 0),
            )
            all_domains.append(d)

        self.log_action("domains_researched", f"Found {len(all_domains)} prospect domains")
        return {
            "expired_domains": expired_data,
            "namejet_auctions": namejet_data,
            "godaddy_auctions": godaddy_data,
            "total_prospects_found": len(all_domains),
        }

    def _evaluate_domains(self) -> dict[str, Any]:
        """Evaluate prospect domains using REAL SEO and backlink data."""
        prospects = self.db.execute(
            "SELECT * FROM domains WHERE status = 'prospect' LIMIT 10"
        )
        if not prospects:
            return {"status": "no_prospects"}

        evaluated = 0
        for domain in prospects:
            d = dict(domain)
            domain_name = d["domain_name"]

            # Check real Wayback Machine data
            wayback_data = self.browse_and_extract(
                f"https://web.archive.org/web/*/{domain_name}",
                f"Extract real archive data for {domain_name}. "
                f"How many snapshots exist? What date range? What was the site about? "
                f"Only include REAL data visible on the page. Do NOT make up any information. "
                f"Return as JSON: {{\"snapshots\": int, \"first_archive\": str, "
                f"\"last_archive\": str, \"site_description\": str}}"
            )

            # Check real backlink/SEO data via a free tool
            seo_data = self.browse_and_extract(
                f"https://ahrefs.com/backlink-checker/free?input={domain_name}",
                f"Extract real backlink and SEO metrics for {domain_name}. "
                f"Include: domain rating, number of backlinks, referring domains, "
                f"and any other metrics shown. "
                f"Only include REAL data visible on the page. Do NOT make up any information. "
                f"Return as JSON: {{\"domain_rating\": float, \"backlinks\": int, "
                f"\"referring_domains\": int, \"organic_keywords\": int}}"
            )

            # Use LLM to evaluate based on REAL metrics
            evaluation = self.think_json(
                f"Evaluate this domain for flipping potential using REAL data:\n"
                f"Domain: {domain_name}\n"
                f"TLD: {d['tld']}\n"
                f"Acquisition cost: ${d.get('acquisition_cost', 0)}\n\n"
                f"REAL Wayback Machine data:\n{json.dumps(wayback_data, default=str)}\n\n"
                f"REAL SEO/backlink data:\n{json.dumps(seo_data, default=str)}\n\n"
                "Based ONLY on the real data above, estimate the domain's resale value "
                "and whether acquisition is recommended. Consider:\n"
                "- Backlink quality and quantity\n"
                "- Domain history and age\n"
                "- Keyword value in the domain name\n"
                "- TLD desirability\n\n"
                "Return: {\"estimated_value\": float, \"acquisition_recommended\": bool, "
                "\"max_bid\": float, \"resale_potential\": str, "
                "\"target_buyer\": str, \"reasoning\": str, "
                "\"data_sources_used\": [str]}"
            )

            self.db.execute(
                "UPDATE domains SET estimated_value = ?, has_backlinks = ? WHERE id = ?",
                (evaluation.get("estimated_value", 0),
                 1 if seo_data.get("backlinks", 0) > 0 else 0,
                 d["id"]),
            )
            evaluated += 1

        return {"evaluated": evaluated}

    def _analyze_market(self) -> dict[str, Any]:
        """Analyze REAL recent domain sales from actual marketplaces."""
        # Browse real domain sales data
        namebio_data = self.browse_and_extract(
            "https://namebio.com/",
            "Extract recent domain sales data shown on this page. "
            "For each sale, extract: domain name, sale price, sale date, "
            "and marketplace where it sold. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"recent_sales\": [{\"domain\": str, \"price\": float, "
            "\"date\": str, \"marketplace\": str}]}"
        )

        dnjournal_data = self.browse_and_extract(
            "https://www.dnjournal.com/domainsales.htm",
            "Extract the latest reported domain sales from DNJournal. "
            "For each sale, extract: domain name, sale price, and any notes. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"sales\": [{\"domain\": str, \"price\": float, "
            "\"notes\": str}]}"
        )

        # Search for recent market trends
        trends_data = self.search_web(
            "domain name sales trends 2024 2025 most expensive domains sold",
            "Extract real recent domain sale records and market trend data. "
            "Include actual sale prices and domain names. "
            "Only include REAL data from the search results. Do NOT make up any information. "
            "Return as JSON: {\"notable_sales\": [{\"domain\": str, \"price\": float, "
            "\"year\": int}], \"trends\": [{\"trend\": str, \"source\": str}]}"
        )

        # Use LLM to synthesize REAL market data into actionable insights
        analysis = self.think_json(
            f"Analyze the domain market based on REAL sales data:\n\n"
            f"REAL NameBio sales data:\n{json.dumps(namebio_data, default=str)}\n\n"
            f"REAL DNJournal sales data:\n{json.dumps(dnjournal_data, default=str)}\n\n"
            f"REAL market trends:\n{json.dumps(trends_data, default=str)}\n\n"
            "Using ONLY the real data above, provide:\n"
            "1. Which TLDs are selling at the highest margins?\n"
            "2. What keyword categories are commanding premium prices?\n"
            "3. What price ranges have the best flip potential?\n"
            "4. Which marketplaces are seeing the most activity?\n\n"
            "Return: {\"trends\": [{\"trend\": str, \"opportunity\": str, "
            "\"supporting_data\": str}], "
            "\"hot_categories\": [str], \"best_marketplaces\": [{\"name\": str, "
            "\"commission\": str, \"audience\": str}], "
            "\"avg_flip_margin_pct\": float, "
            "\"recommended_acquisition_budget\": str}"
        )

        self.log_action("market_analyzed", json.dumps(analysis, default=str)[:300])
        return analysis

    def _list_for_sale(self) -> dict[str, Any]:
        """ACTUALLY list acquired domains on real marketplaces."""
        acquired = self.db.execute(
            "SELECT * FROM domains WHERE status = 'acquired'"
        )
        listed = 0
        for domain in acquired:
            d = dict(domain)

            # Use LLM for listing copy (content creation is legitimate LLM use)
            listing = self.think_json(
                f"Create a compelling sales listing for domain: {d['domain_name']}\n"
                f"Estimated value: ${d.get('estimated_value', 0)}\n"
                f"Niche: {d.get('niche', 'general')}\n"
                f"Has backlinks: {d.get('has_backlinks', 0)}\n"
                f"Domain age: {d.get('domain_age_years', 0)} years\n\n"
                "Return: {\"listing_title\": str, \"description\": str, "
                "\"asking_price\": float, \"min_offer\": float, "
                "\"target_buyer_profile\": str}"
            )

            asking_price = listing.get("asking_price", d.get("estimated_value", 100))
            min_offer = listing.get("min_offer", asking_price * 0.5)
            marketplaces_listed = []

            # List on Sedo
            self.ensure_platform_account("sedo")
            sedo_result = self.platform_action(
                "sedo",
                f"List domain {d['domain_name']} for sale",
                f"Domain: {d['domain_name']}\n"
                f"Asking price: ${asking_price}\n"
                f"Minimum offer: ${min_offer}\n"
                f"Description: {listing.get('description', '')}\n"
                f"Category: {d.get('niche', 'general')}"
            )
            if sedo_result.get("status") != "error":
                marketplaces_listed.append("sedo")

            # List on Afternic
            self.ensure_platform_account("afternic")
            afternic_result = self.platform_action(
                "afternic",
                f"List domain {d['domain_name']} for sale",
                f"Domain: {d['domain_name']}\n"
                f"Asking price: ${asking_price}\n"
                f"Minimum offer: ${min_offer}\n"
                f"Description: {listing.get('description', '')}"
            )
            if afternic_result.get("status") != "error":
                marketplaces_listed.append("afternic")

            # List on Dan.com
            self.ensure_platform_account("dan.com")
            dan_result = self.platform_action(
                "dan.com",
                f"List domain {d['domain_name']} for sale",
                f"Domain: {d['domain_name']}\n"
                f"Asking price: ${asking_price}\n"
                f"Minimum offer: ${min_offer}\n"
                f"Description: {listing.get('description', '')}"
            )
            if dan_result.get("status") != "error":
                marketplaces_listed.append("dan.com")

            self.db.execute(
                "UPDATE domains SET status = 'listed', listed_price = ?, "
                "listed_on = ? WHERE id = ?",
                (asking_price, json.dumps(marketplaces_listed), d["id"]),
            )
            listed += 1

            self.log_action("domain_listed", d["domain_name"],
                            f"Listed on {marketplaces_listed} at ${asking_price}")

        return {"listed": listed}
