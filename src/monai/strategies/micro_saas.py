"""Micro-SaaS strategy agent — builds small tools and deploys them for real.

Identifies opportunities for small, focused software tools that solve
specific problems. Uses the Coder agent to build them. Deploys on
free tiers (Vercel, Railway, Render, etc.) via executor.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

# Real deployment targets with free tiers
DEPLOY_PLATFORMS = {
    "railway": {
        "url": "https://railway.app",
        "signup_url": "https://railway.app/login",
        "deploy_url": "https://railway.app/new",
        "free_tier": "$5/month credit",
    },
    "render": {
        "url": "https://render.com",
        "signup_url": "https://dashboard.render.com/register",
        "deploy_url": "https://dashboard.render.com/create",
        "free_tier": "750 hours/month free",
    },
    "vercel": {
        "url": "https://vercel.com",
        "signup_url": "https://vercel.com/signup",
        "deploy_url": "https://vercel.com/new",
        "free_tier": "Hobby plan free",
    },
}


class MicroSaaSAgent(BaseAgent):
    name = "micro_saas"
    description = (
        "Identifies and builds micro-SaaS tools — small, focused software "
        "that solves specific problems. Deploys them for real on free hosting tiers."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.products_dir = config.data_dir / "micro_saas"
        self.products_dir.mkdir(parents=True, exist_ok=True)

    def _get_product_statuses(self) -> dict[str, int]:
        """Count products by status from JSON files."""
        statuses: dict[str, int] = {}
        for path in self.products_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                s = data.get("status", "unknown")
                statuses[s] = statuses.get(s, 0) + 1
            except (json.JSONDecodeError, OSError):
                continue
        return statuses

    def plan(self) -> list[str]:
        statuses = self._get_product_statuses()

        # Deterministic progression — always advance the pipeline
        if not statuses:
            return ["research_opportunities"]
        if statuses.get("researched", 0) > 0:
            return ["design_product"]
        if statuses.get("designed", 0) > 0:
            return ["build_mvp"]
        if statuses.get("built", 0) > 0:
            return ["review_product"]
        if statuses.get("reviewed", 0) > 0:
            return ["deploy"]
        if statuses.get("deployed", 0) > 0:
            return ["create_landing_page"]
        if statuses.get("build_failed", 0) > 0:
            return ["design_product"]  # Redesign failed builds

        # All products at final stage — start new cycle
        return ["research_opportunities"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting micro-SaaS cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_opportunities":
                results["research"] = self._research_opportunities()
            elif step == "design_product":
                results["design"] = self._design_product()
            elif step == "build_mvp":
                results["build"] = self._build_mvp()
            elif step == "review_product":
                results["review"] = self._review_product()
            elif step == "deploy":
                results["deploy"] = self._deploy()
            elif step == "create_landing_page":
                results["landing"] = self._create_landing_page()
            elif step == "check_usage":
                results["usage"] = self._check_usage()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_opportunities(self) -> dict[str, Any]:
        """Find micro-SaaS opportunities by analyzing REAL market data."""
        # Browse real sources for micro-SaaS ideas
        ph_result = self.browse_and_extract(
            "https://www.producthunt.com/topics/developer-tools",
            "Extract trending developer tools and micro-SaaS products.\n"
            "For each: name, tagline, category, upvotes, pricing model.\n"
            "Return JSON: {\"products\": [{\"name\": str, \"tagline\": str, "
            "\"category\": str, \"upvotes\": int, \"pricing\": str}]}\n"
            "Only include REAL products visible on the page.",
        )

        # Check IndieHackers for real revenue data
        ih_result = self.browse_and_extract(
            "https://www.indiehackers.com/products?revenueVerification=stripe&sorting=highest-revenue",
            "Extract micro-SaaS products with verified revenue.\n"
            "For each: name, description, monthly revenue, founder.\n"
            "Return JSON: {\"products\": [{\"name\": str, \"description\": str, "
            "\"revenue\": str, \"founder\": str}]}\n"
            "Only include REAL products visible on the page.",
        )

        real_data = {
            "product_hunt": ph_result.get("result", {}),
            "indie_hackers": ih_result.get("result", {}),
        }

        # Use real data to identify gaps
        opportunities = self.think_json(
            "Based on this REAL market data, identify 5 micro-SaaS opportunities.\n"
            "Requirements:\n"
            "- Can be built in a day by an AI agent\n"
            "- Solves a specific, real problem\n"
            "- Can be deployed free (Vercel, Render, Railway)\n"
            "- Has a clear monetization path\n\n"
            f"Real market data:\n{json.dumps(real_data, default=str)[:3000]}\n\n"
            "Return: {\"ideas\": [{\"name\": str, \"problem\": str, "
            "\"solution\": str, \"tech_stack\": str, \"pricing_model\": str, "
            "\"deploy_platform\": str}]}",
        )
        # Persist top idea as actionable product file for pipeline progression
        ideas = opportunities.get("ideas", [])
        saved = 0
        for idea in ideas[:2]:
            name = idea.get("name", "untitled")
            safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip()
            if not safe_name:
                continue
            path = self.products_dir / f"{safe_name}.json"
            if not path.exists():
                path.write_text(json.dumps({
                    "research": idea,
                    "status": "researched",
                }, indent=2))
                saved += 1

        self.share_knowledge(
            "opportunity", "micro_saas_ideas",
            json.dumps(ideas)[:1000],
            tags=["micro_saas", "product"],
        )
        self.log_action("research_complete", f"Found {len(ideas)} ideas, saved {saved} for design")
        return opportunities

    def _design_product(self) -> dict[str, Any]:
        """Design a product from a researched opportunity."""
        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "researched":
                continue

            research = data.get("research", {})
            spec = self.think_json(
                f"Design a micro-SaaS product based on this research:\n"
                f"Name: {research.get('name', 'unknown')}\n"
                f"Problem: {research.get('problem', 'unknown')}\n"
                f"Solution: {research.get('solution', 'unknown')}\n"
                f"Tech stack: {research.get('tech_stack', 'Python')}\n"
                f"Pricing model: {research.get('pricing_model', 'freemium')}\n\n"
                "Create a detailed specification.\n\n"
                "Return: {\"name\": str, \"tagline\": str, \"problem\": str, "
                "\"features\": [{\"name\": str, \"description\": str}], "
                "\"tech_stack\": {\"backend\": str, \"frontend\": str, \"database\": str}, "
                "\"api_endpoints\": [{\"method\": str, \"path\": str, \"description\": str}], "
                "\"pricing\": {\"free_tier\": str, \"paid_tier\": str, \"price\": float}, "
                "\"deploy_target\": str}"
            )

            data["design"] = spec
            data["status"] = "designed"
            path.write_text(json.dumps(data, indent=2))

            name = spec.get("name", path.stem)
            self.log_action("product_designed", name)
            return spec

        return {"status": "no_products_to_design"}

    def _build_mvp(self) -> dict[str, Any]:
        """Build an MVP using the Coder agent."""
        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") == "designed":
                design = data["design"]
                name = design.get("name", "untitled")

                spec = (
                    f"Build a micro-SaaS MVP: {name}\n"
                    f"Tagline: {design.get('tagline', '')}\n"
                    f"Features: {json.dumps(design.get('features', []))}\n"
                    f"Tech stack: {json.dumps(design.get('tech_stack', {}))}\n"
                    f"API endpoints: {json.dumps(design.get('api_endpoints', []))}\n"
                    f"Build a working Python backend with all endpoints. "
                    f"Include proper error handling and input validation. "
                    f"Include a Dockerfile and requirements.txt for deployment. "
                    f"Include a simple landing page (index.html) with the product description."
                )

                build_result = self.coder.generate_module(spec)
                data["build"] = build_result
                data["status"] = "built" if build_result.get("status") == "success" else "build_failed"
                path.write_text(json.dumps(data, indent=2))

                self.log_action("mvp_built", name, build_result.get("status", "unknown"))
                return {"product": name, "build_status": build_result.get("status")}

        return {"status": "no_products_to_build"}

    def _review_product(self) -> dict[str, Any]:
        """Quality gate: review built product before deployment."""
        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "built":
                continue

            design = data.get("design", {})
            name = design.get("name", path.stem)

            result = self.reviewer.review_product(
                strategy=self.name,
                product_name=name,
                product_data=data,
                product_type="saas",
            )

            if result.verdict == "approved":
                data["status"] = "reviewed"
                data["review"] = result.to_dict()
                if result.improved_content:
                    data["improved_content"] = result.improved_content
                path.write_text(json.dumps(data, indent=2))
                self.log_action("product_reviewed", f"{name}: APPROVED (score={result.quality_score:.2f})")
            elif result.verdict == "rejected":
                data["status"] = "designed"  # Send back to redesign
                data["review"] = result.to_dict()
                path.write_text(json.dumps(data, indent=2))
                self.log_action("product_review_rejected", f"{name}: REJECTED — {'; '.join(result.issues[:3])}")
            else:
                data["status"] = "reviewed"  # Proceed with notes
                data["review"] = result.to_dict()
                path.write_text(json.dumps(data, indent=2))
                self.log_action("product_reviewed", f"{name}: PASSED WITH NOTES (score={result.quality_score:.2f})")

            return result.to_dict()

        return {"status": "no_products_to_review"}

    def _deploy(self) -> dict[str, Any]:
        """Deploy reviewed products to REAL hosting platforms."""
        deployed = 0

        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") not in ("built", "reviewed"):
                continue

            design = data.get("design", {})
            name = design.get("name", "untitled")
            target = design.get("deploy_target", "railway")

            if target not in DEPLOY_PLATFORMS:
                target = "railway"  # Default

            platform_info = DEPLOY_PLATFORMS[target]

            # Ensure account on the deployment platform
            self.ensure_platform_account(target)

            # Deploy via executor
            build_info = data.get("build", {})
            project_dir = build_info.get("project_dir", "")

            result = self.execute_task(
                f"Deploy this micro-SaaS product to {target}.\n\n"
                f"Product: {name}\n"
                f"Deploy URL: {platform_info['deploy_url']}\n"
                f"Project directory: {project_dir}\n\n"
                f"Steps:\n"
                f"1. Navigate to {platform_info['deploy_url']}\n"
                f"2. Create a new project/service\n"
                f"3. Connect it to the project code (upload or git deploy)\n"
                f"4. Configure environment variables if needed\n"
                f"5. Deploy and wait for the build to complete\n"
                f"6. Return the deployed URL\n\n"
                f"If there's a GitHub deploy option, create a repo first and push the code.",
            )

            if result.get("status") == "completed":
                data["status"] = "deployed"
                data["deploy_result"] = result.get("result", "")
                data["deploy_platform"] = target
                path.write_text(json.dumps(data, indent=2))
                deployed += 1
                self.log_action("deployed", f"Deployed {name} to {target}")
            else:
                self.log_action("deploy_failed",
                                f"Failed to deploy {name}: {result.get('reason', '')}")

        return {"deployed": deployed}

    def _create_landing_page(self) -> dict[str, Any]:
        """Create a real landing page for deployed products."""
        created = 0

        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "deployed":
                continue
            if data.get("landing_page_created"):
                continue

            design = data.get("design", {})
            name = design.get("name", "untitled")
            deploy_url = data.get("deploy_result", "")

            # Build a landing page with the coder
            landing_spec = (
                f"Create a professional, conversion-optimized landing page for:\n"
                f"Product: {name}\n"
                f"Tagline: {design.get('tagline', '')}\n"
                f"Problem it solves: {design.get('problem', '')}\n"
                f"Features: {json.dumps(design.get('features', []))}\n"
                f"Pricing: {json.dumps(design.get('pricing', {}))}\n"
                f"App URL: {deploy_url}\n\n"
                f"Build a single-page HTML/CSS/JS landing page with:\n"
                f"- Hero section with tagline and CTA\n"
                f"- Features section\n"
                f"- Pricing section\n"
                f"- Sign up / Try free CTA buttons linking to {deploy_url}\n"
                f"Make it professional and mobile-responsive."
            )

            build_result = self.coder.generate_module(landing_spec)
            if build_result.get("status") == "success":
                data["landing_page_created"] = True
                path.write_text(json.dumps(data, indent=2))
                created += 1
                self.log_action("landing_page", f"Created landing page for {name}")

        return {"landing_pages_created": created}

    def _check_usage(self) -> dict[str, Any]:
        """Check real usage metrics for deployed products."""
        metrics = {}

        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "deployed":
                continue

            design = data.get("design", {})
            name = design.get("name", "untitled")
            deploy_url = data.get("deploy_result", "")
            platform = data.get("deploy_platform", "")

            if not deploy_url:
                continue

            # Check the deployment platform dashboard for real metrics
            result = self.browse_and_extract(
                deploy_url,
                f"Check if this deployed app at {deploy_url} is running. "
                "Return: {\"status\": \"up\" or \"down\", \"response_time\": str}",
            )
            metrics[name] = {
                "deploy_url": deploy_url,
                "platform": platform,
                "health": result.get("result", {}),
            }

        return {"products_checked": len(metrics), "metrics": metrics}
