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

        # Deterministic progression — always advance the pipeline
        if not stats:
            return ["discover_opportunities"]
        if stats.get("researching", 0) > 0:
            return ["validate_idea"]
        if stats.get("validated", 0) > 0:
            return ["design_architecture"]
        if stats.get("building", 0) > 0:
            return ["build_mvp"]
        if stats.get("beta", 0) > 0:
            return ["review_product"]
        if stats.get("reviewed", 0) > 0:
            return ["create_landing_page"]
        if stats.get("launched", 0) > 0:
            return ["plan_growth"]

        # All products at final stage — discover new ones
        return ["discover_opportunities"]

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
            elif step == "review_product":
                results["review"] = self._review_product()
            elif step == "create_landing_page":
                results["landing"] = self._create_landing_page()
            elif step == "plan_launch":
                results["launch"] = self._plan_launch()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _discover_opportunities(self) -> dict[str, Any]:
        """Research REAL SaaS opportunities from Product Hunt, G2, and Capterra."""
        # Browse real sources for trending/new SaaS products and gaps
        ph_data = self.browse_and_extract(
            "https://www.producthunt.com/topics/saas",
            "Extract the latest trending SaaS products listed on this page. "
            "For each product, extract: name, tagline, category, upvote count, "
            "and any listed pricing. Only include REAL data visible on the page. "
            "Do NOT make up any information. "
            "Return as JSON: {\"products\": [{\"name\": str, \"tagline\": str, "
            "\"category\": str, \"upvotes\": int, \"pricing\": str}]}"
        )

        g2_data = self.browse_and_extract(
            "https://www.g2.com/categories",
            "Extract the top software categories and any trending/emerging categories "
            "visible on this page. For each category, note the name and number of "
            "products listed. Only include REAL data visible on the page. "
            "Do NOT make up any information. "
            "Return as JSON: {\"categories\": [{\"name\": str, \"product_count\": int}]}"
        )

        capterra_data = self.browse_and_extract(
            "https://www.capterra.com/browse/",
            "Extract software categories and any 'emerging' or 'trending' labels. "
            "Note which categories have fewer solutions (market gaps). "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"categories\": [{\"name\": str, \"subcategories\": [str], "
            "\"appears_underserved\": bool}]}"
        )

        # Use LLM to synthesize the REAL data into opportunity analysis
        opportunities = self.think_json(
            f"Based on REAL market research data, identify SaaS opportunities.\n\n"
            f"Product Hunt trending products:\n{json.dumps(ph_data, default=str)}\n\n"
            f"G2 categories:\n{json.dumps(g2_data, default=str)}\n\n"
            f"Capterra categories:\n{json.dumps(capterra_data, default=str)}\n\n"
            "Using ONLY the real data above, identify 5 SaaS opportunities where:\n"
            "- There are market gaps (underserved categories)\n"
            "- Existing solutions are expensive or complex\n"
            "- A focused tool could compete\n\n"
            "For each opportunity, explain which real data points support it.\n\n"
            "Return: {\"opportunities\": [{\"name\": str, \"problem\": str, "
            "\"target_market\": str, \"current_solutions\": [str], "
            "\"why_switch\": str, \"market_size\": str, "
            "\"revenue_model\": str, \"estimated_mrr_potential\": float, "
            "\"build_complexity\": str, \"moat\": str, "
            "\"supporting_data\": str}]}"
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
        """Validate a SaaS idea using REAL competitor pricing, reviews, and market data."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status = 'researching' LIMIT 1"
        )
        if not products:
            return {"status": "no_ideas_to_validate"}

        product = dict(products[0])

        # Search for real competitor pricing and reviews
        competitor_data = self.search_web(
            f"{product['name']} alternatives competitors pricing",
            "Extract real competitor products for this market. For each competitor, "
            "extract: name, URL, pricing tiers (with actual dollar amounts), "
            "and any review scores or user counts mentioned. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"competitors\": [{\"name\": str, \"url\": str, "
            "\"pricing\": str, \"review_score\": float, \"user_count\": str}]}"
        )

        # Search for real market size data
        market_data = self.search_web(
            f"{product['target_market']} market size TAM SAM SOM",
            "Extract any real market size figures, growth rates, or industry "
            "statistics mentioned. Include the source of each figure. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"market_figures\": [{\"metric\": str, \"value\": str, "
            "\"source\": str, \"year\": str}]}"
        )

        # Search for real user complaints about existing solutions
        pain_points = self.search_web(
            f"{product['problem']} software complaints frustrations reddit",
            "Extract real user complaints and pain points about existing solutions "
            "in this space. Include the platform where the complaint was found. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"complaints\": [{\"complaint\": str, \"platform\": str, "
            "\"about_product\": str}]}"
        )

        # Now use LLM to score based on REAL data
        validation = self.think_json(
            f"Validate this SaaS idea using REAL market data:\n"
            f"Name: {product['name']}\n"
            f"Problem: {product['problem']}\n"
            f"Target: {product['target_market']}\n\n"
            f"REAL competitor data:\n{json.dumps(competitor_data, default=str)}\n\n"
            f"REAL market size data:\n{json.dumps(market_data, default=str)}\n\n"
            f"REAL user pain points:\n{json.dumps(pain_points, default=str)}\n\n"
            "Based ONLY on the real data above, score each dimension 1-10:\n"
            "1. Problem severity (based on real complaints found)\n"
            "2. Market size (based on real market figures)\n"
            "3. Willingness to pay (based on real competitor pricing)\n"
            "4. Competition (based on real competitors found)\n"
            "5. Buildability (can an AI agent build this?)\n"
            "6. Distribution (how do we reach customers?)\n"
            "7. Retention (will they keep paying?)\n\n"
            "Be BRUTALLY honest. Cite which real data points support each score. "
            "Most ideas fail. Only recommend building if the total score is 50+ out of 70.\n\n"
            "Return: {\"scores\": {\"problem_severity\": int, \"market_size\": int, "
            "\"willingness_to_pay\": int, \"competition\": int, \"buildability\": int, "
            "\"distribution\": int, \"retention\": int}, \"total_score\": int, "
            "\"verdict\": \"build\"|\"iterate\"|\"kill\", \"reasoning\": str, "
            "\"risks\": [str], \"suggested_pivot\": str, "
            "\"data_sources_used\": [str]}"
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
        """Deep-dive competitor analysis using REAL competitor websites and reviews."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status IN ('researching', 'validated') LIMIT 1"
        )
        if not products:
            return {"status": "no_products_to_analyze"}

        product = dict(products[0])

        # Search for real competitors
        search_results = self.search_web(
            f"{product['name']} {product['problem']} software tools alternatives",
            "Extract a list of real competitor products with their URLs. "
            "Only include REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"competitors\": [{\"name\": str, \"url\": str}]}"
        )

        competitors_analyzed = []
        for comp in search_results.get("competitors", [])[:5]:
            url = comp.get("url", "")
            if not url:
                continue

            # Browse the actual competitor website for real pricing and features
            comp_details = self.browse_and_extract(
                url,
                f"Extract real information about this SaaS product from its website. "
                f"Include: product name, pricing tiers with actual prices, "
                f"key features listed, any customer counts or social proof shown, "
                f"integrations offered, and target audience. "
                f"Only include REAL data visible on the page. Do NOT make up any information. "
                f"Return as JSON: {{\"name\": str, \"url\": str, \"pricing_tiers\": "
                f"[{{\"name\": str, \"price\": str, \"features\": [str]}}], "
                f"\"key_features\": [str], \"social_proof\": str, "
                f"\"integrations\": [str], \"target_audience\": str}}"
            )

            # Check real reviews on G2
            review_data = self.browse_and_extract(
                f"https://www.g2.com/products/{comp['name'].lower().replace(' ', '-')}/reviews",
                f"Extract real review data for {comp['name']}. Include: "
                f"overall rating, number of reviews, top positive themes, "
                f"top negative themes (what users complain about). "
                f"Only include REAL data visible on the page. Do NOT make up any information. "
                f"Return as JSON: {{\"rating\": float, \"review_count\": int, "
                f"\"positive_themes\": [str], \"negative_themes\": [str]}}"
            )

            comp_details["reviews"] = review_data
            competitors_analyzed.append(comp_details)

        # Use LLM to synthesize the REAL competitive data
        analysis = self.think_json(
            f"Based on REAL competitor research, analyze the competitive landscape for: "
            f"{product['name']}\n"
            f"Problem: {product['problem']}\n"
            f"Market: {product['target_market']}\n\n"
            f"REAL competitor data:\n{json.dumps(competitors_analyzed, default=str)}\n\n"
            "Using ONLY the real data above:\n"
            "1. Identify each competitor's real strengths and weaknesses\n"
            "2. Find gaps in their offerings (based on real user complaints)\n"
            "3. Recommend our positioning based on real pricing data\n"
            "4. Suggest pricing based on real competitor prices\n\n"
            "Return: {\"competitors\": [{\"name\": str, \"url\": str, "
            "\"pricing\": str, \"strengths\": [str], \"weaknesses\": [str], "
            "\"market_position\": str}], "
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

        # Check for review feedback from previous rejection
        review_feedback = ""
        prev_reviews = self.db.execute(
            "SELECT issues, suggestions FROM product_reviews "
            "WHERE strategy = ? AND product_name = ? ORDER BY id DESC LIMIT 1",
            (self.name, product["name"]),
        )
        if prev_reviews:
            from monai.agents.product_reviewer import ProductReviewer
            review_data = {
                "issues": json.loads(prev_reviews[0].get("issues", "[]")),
                "suggestions": json.loads(prev_reviews[0].get("suggestions", "[]")),
                "quality_score": 0,
            }
            review_feedback = ProductReviewer.format_feedback_for_prompt(review_data)

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
            f"{review_feedback}"
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

    def _review_product(self) -> dict[str, Any]:
        """Quality gate: review SaaS product before landing page and launch."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status = 'beta' LIMIT 1"
        )
        if not products:
            return {"status": "no_products_to_review"}

        product = products[0]
        features = self.db.execute(
            "SELECT * FROM saas_features WHERE product_id = ?", (product["id"],)
        )
        product_data = {
            "design": {
                "name": product["name"],
                "tagline": product.get("tagline", ""),
                "problem": product["problem"],
                "solution": product["solution"],
                "target_market": product["target_market"],
                "features": [{"name": f["feature_name"], "description": f.get("description", "")} for f in features],
                "pricing": product.get("pricing_model", ""),
            },
        }

        result = self.reviewer.review_product(
            strategy=self.name,
            product_name=product["name"],
            product_data=product_data,
            product_type="saas",
        )

        if result.verdict == "rejected":
            self.db.execute_insert(
                "UPDATE saas_products SET status = 'building' WHERE id = ?",
                (product["id"],),
            )
            # Feedback is stored in product_reviews table by the reviewer
            self.log_action("product_review_rejected",
                            f"{product['name']}: {'; '.join(result.issues[:3])}")
        elif result.verdict == "needs_revision":
            # Actively revise content before proceeding
            self.reviewer.revise_product(product_data, result, "saas")
            self.db.execute_insert(
                "UPDATE saas_products SET status = 'reviewed' WHERE id = ?",
                (product["id"],),
            )
            self.log_action("product_revised",
                            f"{product['name']}: REVISED and proceeding (score={result.quality_score:.2f})")
        else:
            self.db.execute_insert(
                "UPDATE saas_products SET status = 'reviewed' WHERE id = ?",
                (product["id"],),
            )
            self.log_action("product_reviewed",
                            f"{product['name']}: {result.verdict} (score={result.quality_score:.2f})")

        return result.to_dict()

    def _create_landing_page(self) -> dict[str, Any]:
        """Create and deploy a REAL landing page for a SaaS product."""
        # Ensure Stripe is set up for subscription payments
        self.ensure_platform_account("stripe")

        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status IN ('building', 'beta', 'reviewed') LIMIT 1"
        )
        if not products:
            return {"status": "no_products_need_landing"}

        product = dict(products[0])

        # Use LLM to plan the landing page content (planning is legitimate LLM use)
        landing_content = self.think_json(
            f"Create landing page content for: {product['name']}\n"
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

        # Use Coder to build a real landing page from the content
        landing_spec = (
            f"Build a production-ready landing page for: {product['name']}\n\n"
            f"Content:\n{json.dumps(landing_content, default=str)}\n\n"
            "Requirements:\n"
            "- Single-page responsive HTML/CSS/JS\n"
            "- Modern design with Tailwind CSS (via CDN)\n"
            "- Email capture form (connected to a simple backend endpoint)\n"
            "- SEO meta tags\n"
            "- Fast loading, no unnecessary dependencies\n"
            "- Include all sections: hero, pain points, features, pricing, FAQ, footer\n"
        )
        build_result = self.coder.generate_module(landing_spec)

        # Deploy the landing page
        deploy_result = self.execute_task(
            f"Deploy the landing page for {product['name']} to a hosting platform",
            f"The landing page files were generated by the coder. "
            f"Build result: {json.dumps(build_result, default=str)[:500]}\n"
            f"Deploy to Vercel, Netlify, or GitHub Pages. Return the live URL."
        )

        self.log_action("landing_page_deployed", product["name"],
                        json.dumps(deploy_result, default=str)[:200])
        return {
            "content": landing_content,
            "build": build_result,
            "deployment": deploy_result,
        }

    def _plan_launch(self) -> dict[str, Any]:
        """Execute REAL launch activities using platform actions."""
        products = self.db.execute(
            "SELECT * FROM saas_products WHERE status IN ('building', 'beta') LIMIT 1"
        )
        if not products:
            return {"status": "no_products_to_launch"}

        product = dict(products[0])

        # Use LLM to plan launch content (planning is legitimate LLM use)
        launch_plan = self.think_json(
            f"Create launch copy and strategy for: {product['name']}\n"
            f"Tagline: {product.get('tagline', '')}\n"
            f"Problem: {product['problem']}\n"
            f"Solution: {product['solution']}\n"
            f"Target: {product['target_market']}\n\n"
            "Create specific, ready-to-post content for each channel.\n\n"
            "Return: {\"product_hunt_tagline\": str, "
            "\"product_hunt_description\": str, "
            "\"twitter_launch_thread\": [str], "
            "\"reddit_post_title\": str, \"reddit_post_body\": str, "
            "\"hacker_news_title\": str, "
            "\"directory_description\": str}"
        )

        launch_results = {}

        # Submit to Product Hunt
        self.ensure_platform_account("producthunt")
        ph_result = self.platform_action(
            "producthunt",
            f"Submit a new product to Product Hunt",
            f"Product name: {product['name']}\n"
            f"Tagline: {launch_plan.get('product_hunt_tagline', product.get('tagline', ''))}\n"
            f"Description: {launch_plan.get('product_hunt_description', product['solution'])}\n"
            f"Website URL: (use the deployed landing page URL)\n"
            f"Topics: SaaS, {product['target_market']}"
        )
        launch_results["product_hunt"] = ph_result

        # Post on Twitter/X
        self.ensure_platform_account("twitter")
        twitter_thread = launch_plan.get("twitter_launch_thread", [])
        if twitter_thread:
            tw_result = self.platform_action(
                "twitter",
                "Post a launch thread on Twitter/X",
                f"Thread content (post as a thread, each item is a tweet):\n"
                + "\n---\n".join(twitter_thread)
            )
            launch_results["twitter"] = tw_result

        # Submit to SaaS directories
        directories = [
            "https://www.saashub.com",
            "https://www.betalist.com",
            "https://alternativeto.net",
        ]
        for directory_url in directories:
            dir_result = self.execute_task(
                f"Submit {product['name']} to the SaaS directory at {directory_url}",
                f"Product name: {product['name']}\n"
                f"Description: {launch_plan.get('directory_description', product['solution'])}\n"
                f"Category: SaaS, {product['target_market']}\n"
                f"Register an account if needed, then submit the product listing."
            )
            launch_results[directory_url] = dir_result

        # Update product status
        self.db.execute(
            "UPDATE saas_products SET status = 'launched', launched_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (product["id"],),
        )

        self.log_action("product_launched", product["name"],
                        json.dumps(launch_results, default=str)[:500])
        return {"launch_plan": launch_plan, "launch_results": launch_results}

    def apply_improvements(self) -> dict[str, Any]:
        """Apply pending improvements from ProductIterator to existing SaaS products.

        For SaaS products stored in the database, this rebuilds features via
        the Coder agent based on improvement plans from the ProductIterator.
        """
        pending = self.product_iterator.get_pending_improvements(self.name)
        if not pending:
            return {"status": "no_pending_improvements"}

        applied = 0
        for improvement in pending[:2]:  # Max 2 per cycle
            product_name = improvement["product_name"]
            improvements_json = improvement.get("improvements", "[]")
            try:
                plan = json.loads(improvements_json) if isinstance(improvements_json, str) else improvements_json
            except (json.JSONDecodeError, TypeError):
                plan = {}

            improvement_items = plan.get("improvements", []) if isinstance(plan, dict) else []

            # Find the product in DB
            products = self.db.execute(
                "SELECT * FROM saas_products WHERE name = ? LIMIT 1",
                (product_name,),
            )
            if not products:
                self.product_iterator.mark_applied(improvement["id"])
                continue

            product = dict(products[0])
            features = self.db.execute(
                "SELECT * FROM saas_features WHERE product_id = ?",
                (product["id"],),
            )

            change_descriptions = [
                f"- {item.get('area', 'general')}: {item.get('specific_change', item.get('current_issue', ''))}"
                for item in improvement_items
            ]
            changes_text = "\n".join(change_descriptions) if change_descriptions else "General quality improvements"

            # Rebuild affected features via Coder
            features_rebuilt = 0
            for feature in features:
                feature_dict = dict(feature)
                # Check if any improvement targets this feature area
                relevant = any(
                    feature_dict["feature_name"].lower() in item.get("area", "").lower()
                    or item.get("area", "").lower() in feature_dict["feature_name"].lower()
                    for item in improvement_items
                ) if improvement_items else True  # If no specific areas, improve all

                if not relevant:
                    continue

                rebuild_spec = (
                    f"Improve the '{feature_dict['feature_name']}' feature of SaaS product '{product_name}'.\n"
                    f"Feature description: {feature_dict.get('description', '')}\n"
                    f"Product problem: {product['problem']}\n"
                    f"Product solution: {product['solution']}\n\n"
                    f"REQUIRED IMPROVEMENTS:\n{changes_text}\n\n"
                    f"Rebuild this feature with the improvements applied. "
                    f"Maintain backward compatibility where possible."
                )
                build_result = self.coder.generate_module(rebuild_spec)

                if build_result.get("status") == "success":
                    self.db.execute(
                        "UPDATE saas_features SET status = 'testing' WHERE id = ?",
                        (feature_dict["id"],),
                    )
                    features_rebuilt += 1

            # Send product back through review pipeline
            if features_rebuilt > 0:
                self.db.execute(
                    "UPDATE saas_products SET status = 'beta' WHERE id = ?",
                    (product["id"],),
                )

            self.product_iterator.mark_applied(improvement["id"])
            applied += 1
            self.log_action(
                "product_improved",
                f"{product_name}: {features_rebuilt} features rebuilt with improvements",
            )

        return {"applied": applied, "total_pending": len(pending)}
