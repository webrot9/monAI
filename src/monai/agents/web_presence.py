"""Web Presence — per-brand websites, landing pages, and domain management.

Each brand gets its own domain, landing page, and web analytics.
Uses the Provisioner for domain registration and the Executor for
deployment. Integrates with Pipeline for lead capture.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM
from monai.web.landing import generator as landing_generator
from monai.web.landing import deploy as landing_deploy

logger = logging.getLogger(__name__)

WEB_PRESENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_websites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    domain TEXT,                        -- e.g. myproduct.com
    registrar TEXT,                     -- namecheap, cloudflare, etc.
    hosting TEXT,                       -- vercel, netlify, cloudflare_pages, vps
    site_type TEXT DEFAULT 'landing',   -- landing, blog, saas_app, store
    status TEXT DEFAULT 'planned',      -- planned, domain_acquired, building, live, suspended
    ssl_enabled INTEGER DEFAULT 0,
    analytics_id TEXT,                  -- GA4 or Plausible tracking ID
    metadata TEXT,                      -- JSON: deployment config
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(brand, domain)
);

CREATE TABLE IF NOT EXISTS brand_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    website_id INTEGER NOT NULL REFERENCES brand_websites(id),
    brand TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '/',     -- URL path: /, /pricing, /blog/post-1
    page_type TEXT NOT NULL,            -- landing, pricing, about, blog_post, lead_capture
    title TEXT,
    content TEXT,                       -- HTML or markdown content
    meta_description TEXT,
    meta_keywords TEXT,
    og_image TEXT,
    status TEXT DEFAULT 'draft',        -- draft, published, archived
    page_views INTEGER DEFAULT 0,
    conversions INTEGER DEFAULT 0,      -- form submissions / signups
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS web_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    website_id INTEGER NOT NULL REFERENCES brand_websites(id),
    brand TEXT NOT NULL,
    date TEXT NOT NULL,                 -- YYYY-MM-DD
    page_views INTEGER DEFAULT 0,
    unique_visitors INTEGER DEFAULT 0,
    bounce_rate REAL DEFAULT 0,
    avg_session_seconds INTEGER DEFAULT 0,
    conversions INTEGER DEFAULT 0,
    top_referrers TEXT,                 -- JSON list
    top_pages TEXT,                     -- JSON list
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(website_id, date)
);
"""


