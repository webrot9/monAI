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

        # Deterministic progression
        if not stats:
            return ["research_niches"]
        if stats.get("building", 0) > 0:
            return ["enrich_leads"]
        if stats.get("qualifying", 0) > 0:
            return ["qualify_leads"]
        if stats.get("ready", 0) > 0:
            return ["find_buyers"]

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
            "find_buyers": self._find_buyers,
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

        self.share_knowledge(
            "opportunity", "leadgen_niches",
            json.dumps(niches.get("niches", []))[:1000],
            tags=["lead_gen", "niches"],
        )
        return niches

    def _build_list(self) -> dict[str, Any]:
        """Build a lead list by scraping REAL business data from public directories."""
        # First, plan what list to build
        plan = self.think_json(
            "Design a lead list to build. Specify:\n"
            "- Target niche and buyer persona\n"
            "- Where to find the data (public directories, Google Maps, industry listings)\n"
            "- What data points to collect\n"
            "- Qualification criteria\n"
            "- How many leads to target\n\n"
            "Return: {\"name\": str, \"niche\": str, \"source\": str, "
            "\"search_query\": str, \"location\": str, "
            "\"data_points\": [str], \"target_count\": int, "
            "\"qualification_criteria\": [str], \"price_per_lead\": float}"
        )

        name = plan.get("name", "untitled_list")
        niche = plan.get("niche", "")
        search_query = plan.get("search_query", niche)
        location = plan.get("location", "")

        # Create the list record
        list_id = self.db.execute_insert(
            "INSERT INTO lead_lists (name, niche, source, price_per_lead) VALUES (?, ?, ?, ?)",
            (name, niche, plan.get("source", "multiple"),
             plan.get("price_per_lead", 1.0)),
        )
        self.log_action("list_created", name, f"id={list_id}")

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

    def _find_buyers(self) -> dict[str, Any]:
        """Find REAL buyers for lead lists by browsing data marketplaces."""
        # Get ready lead lists
        ready_lists = self.db.execute(
            "SELECT * FROM lead_lists WHERE status IN ('qualifying', 'ready', 'building') LIMIT 3"
        )
        if not ready_lists:
            return {"status": "no_lists_ready_for_sale"}

        self.log_action("find_buyers", "Browsing real data marketplaces for buyers")

        # Browse Datarade to understand the marketplace and find buyer demand
        datarade_data = self.browse_and_extract(
            "https://datarade.ai/",
            "Extract information about how to sell data on this marketplace. "
            "Look for:\n"
            "- How to list data products for sale\n"
            "- Categories of data in demand\n"
            "- Pricing structures\n"
            "- Buyer types and what they look for\n"
            "- Any seller signup or listing process\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"listing_process\": str, \"categories\": [str], "
            "\"pricing_info\": str, \"buyer_types\": [str]}"
        )

        # Search for businesses that buy B2B leads in relevant niches
        list_niches = [dict(r).get("niche", "") for r in ready_lists]
        niche_str = ", ".join(list_niches[:3])

        buyer_search = self.search_web(
            f"companies that buy B2B leads {niche_str} lead buyers",
            "Extract company names, what types of leads they purchase, any pricing "
            "or volume information, and how to contact or sell to them.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"buyers\": [{\"company\": str, \"lead_types\": str, "
            "\"pricing\": str, \"contact_method\": str}]}"
        )

        # Search for lead marketplaces and exchanges
        marketplace_search = self.search_web(
            "B2B lead marketplace sell leads data exchange platform 2026",
            "Extract platform names, URLs, what types of data/leads they trade, "
            "commission structures, and how to list leads for sale.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"marketplaces\": [{\"name\": str, \"url\": str, "
            "\"lead_types\": str, \"commission\": str, \"listing_process\": str}]}"
        )

        # Search for agencies and companies that resell leads
        reseller_search = self.search_web(
            f"lead generation agencies buying leads {niche_str} wholesale",
            "Extract agency names, what industries they serve, and any information "
            "about their lead buying practices.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"agencies\": [{\"name\": str, \"industries\": [str], "
            "\"details\": str}]}"
        )

        # Use LLM to synthesize real data into actionable buyer list
        raw_data = {
            "datarade": datarade_data,
            "buyer_search": buyer_search,
            "marketplaces": marketplace_search,
            "resellers": reseller_search,
            "our_lists": [{"name": dict(r).get("name", ""), "niche": dict(r).get("niche", ""),
                           "total_leads": dict(r).get("total_leads", 0),
                           "qualified_leads": dict(r).get("qualified_leads", 0)}
                          for r in ready_lists],
        }
        buyers = self.think_json(
            "Based on the following REAL market data, identify the 5 best channels "
            "to sell our lead lists.\n\n"
            f"Raw research data:\n{json.dumps(raw_data, default=str)[:4000]}\n\n"
            "For each channel, specify:\n"
            "- What kind of leads they want\n"
            "- How much they typically pay per lead\n"
            "- How to list or pitch our leads to them\n"
            "- What quality standards they expect\n\n"
            "IMPORTANT: Only include buyers/platforms that appeared in the real "
            "data above. Do not invent companies.\n\n"
            "Return: {\"buyers\": [{\"business_type\": str, \"lead_criteria\": str, "
            "\"price_range_per_lead\": str, \"where_to_find\": str, "
            "\"quality_requirements\": [str], \"source\": str}]}"
        )

        self.log_action("buyers_found", f"{len(buyers.get('buyers', []))} buyer channels identified")
        return buyers
