"""SaaS strategy agent — builds full SaaS products with market research.

Unlike micro-SaaS (quick tools), this agent builds larger products with:
- Market research and validation
- Competitive analysis
- Feature prioritization
- Full-stack development via Coder
- Landing page creation
- Pricing strategy
- Growth/marketing plan
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

SAAS_SCHEMA = """
CREATE TABLE IF NOT EXISTS saas_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tagline TEXT,
    problem TEXT NOT NULL,
    solution TEXT NOT NULL,
    target_market TEXT NOT NULL,
    market_size_estimate TEXT,
    competitors TEXT,                        -- JSON list of competitors
    differentiator TEXT,
    tech_stack TEXT,                         -- JSON: {backend, frontend, db, hosting}
    pricing_model TEXT,                      -- JSON: {free_tier, pro_tier, enterprise_tier}
    mrr_target REAL DEFAULT 0.0,
    current_mrr REAL DEFAULT 0.0,
    status TEXT DEFAULT 'researching',       -- researching, validated, building, beta, launched, growing, pivoting
    validation_score REAL DEFAULT 0.0,       -- 0-1 confidence score
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    launched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS saas_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER REFERENCES saas_products(id),
    feature_name TEXT NOT NULL,
    description TEXT,
    priority TEXT DEFAULT 'medium',          -- critical, high, medium, low, nice_to_have
    complexity TEXT DEFAULT 'medium',        -- trivial, low, medium, high, very_high
    status TEXT DEFAULT 'planned',           -- planned, building, testing, shipped
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER REFERENCES saas_products(id),
    research_type TEXT NOT NULL,             -- competitor_analysis, user_interviews, keyword_research, market_sizing
    findings TEXT NOT NULL,                  -- JSON findings
    confidence REAL DEFAULT 0.0,            -- 0-1 confidence level
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class SaaSAgent(BaseAgent):
    name = "saas"
    description = (
        "Builds full SaaS products with proper market research and validation. "
        "Identifies market gaps, validates demand, designs architecture, builds "
        "MVPs with the Coder agent, and launches with pricing and marketing strategy."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(SAAS_SCHEMA)

    def plan(self) -> list[str]:
        products = self.db.execute("SELECT status, COUNT(*) as c FROM saas_products GROUP BY status")
        stats = {r["status"]: r["c"] for r in products}
        plan = self.think_json(
            f"SaaS portfolio: {json.dumps(stats)}. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: discover_opportunities, validate_idea, competitor_analysis, "
            "design_architecture, build_mvp, create_landing_page, plan_launch, "
            "analyze_metrics, plan_growth.",
        )
        return plan.get("steps", ["discover_opportunities"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting SaaS cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "discover_opportunities":
                results["opportunities"] = self._discover_opportunities()
            elif step == "validate_idea":
                results["validation"] = self._validate_idea()
            elif step == "competitor_analysis":
                results["competitors"] = self._competitor_analysis()
            elif step == "design_architecture":
                results["architecture"] = self._design_architecture()
            elif step == "build_mvp":
                results["build"] = self._build_mvp()
            elif step == "create_landing_page":
                results["landing"] = self._create_landing_page()
            elif step == "plan_launch":
                results["launch"] = self._plan_launch()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _discover_opportunities(self) -> dict[str, Any]:
        """Research SaaS opportunities through market gaps."""
        opportunities = self.think_json(
            "Research 5 SaaS product opportunities. For each, analyze:\n"
            "1. What problem exists that current solutions don't solve well?\n"
            "2. Who has this problem? (be specific — job titles, company sizes)\n"
            "3. How do they currently solve it? (workarounds, competitors)\n"
            "4. Why would they pay for a better solution?\n"
            "5. How big is the market?\n\n"
            "Focus on:\n"
            "- B2B niches (higher willingness to pay)\n"
            "- Problems with existing solutions that are too expensive or too complex\n"
            "- Workflows that are still manual/spreadsheet-based\n"
            "- API-first products (easier to build)\n"
            "- Vertical SaaS (industry-specific tools)\n\n"
            "Return: {\"opportunities\": [{\"name\": str, \"problem\": str, "
            "\"target_market\": str, \"current_solutions\": [str], "
            "\"why_switch\": str, \"market_size\": str, "
            "\"revenue_model\": str, \"estimated_mrr_potential\": float, "
            "\"build_complexity\": str, \"moat\": str}]}"
        )

        # Store promising ones
        for opp in opportunities.get("opportunities", []):
            if opp.get("estimated_mrr_potential", 0) >= 500:
                self.db.execute_insert(
                    "INSERT OR IGNORE INTO saas_products "
                    "(name, problem, solution, target_market, market_size_estimate, status) "
                    "VALUES (?, ?, ?, ?, ?, 'researching')",
                    (opp["name"], opp["problem"], opp.get("why_switch", ""),
                     opp["target_market"], opp.get("market_size", "")),
                )

        self.share_knowledge(
            "opportunity", "saas_opportunities",
            json.dumps(opportunities.get("opportunities", []))[:1000],
            tags=["saas", "market_research"],
        )
        return opportunities

    def _validate_idea(self) -> dict[str, Any]:
        """Validate a SaaS idea before building."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status = 'researching' LIMIT 1"
        )
        if not products:
            return {"status": "no_ideas_to_validate"}

        product = dict(products[0])
        validation = self.think_json(
            f"Validate this SaaS idea rigorously:\n"
            f"Name: {product['name']}\n"
            f"Problem: {product['problem']}\n"
            f"Target: {product['target_market']}\n\n"
            "Score each dimension 1-10:\n"
            "1. Problem severity (how painful is it?)\n"
            "2. Market size (enough customers?)\n"
            "3. Willingness to pay (will they pay $X/month?)\n"
            "4. Competition (can we differentiate?)\n"
            "5. Buildability (can an AI agent build this?)\n"
            "6. Distribution (how do we reach customers?)\n"
            "7. Retention (will they keep paying?)\n\n"
            "Be BRUTALLY honest. Most ideas fail. Only recommend building if "
            "the total score is 50+ out of 70.\n\n"
            "Return: {\"scores\": {\"problem_severity\": int, \"market_size\": int, "
            "\"willingness_to_pay\": int, \"competition\": int, \"buildability\": int, "
            "\"distribution\": int, \"retention\": int}, \"total_score\": int, "
            "\"verdict\": \"build\"|\"iterate\"|\"kill\", \"reasoning\": str, "
            "\"risks\": [str], \"suggested_pivot\": str}"
        )

        total = validation.get("total_score", 0)
        confidence = total / 70.0
        verdict = validation.get("verdict", "kill")

        new_status = "validated" if verdict == "build" else "researching"
        self.db.execute(
            "UPDATE saas_products SET validation_score = ?, status = ? WHERE id = ?",
            (confidence, new_status, product["id"]),
        )

        # Store research
        self.db.execute_insert(
            "INSERT INTO market_research (product_id, research_type, findings, confidence) "
            "VALUES (?, 'validation', ?, ?)",
            (product["id"], json.dumps(validation, default=str), confidence),
        )

        self.log_action("idea_validated", product["name"],
                        f"Score: {total}/70, Verdict: {verdict}")
        return validation

    def _competitor_analysis(self) -> dict[str, Any]:
        """Deep-dive competitor analysis for a product."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status IN ('researching', 'validated') LIMIT 1"
        )
        if not products:
            return {"status": "no_products_to_analyze"}

        product = dict(products[0])
        analysis = self.think_json(
            f"Deep competitor analysis for: {product['name']}\n"
            f"Problem: {product['problem']}\n"
            f"Market: {product['target_market']}\n\n"
            "For each competitor:\n"
            "1. What they do well\n"
            "2. What they do poorly (1-star reviews, complaints)\n"
            "3. Their pricing\n"
            "4. Their weaknesses we can exploit\n"
            "5. Their distribution channels\n\n"
            "Return: {\"competitors\": [{\"name\": str, \"url\": str, "
            "\"pricing\": str, \"strengths\": [str], \"weaknesses\": [str], "
            "\"market_position\": str, \"estimated_revenue\": str}], "
            "\"gap_analysis\": str, \"our_positioning\": str, "
            "\"pricing_recommendation\": str}"
        )

        self.db.execute(
            "UPDATE saas_products SET competitors = ? WHERE id = ?",
            (json.dumps([c["name"] for c in analysis.get("competitors", [])]),
             product["id"]),
        )
        self.db.execute_insert(
            "INSERT INTO market_research (product_id, research_type, findings, confidence) "
            "VALUES (?, 'competitor_analysis', ?, 0.7)",
            (product["id"], json.dumps(analysis, default=str)),
        )

        self.log_action("competitor_analysis", product["name"],
                        f"{len(analysis.get('competitors', []))} competitors analyzed")
        return analysis

    def _design_architecture(self) -> dict[str, Any]:
        """Design the technical architecture for a validated product."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status = 'validated' LIMIT 1"
        )
        if not products:
            return {"status": "no_validated_products"}

        product = dict(products[0])
        architecture = self.think_json(
            f"Design technical architecture for: {product['name']}\n"
            f"Problem: {product['problem']}\n"
            f"Solution: {product['solution']}\n\n"
            "Requirements:\n"
            "- Must be buildable by an AI coding agent\n"
            "- Deploy on free/cheap tiers (Vercel, Railway, Supabase, Neon)\n"
            "- Start simple, design for extensibility\n"
            "- API-first architecture\n\n"
            "Return: {\"tech_stack\": {\"backend\": str, \"frontend\": str, "
            "\"database\": str, \"hosting\": str, \"auth\": str, \"payments\": str}, "
            "\"core_features\": [{\"name\": str, \"description\": str, "
            "\"priority\": str, \"complexity\": str}], "
            "\"api_endpoints\": [{\"method\": str, \"path\": str, \"description\": str}], "
            "\"data_model\": [{\"table\": str, \"fields\": [str]}], "
            "\"mvp_scope\": str, \"estimated_build_hours\": int}"
        )

        # Store features
        for feature in architecture.get("core_features", []):
            self.db.execute_insert(
                "INSERT INTO saas_features (product_id, feature_name, description, "
                "priority, complexity) VALUES (?, ?, ?, ?, ?)",
                (product["id"], feature["name"], feature.get("description", ""),
                 feature.get("priority", "medium"), feature.get("complexity", "medium")),
            )

        self.db.execute(
            "UPDATE saas_products SET tech_stack = ?, status = 'building' WHERE id = ?",
            (json.dumps(architecture.get("tech_stack", {})), product["id"]),
        )

        self.log_action("architecture_designed", product["name"])
        return architecture

    def _build_mvp(self) -> dict[str, Any]:
        """Build the MVP using the Coder agent."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status = 'building' LIMIT 1"
        )
        if not products:
            return {"status": "no_products_to_build"}

        product = dict(products[0])
        features = self.db.execute(
            "SELECT * FROM saas_features WHERE product_id = ? AND priority IN ('critical', 'high') "
            "AND status = 'planned' ORDER BY CASE priority WHEN 'critical' THEN 1 ELSE 2 END",
            (product["id"],),
        )

        if not features:
            return {"status": "no_features_to_build"}

        feature = dict(features[0])
        tech_stack = json.loads(product.get("tech_stack", "{}"))

        spec = (
            f"Build feature for SaaS product: {product['name']}\n"
            f"Feature: {feature['feature_name']}\n"
            f"Description: {feature.get('description', '')}\n"
            f"Tech stack: {json.dumps(tech_stack)}\n\n"
            f"Build a production-quality implementation with:\n"
            f"- Clean API endpoints\n"
            f"- Input validation\n"
            f"- Error handling\n"
            f"- Unit tests\n"
        )

        build_result = self.coder.generate_module(spec)

        status = "shipped" if build_result.get("status") == "success" else "planned"
        self.db.execute(
            "UPDATE saas_features SET status = ? WHERE id = ?",
            (status, feature["id"]),
        )

        self.log_action("feature_built", feature["feature_name"],
                        build_result.get("status", "unknown"))
        return {"feature": feature["feature_name"], "build_status": build_result.get("status")}

    def _create_landing_page(self) -> dict[str, Any]:
        """Create a landing page for a SaaS product."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status IN ('building', 'beta') LIMIT 1"
        )
        if not products:
            return {"status": "no_products_need_landing"}

        product = dict(products[0])
        landing = self.think_json(
            f"Create a landing page for: {product['name']}\n"
            f"Tagline: {product.get('tagline', '')}\n"
            f"Problem: {product['problem']}\n"
            f"Solution: {product['solution']}\n"
            f"Target: {product['target_market']}\n\n"
            "Return: {\"headline\": str, \"subheadline\": str, "
            "\"hero_cta\": str, \"pain_points\": [str], "
            "\"features\": [{\"title\": str, \"description\": str}], "
            "\"social_proof\": str, \"pricing_section\": str, "
            "\"faq\": [{\"q\": str, \"a\": str}]}"
        )

        self.log_action("landing_page_created", product["name"])
        return landing

    def _plan_launch(self) -> dict[str, Any]:
        """Plan the launch strategy for a SaaS product."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status IN ('building', 'beta') LIMIT 1"
        )
        if not products:
            return {"status": "no_products_to_launch"}

        product = dict(products[0])
        return self.think_json(
            f"Plan launch strategy for: {product['name']}\n"
            f"Target: {product['target_market']}\n\n"
            "Include:\n"
            "1. Pre-launch (waitlist, beta users)\n"
            "2. Launch channels (Product Hunt, HN, Reddit, Twitter)\n"
            "3. Content marketing plan\n"
            "4. Pricing and discount strategy\n"
            "5. First 30 days growth targets\n\n"
            "Return: {\"pre_launch\": [str], \"launch_channels\": [{\"channel\": str, "
            "\"strategy\": str, \"expected_signups\": int}], "
            "\"content_plan\": [str], \"pricing\": {\"free_tier\": str, "
            "\"pro_tier\": str, \"pro_price\": float}, "
            "\"day_30_target\": {\"users\": int, \"paying\": int, \"mrr\": float}}"
        )