class WebPresence(BaseAgent):
    """Manages websites and landing pages per brand."""

    name = "web_presence"
    description = (
        "Creates and manages websites per brand — domains, landing pages, "
        "SEO content, lead capture forms, and web analytics."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(WEB_PRESENCE_SCHEMA)

    def plan(self) -> list[str]:
        return [
            "Check which brands need websites",
            "Acquire domains for brands without one",
            "Generate landing page content per brand",
            "Deploy sites and verify they're live",
            "Track web analytics and optimize",
        ]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Full web presence cycle."""
        brand_filter = kwargs.get("brand")
        brands = self._get_brands(brand_filter)

        results = {}

        # Always regenerate crowdfunding page with latest funding progress
        results["crowdfunding"] = self.generate_crowdfunding_page()

        for brand in brands:
            sites = self._get_brand_sites(brand)
            if not sites:
                results[brand] = {"status": "no_sites"}
                continue

            pages_created = 0
            for site in sites:
                if site["status"] == "live":
                    pages = self._plan_pages(brand, site)
                    pages_created += len(pages)

            results[brand] = {
                "sites": len(sites),
                "live": len([s for s in sites if s["status"] == "live"]),
                "pages_created": pages_created,
            }

        return {"brands_processed": len(brands), "per_brand": results}

    # ── Site Management ──────────────────────────────────────

    def _get_brands(self, brand_filter: str | None = None) -> list[str]:
        if brand_filter:
            return [brand_filter]
        rows = self.db.execute(
            "SELECT DISTINCT brand FROM brand_websites"
        )
        return [r["brand"] for r in rows]

    def register_site(self, brand: str, domain: str,
                      registrar: str = "", hosting: str = "vercel",
                      site_type: str = "landing") -> dict[str, Any]:
        """Register a new website for a brand."""
        site_id = self.db.execute_insert(
            "INSERT OR IGNORE INTO brand_websites "
            "(brand, domain, registrar, hosting, site_type, status) "
            "VALUES (?, ?, ?, ?, ?, 'planned')",
            (brand, domain, registrar, hosting, site_type),
        )
        self.log_action("register_site", f"{brand}: {domain}")
        self.share_knowledge(
            "website", f"site_{brand}",
            f"Website registered: {domain} for {brand} ({site_type})",
            tags=["website", brand],
        )
        return {"site_id": site_id, "brand": brand,
                "domain": domain, "status": "planned"}

    def activate_site(self, brand: str, domain: str,
                      analytics_id: str = "") -> dict[str, Any]:
        """Mark a site as live after deployment."""
        self.db.execute(
            "UPDATE brand_websites SET status = 'live', ssl_enabled = 1, "
            "analytics_id = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE brand = ? AND domain = ?",
            (analytics_id, brand, domain),
        )
        return {"brand": brand, "domain": domain, "status": "live"}

    def _get_brand_sites(self, brand: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM brand_websites WHERE brand = ?", (brand,)
        )
        return [dict(r) for r in rows]

    def get_all_sites(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM brand_websites ORDER BY brand")
        return [dict(r) for r in rows]

    # ── Page Management ──────────────────────────────────────

    def create_page(self, website_id: int, brand: str, path: str,
                    page_type: str, title: str = "",
                    content: str = "", meta_description: str = "",
                    meta_keywords: str = "") -> int:
        """Create a page for a website."""
        return self.db.execute_insert(
            "INSERT INTO brand_pages "
            "(website_id, brand, path, page_type, title, content, "
            "meta_description, meta_keywords) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (website_id, brand, path, page_type, title, content,
             meta_description, meta_keywords),
        )

    def generate_landing_page(self, brand: str,
                              website_id: int) -> dict[str, Any]:
        """Use LLM to generate landing page content for a brand."""
        content = self.think_json(
            f"Generate a high-converting landing page for the '{brand}' brand.\n\n"
            "Include:\n"
            "1. Headline (benefit-driven, max 10 words)\n"
            "2. Subheadline (clarifying value prop)\n"
            "3. 3 feature bullets with icons\n"
            "4. Social proof section\n"
            "5. CTA button text\n"
            "6. SEO meta description (155 chars)\n"
            "7. SEO keywords (comma-separated)\n\n"
            "Return JSON: {{\"headline\": str, \"subheadline\": str, "
            "\"features\": [{{\"icon\": str, \"title\": str, \"description\": str}}], "
            "\"social_proof\": str, \"cta_text\": str, "
            "\"meta_description\": str, \"meta_keywords\": str}}"
        )

        page_content = json.dumps(content)
        page_id = self.create_page(
            website_id, brand, "/", "landing",
            title=content.get("headline", f"{brand} - Landing"),
            content=page_content,
            meta_description=content.get("meta_description", ""),
            meta_keywords=content.get("meta_keywords", ""),
        )

        self.log_action("generate_landing", f"{brand}: {content.get('headline', '')[:50]}")
        return {"page_id": page_id, "content": content}

    def generate_crowdfunding_page(
        self,
        stripe_links: dict[int, str] | None = None,
        kofi_url: str | None = None,
        monero_address: str | None = None,
    ) -> dict[str, Any]:
        """Generate the crowdfunding landing page using the real template generator.

        Uses web/landing/generator.py to fill the HTML template with live
        funding progress from the DB and real payment links.
        """
        try:
            output_path = landing_generator.generate(
                config=self.config,
                db=self.db,
                stripe_links=stripe_links,
                kofi_url=kofi_url,
                monero_address=monero_address,
            )
            self.log_action("generate_crowdfunding_page", f"Generated at {output_path}")
            return {"status": "generated", "path": str(output_path)}
        except Exception as e:
            logger.error(f"Crowdfunding page generation failed: {e}")
            return {"status": "error", "error": str(e)}

    def deploy_crowdfunding_page(
        self,
        provider: str = "netlify",
        site_name: str | None = None,
    ) -> dict[str, Any]:
        """Deploy the crowdfunding landing page to a hosting provider.

        First generates the page with live data, then deploys it.
        Uses web/landing/deploy.py for Netlify, Vercel, or Cloudflare Pages.
        """
        # Generate fresh page with live data
        gen_result = self.generate_crowdfunding_page()
        if gen_result.get("status") != "generated":
            return {"status": "error", "error": f"Generation failed: {gen_result.get('error')}"}

        try:
            deploy_result = landing_deploy.deploy(
                provider=provider,
                site_name=site_name,
            )
            result = {
                "status": "deployed" if deploy_result.success else "deploy_failed",
                "url": deploy_result.url,
                "provider": deploy_result.provider,
                "error": deploy_result.error,
            }
            if deploy_result.success:
                self.log_action("deploy_crowdfunding", f"Live at {deploy_result.url}")
                self.share_knowledge(
                    "website", "crowdfunding_page",
                    f"Crowdfunding page live at {deploy_result.url}",
                    tags=["crowdfunding", "website", "live"],
                )
            return result
        except Exception as e:
            logger.error(f"Crowdfunding page deploy failed: {e}")
            return {"status": "error", "error": str(e)}

    def _plan_pages(self, brand: str,
                    site: dict[str, Any]) -> list[dict[str, Any]]:
        """Plan which pages to create for a live site."""
        existing = self.db.execute(
            "SELECT path FROM brand_pages WHERE website_id = ?",
            (site["id"],),
        )
        existing_paths = {r["path"] for r in existing}

        pages_created = []
        if "/" not in existing_paths:
            result = self.generate_landing_page(brand, site["id"])
            pages_created.append(result)

        return pages_created

    def get_brand_pages(self, brand: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM brand_pages WHERE brand = ? ORDER BY path",
            (brand,),
        )
        return [dict(r) for r in rows]

    def publish_page(self, page_id: int) -> dict[str, Any]:
        """Mark a page as published."""
        self.db.execute(
            "UPDATE brand_pages SET status = 'published', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (page_id,),
        )
        return {"status": "published", "page_id": page_id}

    # ── Analytics ────────────────────────────────────────────

    def record_analytics(self, website_id: int, brand: str,
                         date: str, page_views: int = 0,
                         unique_visitors: int = 0, bounce_rate: float = 0,
                         avg_session_seconds: int = 0, conversions: int = 0,
                         top_referrers: list | None = None,
                         top_pages: list | None = None) -> int:
        """Record daily web analytics."""
        return self.db.execute_insert(
            "INSERT OR REPLACE INTO web_analytics "
            "(website_id, brand, date, page_views, unique_visitors, "
            "bounce_rate, avg_session_seconds, conversions, "
            "top_referrers, top_pages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (website_id, brand, date, page_views, unique_visitors,
             bounce_rate, avg_session_seconds, conversions,
             json.dumps(top_referrers or []),
             json.dumps(top_pages or [])),
        )

    def get_analytics(self, brand: str,
                      days: int = 30) -> list[dict[str, Any]]:
        """Get recent analytics for a brand."""
        rows = self.db.execute(
            "SELECT * FROM web_analytics WHERE brand = ? "
            "ORDER BY date DESC LIMIT ?",
            (brand, days),
        )
        return [dict(r) for r in rows]

    def get_analytics_summary(self, brand: str | None = None) -> dict[str, Any]:
        """Aggregate analytics across brands."""
        query = (
            "SELECT brand, SUM(page_views) as total_views, "
            "SUM(unique_visitors) as total_visitors, "
            "SUM(conversions) as total_conversions, "
            "AVG(bounce_rate) as avg_bounce_rate "
            "FROM web_analytics "
        )
        params: tuple = ()
        if brand:
            query += "WHERE brand = ? "
            params = (brand,)
        query += "GROUP BY brand"
        rows = self.db.execute(query, params)
        return {r["brand"]: dict(r) for r in rows}
