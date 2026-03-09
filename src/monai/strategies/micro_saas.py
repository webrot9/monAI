"""Micro-SaaS strategy agent — builds small tools and API wrappers.

Identifies opportunities for small, focused software tools that solve
specific problems. Uses the Coder agent to build them. Deploys on
free tiers (Vercel, Railway, Render, etc.).
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class MicroSaaSAgent(BaseAgent):
    name = "micro_saas"
    description = (
        "Identifies and builds micro-SaaS tools — small, focused software "
        "that solves specific problems. API wrappers, converters, calculators, "
        "generators. Deploys on free tiers for zero-cost hosting."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.products_dir = config.data_dir / "micro_saas"
        self.products_dir.mkdir(parents=True, exist_ok=True)

    def plan(self) -> list[str]:
        existing = list(self.products_dir.glob("*.json"))
        plan = self.think_json(
            f"I have {len(existing)} micro-SaaS products. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_opportunities, design_product, build_mvp, "
            "deploy, create_landing_page, analyze_usage.",
        )
        return plan.get("steps", ["research_opportunities"])

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

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_opportunities(self) -> dict[str, Any]:
        """Find micro-SaaS opportunities — small tools people would pay for."""
        opportunities = self.think_json(
            "Brainstorm 5 micro-SaaS product ideas. Requirements:\n"
            "- Can be built in a day by an AI agent\n"
            "- Solves a specific, real problem\n"
            "- Can be deployed free (Vercel, Render, Railway)\n"
            "- Has a clear monetization path (freemium, one-time, API pricing)\n"
            "- Low competition in the exact niche\n\n"
            "Think about: API wrappers, format converters, calculators, "
            "generators, validators, scrapers-as-a-service, data enrichment tools.\n\n"
            "Return: {\"ideas\": [{\"name\": str, \"problem\": str, "
            "\"solution\": str, \"tech_stack\": str, \"pricing_model\": str, "
            "\"estimated_monthly_revenue\": float, \"build_time_hours\": int, "
            "\"deploy_platform\": str}]}"
        )
        self.share_knowledge(
            "opportunity", "micro_saas_ideas",
            json.dumps(opportunities.get("ideas", []))[:1000],
            tags=["micro_saas", "product"],
        )
        return opportunities

    def _design_product(self) -> dict[str, Any]:
        """Design a specific micro-SaaS product."""
        spec = self.think_json(
            "Design a micro-SaaS product to build right now. "
            "Create a detailed specification.\n\n"
            "Return: {\"name\": str, \"tagline\": str, \"problem\": str, "
            "\"features\": [{\"name\": str, \"description\": str}], "
            "\"tech_stack\": {\"backend\": str, \"frontend\": str, \"database\": str}, "
            "\"api_endpoints\": [{\"method\": str, \"path\": str, \"description\": str}], "
            "\"pricing\": {\"free_tier\": str, \"paid_tier\": str, \"price\": float}, "
            "\"deploy_target\": str}"
        )

        # Save design
        name = spec.get("name", "untitled")
        safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip()
        design_path = self.products_dir / f"{safe_name}.json"
        design_path.write_text(json.dumps({"design": spec, "status": "designed"}, indent=2))

        self.log_action("product_designed", name)
        return spec

    def _build_mvp(self) -> dict[str, Any]:
        """Build an MVP using the Coder agent."""
        # Find a designed but not built product
        for path in self.products_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") == "designed":
                design = data["design"]
                name = design.get("name", "untitled")

                # Use Coder to build it
                spec = (
                    f"Build a micro-SaaS MVP: {name}\n"
                    f"Tagline: {design.get('tagline', '')}\n"
                    f"Features: {json.dumps(design.get('features', []))}\n"
                    f"Tech stack: {json.dumps(design.get('tech_stack', {}))}\n"
                    f"API endpoints: {json.dumps(design.get('api_endpoints', []))}\n"
                    f"Build a working Python backend with all endpoints. "
                    f"Include proper error handling and input validation."
                )

                build_result = self.coder.generate_module(spec)
                data["build"] = build_result
                data["status"] = "built" if build_result.get("status") == "success" else "build_failed"
                path.write_text(json.dumps(data, indent=2))

                self.log_action("mvp_built", name, build_result.get("status", "unknown"))
                return {"product": name, "build_status": build_result.get("status")}

        return {"status": "no_products_to_build"}
