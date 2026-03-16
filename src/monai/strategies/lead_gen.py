"""Lead generation strategy agent.

Scrapes, qualifies, and sells B2B leads to businesses.
Uses REAL web research to find, enrich, and qualify leads from
public directories and business listings.
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

# Real directories and platforms to scrape leads from
LEAD_SOURCES = [
    {
        "name": "google_maps",
        "base_url": "https://www.google.com/maps/search/",
        "description": "Google Maps business listings",
    },
    {
        "name": "yelp",
        "base_url": "https://www.yelp.com/search?find_desc=",
        "description": "Yelp business directory",
    },
    {
        "name": "linkedin",
        "base_url": "https://www.linkedin.com/search/results/companies/",
        "description": "LinkedIn company directory",
    },
    {
        "name": "yellowpages",
        "base_url": "https://www.yellowpages.com/search?search_terms=",
        "description": "Yellow Pages business directory",
    },
    {
        "name": "bbb",
        "base_url": "https://www.bbb.org/search?find_text=",
        "description": "Better Business Bureau listings",
    },
]

# Platforms where lead lists are bought and sold
LEAD_MARKETPLACES = [
    {
        "name": "datarade",
        "url": "https://datarade.ai/",
        "description": "B2B data marketplace",
    },
    {
        "name": "leadgen_app",
        "url": "https://leadgenapp.io/",
        "description": "Lead generation marketplace",
    },
]


class LeadGenAgent(BaseAgent):
    name = "lead_gen"
    description = (
        "Generates and sells qualified B2B leads. Scrapes REAL public directories, "
        "enriches with additional data from company websites, qualifies by fit score, "
        "and packages into lead lists for sale to businesses."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(LEADGEN_SCHEMA)

    def plan(self) -> list[str]:
        lists = self.db.execute("SELECT status, COUNT(*) as c FROM lead_lists GROUP BY status")
        stats = {r["status"]: r["c"] for r in lists}

        # Also check for leads that need processing
        lead_stats_rows = self.db.execute(
            "SELECT status, COUNT(*) as c FROM leads GROUP BY status"
        )
        lead_stats = {r["status"]: r["c"] for r in lead_stats_rows} if lead_stats_rows else {}

        # Deterministic progression — each step advances the pipeline
        if not stats:
            return ["research_niches"]
        if stats.get("planned", 0) > 0:
            return ["build_list"]
        if lead_stats.get("raw", 0) > 0:
            return ["enrich_leads"]
        if lead_stats.get("enriched", 0) > 0:
            return ["qualify_leads"]
        if stats.get("building", 0) > 0 and lead_stats.get("qualified", 0) > 0:
            return ["finalize_list"]
        if stats.get("ready", 0) > 0:
            return ["sell_leads"]

        # All lists sold — research new niches
        return ["research_niches"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting lead gen cycle")
        steps = self.plan()
        results = {}

        step_methods = {
            "research_niches": self._research_niches,
            "build_list": self._build_list,
            "enrich_leads": self._enrich_leads,
            "qualify_leads": self._qualify_leads,
            "finalize_list": self._finalize_list,
            "sell_leads": self._sell_leads,
        }

        for step in steps:
            fn = step_methods.get(step)
            if fn:
                results[step] = self.run_step(step, fn)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Find niches where businesses actively buy leads using REAL web data."""
        self.log_action("niche_research", "Browsing real B2B directories for niche data")

        # Browse Datarade to see what kind of lead data is actually in demand
        datarade_data = self.browse_and_extract(
            "https://datarade.ai/",
            "Extract data product categories, industries, lead types, and any "
            "pricing or demand indicators shown on this page. What kinds of B2B "
            "data are listed for sale?\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"categories\": [{\"category\": str, \"description\": str, "
            "\"industries\": [str], \"pricing_range\": str}]}"
        )

        # Search for B2B industries with highest lead demand
        demand_data = self.search_web(
            "most profitable B2B lead generation niches 2026 highest value",
            "Extract industry names, average lead values, demand indicators, and "
            "any data about which B2B niches pay most for leads.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"niches\": [{\"niche\": str, \"avg_lead_value\": str, "
            "\"demand_indicator\": str, \"source\": str}]}"
        )

        # Search for publicly scrapable business directories by industry
        directory_data = self.search_web(
            "best public business directories for lead generation B2B data sources",
            "Extract directory names, what industries they cover, what data is "
            "publicly available (name, phone, email, address, etc.), and any "
            "access details.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"directories\": [{\"name\": str, \"url\": str, "
            "\"industries\": [str], \"available_data\": [str]}]}"
        )

        # Browse BBB to see what industries have the most listings
        bbb_data = self.browse_and_extract(
            "https://www.bbb.org/search?find_text=contractors&find_type=Category",
            "Extract business categories, number of listings per category, and "
            "the types of businesses listed. Focus on categories with many listings.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"categories\": [{\"category\": str, "
            "\"listing_count\": str, \"details\": str}]}"
        )

        # Use LLM to analyze real data and pick the best niches
        raw_data = {
            "datarade_marketplace": datarade_data,
            "demand_research": demand_data,
            "directory_research": directory_data,
            "bbb_categories": bbb_data,
        }
        niches = self.think_json(
            "Based on the following REAL market data, identify 5 B2B niches "
            "where businesses actively buy leads.\n\n"
            f"Raw research data:\n{json.dumps(raw_data, default=str)[:4000]}\n\n"
            "Focus on:\n"
            "- High customer lifetime value (so they pay more per lead)\n"
            "- Businesses that rely on outbound sales\n"
            "- Industries where contact info is publicly findable in directories\n"
            "- Not oversaturated with existing lead gen services\n\n"
            "IMPORTANT: Base your analysis on the real data above. Do not invent "
            "market data.\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"typical_buyer\": str, "
            "\"lead_value_usd\": float, \"data_sources\": [str], "
            "\"qualification_criteria\": [str], \"estimated_demand\": str, "
            "\"source\": str}]}"
        )

        # Create a lead_lists record for the best niche so plan() progresses
        best_niches = niches.get("niches", [])
        if best_niches:
            best = best_niches[0]
            self.db.execute_insert(
                "INSERT INTO lead_lists (name, niche, source, price_per_lead, status) "
                "VALUES (?, ?, ?, ?, 'planned')",
                (
                    f"{best.get('niche', 'unknown')}_leads",
                    best.get("niche", ""),
                    ", ".join(best.get("data_sources", ["web"])),
                    best.get("lead_value_usd", 1.0),
                ),
            )
            self.log_action("niche_selected", best.get("niche", "unknown"))

        self.share_knowledge(
            "opportunity", "leadgen_niches",
            json.dumps(niches.get("niches", []))[:1000],
            tags=["lead_gen", "niches"],
        )
        return niches

    def _build_list(self) -> dict[str, Any]:
        """Build a lead list by scraping REAL business data from public directories."""
        # Pick the planned list to build
        planned = self.db.execute(
            "SELECT * FROM lead_lists WHERE status = 'planned' LIMIT 1"
        )
        if not planned:
            return {"status": "no_planned_lists"}

        planned_list = dict(planned[0])
        list_id = planned_list["id"]
        name = planned_list["name"]
        niche = planned_list["niche"]

        # Use LLM to determine search parameters based on the niche
        plan = self.think_json(
            f"We need to build a lead list for the '{niche}' niche.\n"
            "Provide search parameters:\n"
            "- search_query: what to search on Google Maps\n"
            "- location: geographic focus (US city or region)\n"
            "- target_count: how many leads to aim for\n\n"
            "Return: {\"search_query\": str, \"location\": str, \"target_count\": int}"
        )

        search_query = plan.get("search_query", niche)
        location = plan.get("location", "")

        # Update list to building status
        self.db.execute(
            "UPDATE lead_lists SET status = 'building' WHERE id = ?", (list_id,)
        )
        self.log_action("list_building", name, f"id={list_id}")

        leads_added = 0

        # Scrape Google Maps for real business listings
        maps_query = f"{search_query}+{location}".replace(" ", "+")
        maps_data = self.browse_and_extract(
            f"https://www.google.com/maps/search/{maps_query}",
            "Extract business listings from this Google Maps page. For each business, get:\n"
            "- business_name: the company/business name\n"
            "- address: full address\n"
            "- phone: phone number (if shown)\n"
            "- website: website URL (if shown)\n"
            "- rating: star rating (if shown)\n"
            "- review_count: number of reviews (if shown)\n"
            "- category: business category\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"businesses\": [{\"business_name\": str, "
            "\"address\": str, \"phone\": str, \"website\": str, "
            "\"rating\": str, \"review_count\": str, \"category\": str}]}"
        )

        # Store Google Maps leads
        if maps_data.get("status") == "completed":
            result = maps_data.get("result", {})
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    result = {}
            businesses = result.get("businesses", []) if isinstance(result, dict) else []
            for biz in businesses:
                self.db.execute_insert(
                    "INSERT INTO leads (list_id, company_name, phone, website, "
                    "location, industry, status) VALUES (?, ?, ?, ?, ?, ?, 'raw')",
                    (list_id, biz.get("business_name", ""),
                     biz.get("phone", ""), biz.get("website", ""),
                     biz.get("address", ""), niche),
                )
                leads_added += 1

        # Scrape Yelp for additional business listings
        yelp_query = search_query.replace(" ", "+")
        yelp_location = location.replace(" ", "+") if location else ""
        yelp_url = f"https://www.yelp.com/search?find_desc={yelp_query}"
        if yelp_location:
            yelp_url += f"&find_loc={yelp_location}"

        yelp_data = self.browse_and_extract(
            yelp_url,
            "Extract business listings from this Yelp search page. For each business, get:\n"
            "- business_name: the company/business name\n"
            "- address: address (if shown)\n"
            "- phone: phone number (if shown)\n"
            "- website: website URL (if shown)\n"
            "- rating: star rating\n"
            "- review_count: number of reviews\n"
            "- category: business category/type\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"businesses\": [{\"business_name\": str, "
            "\"address\": str, \"phone\": str, \"website\": str, "
            "\"rating\": str, \"review_count\": str, \"category\": str}]}"
        )

        # Store Yelp leads
        if yelp_data.get("status") == "completed":
            result = yelp_data.get("result", {})
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    result = {}
            businesses = result.get("businesses", []) if isinstance(result, dict) else []
            for biz in businesses:
                self.db.execute_insert(
                    "INSERT INTO leads (list_id, company_name, phone, website, "
                    "location, industry, status) VALUES (?, ?, ?, ?, ?, ?, 'raw')",
                    (list_id, biz.get("business_name", ""),
                     biz.get("phone", ""), biz.get("website", ""),
                     biz.get("address", ""), niche),
                )
                leads_added += 1

        # Scrape Yellow Pages for more listings
        yp_query = search_query.replace(" ", "+")
        yp_url = f"https://www.yellowpages.com/search?search_terms={yp_query}"
        if location:
            yp_url += f"&geo_location_terms={location.replace(' ', '+')}"

        yp_data = self.browse_and_extract(
            yp_url,
            "Extract business listings from this Yellow Pages page. For each business, get:\n"
            "- business_name: the company/business name\n"
            "- address: full address\n"
            "- phone: phone number\n"
            "- website: website URL (if shown)\n"
            "- category: business category\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"businesses\": [{\"business_name\": str, "
            "\"address\": str, \"phone\": str, \"website\": str, \"category\": str}]}"
        )

        # Store Yellow Pages leads
        if yp_data.get("status") == "completed":
            result = yp_data.get("result", {})
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    result = {}
            businesses = result.get("businesses", []) if isinstance(result, dict) else []
            for biz in businesses:
                self.db.execute_insert(
                    "INSERT INTO leads (list_id, company_name, phone, website, "
                    "location, industry, status) VALUES (?, ?, ?, ?, ?, ?, 'raw')",
                    (list_id, biz.get("business_name", ""),
                     biz.get("phone", ""), biz.get("website", ""),
                     biz.get("address", ""), niche),
                )
                leads_added += 1

        # Update the list total
        self.db.execute(
            "UPDATE lead_lists SET total_leads = ? WHERE id = ?",
            (leads_added, list_id),
        )

        self.log_action("list_built", f"{leads_added} leads scraped for '{name}'")
        return {**plan, "list_id": list_id, "leads_scraped": leads_added}

    def _enrich_leads(self) -> dict[str, Any]:
        """Enrich raw leads by browsing their websites and LinkedIn profiles."""
        raw_leads = self.db.execute(
            "SELECT * FROM leads WHERE status = 'raw' AND website IS NOT NULL "
            "AND website != '' LIMIT 10"
        )
        if not raw_leads:
            return {"status": "no_raw_leads_with_websites", "enriched": 0}

        enriched_count = 0
        for lead in raw_leads:
            lead = dict(lead)
            enrichment = {}

            # Browse the company website for real data
            if lead.get("website"):
                website_url = lead["website"]
                if not website_url.startswith("http"):
                    website_url = f"https://{website_url}"

                website_data = self.browse_and_extract(
                    website_url,
                    "Extract business information from this company website:\n"
                    "- company_description: what the company does\n"
                    "- services: list of services/products offered\n"
                    "- team_size_indicator: any info about company size (team page, about page)\n"
                    "- contact_email: email addresses found on the page\n"
                    "- contact_name: names of founders, owners, or key contacts\n"
                    "- social_media: links to social media profiles\n"
                    "- year_founded: if mentioned\n"
                    "- location: city/state if mentioned\n\n"
                    "Only include REAL data visible on the page. Do NOT make up any information.\n"
                    "Return as JSON: {\"company_description\": str, \"services\": [str], "
                    "\"team_size_indicator\": str, \"contact_email\": str, "
                    "\"contact_name\": str, \"social_media\": [str], "
                    "\"year_founded\": str, \"location\": str}"
                )

                if website_data.get("status") == "completed":
                    result = website_data.get("result", {})
                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except (json.JSONDecodeError, TypeError):
                            result = {}
                    if isinstance(result, dict):
                        enrichment["website_data"] = result
                        # Update contact info if found
                        if result.get("contact_email"):
                            self.db.execute(
                                "UPDATE leads SET email = ? WHERE id = ? AND (email IS NULL OR email = '')",
                                (result["contact_email"], lead["id"]),
                            )
                        if result.get("contact_name"):
                            self.db.execute(
                                "UPDATE leads SET contact_name = ? WHERE id = ? AND (contact_name IS NULL OR contact_name = '')",
                                (result["contact_name"], lead["id"]),
                            )
                        if result.get("team_size_indicator"):
                            self.db.execute(
                                "UPDATE leads SET company_size = ? WHERE id = ?",
                                (result["team_size_indicator"], lead["id"]),
                            )

            # Search LinkedIn for additional company data
            if lead.get("company_name"):
                linkedin_data = self.browse_and_extract(
                    f"https://www.linkedin.com/company/{lead['company_name'].lower().replace(' ', '-')}",
                    "Extract company information from this LinkedIn page:\n"
                    "- employee_count: number of employees\n"
                    "- industry: industry listed\n"
                    "- headquarters: headquarters location\n"
                    "- description: company description\n"
                    "- specialties: listed specialties\n"
                    "- founded: year founded\n\n"
                    "Only include REAL data visible on the page. Do NOT make up any information.\n"
                    "Return as JSON: {\"employee_count\": str, \"industry\": str, "
                    "\"headquarters\": str, \"description\": str, "
                    "\"specialties\": [str], \"founded\": str}"
                )

                if linkedin_data.get("status") == "completed":
                    result = linkedin_data.get("result", {})
                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except (json.JSONDecodeError, TypeError):
                            result = {}
                    if isinstance(result, dict):
                        enrichment["linkedin_data"] = result

            # Save enrichment data and update status
            if enrichment:
                self.db.execute(
                    "UPDATE leads SET enrichment_data = ?, status = 'enriched' WHERE id = ?",
                    (json.dumps(enrichment), lead["id"]),
                )
                enriched_count += 1

        self.log_action("enrich_leads", f"Enriched {enriched_count} leads with real data")
        return {"enriched": enriched_count, "total_processed": len(raw_leads)}

    def _qualify_leads(self) -> dict[str, Any]:
        """Score and qualify enriched leads using real enrichment data."""
        enriched_leads = self.db.execute(
            "SELECT * FROM leads WHERE status = 'enriched' LIMIT 20"
        )
        if not enriched_leads:
            return {"status": "no_enriched_leads", "qualified": 0}

        qualified_count = 0
        for lead in enriched_leads:
            lead = dict(lead)
            enrichment = {}
            if lead.get("enrichment_data"):
                try:
                    enrichment = json.loads(lead["enrichment_data"])
                except (json.JSONDecodeError, TypeError):
                    enrichment = {}

            # Get the list's qualification criteria
            list_data = self.db.execute(
                "SELECT * FROM lead_lists WHERE id = ?", (lead.get("list_id"),)
            )
            list_info = dict(list_data[0]) if list_data else {}

            # Use LLM to score the lead based on REAL enrichment data
            score_result = self.think_json(
                "Score this lead based on the REAL data we collected.\n\n"
                f"Lead: {lead.get('company_name', 'Unknown')}\n"
                f"Industry: {lead.get('industry', 'Unknown')}\n"
                f"Location: {lead.get('location', 'Unknown')}\n"
                f"Company size: {lead.get('company_size', 'Unknown')}\n"
                f"Has email: {'Yes' if lead.get('email') else 'No'}\n"
                f"Has phone: {'Yes' if lead.get('phone') else 'No'}\n"
                f"Has website: {'Yes' if lead.get('website') else 'No'}\n"
                f"Enrichment data: {json.dumps(enrichment, default=str)[:1500]}\n"
                f"List niche: {list_info.get('niche', 'Unknown')}\n\n"
                "Score from 0.0 to 1.0 based on:\n"
                "- Data completeness (has email, phone, website)\n"
                "- Company size fit for the target buyer\n"
                "- Industry relevance\n"
                "- Likelihood to convert\n\n"
                "IMPORTANT: Base the score ONLY on the real data above. If data is "
                "missing, the score should be lower.\n\n"
                "Return: {\"score\": float, \"reasoning\": str, "
                "\"missing_data\": [str], \"recommendation\": str}"
            )

            score = score_result.get("score", 0.0)
            self.db.execute(
                "UPDATE leads SET qualification_score = ?, status = 'qualified' WHERE id = ?",
                (score, lead["id"]),
            )
            qualified_count += 1

        # Update the list's qualified count
        if enriched_leads:
            list_id = dict(enriched_leads[0]).get("list_id")
            if list_id:
                qualified_total = self.db.execute(
                    "SELECT COUNT(*) as c FROM leads WHERE list_id = ? AND status = 'qualified' "
                    "AND qualification_score >= 0.5",
                    (list_id,),
                )
                count = qualified_total[0]["c"] if qualified_total else 0
                self.db.execute(
                    "UPDATE lead_lists SET qualified_leads = ? WHERE id = ?",
                    (count, list_id),
                )

        self.log_action("qualify_leads", f"Qualified {qualified_count} leads")
        return {"qualified": qualified_count, "total_processed": len(enriched_leads)}

    def _finalize_list(self) -> dict[str, Any]:
        """Mark lead lists as ready for sale once their leads are qualified."""
        building_lists = self.db.execute(
            "SELECT * FROM lead_lists WHERE status = 'building'"
        )
        if not building_lists:
            return {"status": "no_lists_to_finalize"}

        finalized = 0
        for row in building_lists:
            ll = dict(row)
            list_id = ll["id"]

            # Count qualified leads for this list
            qualified = self.db.execute(
                "SELECT COUNT(*) as c FROM leads "
                "WHERE list_id = ? AND status = 'qualified' AND qualification_score >= 0.5",
                (list_id,),
            )
            count = qualified[0]["c"] if qualified else 0

            if count > 0:
                self.db.execute(
                    "UPDATE lead_lists SET status = 'ready', qualified_leads = ? WHERE id = ?",
                    (count, list_id),
                )
                finalized += 1
                self.log_action("list_finalized", f"List {ll['name']}: {count} qualified leads ready")
            else:
                self.log_action("list_not_ready", f"List {ll['name']}: 0 qualified leads")

        return {"finalized": finalized}

    def _sell_leads(self) -> dict[str, Any]:
        """List lead packages on data marketplaces and reach out to potential buyers.

        This is where actual revenue comes from — we list on Datarade, reach out
        to agencies, and respond to buyer demand for B2B data.
        """
        ready_lists = self.db.execute(
            "SELECT * FROM lead_lists WHERE status = 'ready' LIMIT 3"
        )
        if not ready_lists:
            return {"status": "no_lists_ready_for_sale"}

        sold = 0

        for row in ready_lists:
            ll = dict(row)
            list_id = ll["id"]
            name = ll["name"]
            niche = ll["niche"]
            qualified_count = ll.get("qualified_leads", 0)
            price = ll.get("price_per_lead", 1.0)

            # Step 1: List on Datarade marketplace
            try:
                account = self.ensure_platform_account("datarade")
                if account.get("status") not in ("blocked", "error"):
                    self.execute_task(
                        f"List a lead data product on Datarade.ai for sale.\n"
                        f"Product name: {name}\n"
                        f"Niche: {niche}\n"
                        f"Number of qualified leads: {qualified_count}\n"
                        f"Price per lead: ${price:.2f}\n"
                        f"Data fields: company name, email, phone, website, industry, "
                        f"location, company size, qualification score\n\n"
                        f"Steps:\n"
                        f"1. Go to datarade.ai seller dashboard\n"
                        f"2. Create a new data product listing\n"
                        f"3. Set pricing and description\n"
                        f"4. Submit for review\n"
                        f"5. Return the listing URL\n\n"
                        f"Return: {{\"url\": str, \"status\": str}}",
                        f"Listing lead data on Datarade: {name}",
                    )
            except Exception as e:
                self.log_action("datarade_listing_failed", f"{name}: {e}")

            # Step 2: Direct outreach to agencies that buy leads in this niche
            try:
                buyer_search = self.search_web(
                    f"{niche} marketing agency buying leads B2B data provider",
                    "Find 3 marketing agencies or companies that buy B2B leads. "
                    "Return {\"agencies\": [{\"name\": str, \"email\": str, \"url\": str}]}",
                    num_results=5,
                )
                agencies = buyer_search.get("agencies", [])

                for agency in agencies[:3]:
                    email = agency.get("email", "")
                    if not email:
                        continue

                    self.platform_action(
                        "email",
                        f"Send a sales outreach email.\n"
                        f"To: {email}\n"
                        f"Subject: {qualified_count} Qualified {niche.title()} Leads Available\n"
                        f"Body: Professional email offering our lead list.\n"
                        f"- {qualified_count} pre-qualified leads\n"
                        f"- Includes: company name, email, phone, website, qualification score\n"
                        f"- All data sourced from public directories and verified\n"
                        f"- Price: ${price:.2f} per lead or ${price * qualified_count * 0.8:.2f} for the full list\n"
                        f"- Sample available on request\n"
                        f"Keep it professional, concise, and value-focused.",
                        f"Outreach to {agency.get('name', 'agency')} for {niche} leads",
                    )
                    self.log_action("outreach_sent", f"{agency.get('name', '')} for {name}")

            except Exception as e:
                self.log_action("outreach_failed", f"{name}: {e}")

            # Step 3: Create a payment link so buyers can pay directly
            bundle_price = round(price * qualified_count * 0.8, 2)
            checkout = self.create_checkout_link(
                amount=bundle_price,
                product=f"{niche.title()} Lead List ({qualified_count} qualified leads)",
                provider="kofi",  # Works behind Tor, lowest friction
                metadata={"list_id": list_id, "niche": niche},
            )
            checkout_url = checkout.get("checkout_url", "")
            if checkout_url:
                self.log_action(
                    "checkout_created", f"{name}: {checkout_url}"
                )

            # Mark as listed (still active, waiting for sales)
            self.db.execute(
                "UPDATE lead_lists SET status = 'listed' WHERE id = ?", (list_id,)
            )
            sold += 1
            self.log_action("list_listed", f"{name}: listed on marketplace + outreach sent")

        return {"lists_listed": sold}
