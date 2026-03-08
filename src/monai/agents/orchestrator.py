"""Orchestrator agent — the brain of monAI.

Decides which strategies to run, allocates resources, monitors performance,
discovers new opportunities, and ensures diversification.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.business.crm import CRM
from monai.business.finance import Finance
from monai.business.risk import RiskManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)


class Orchestrator(BaseAgent):
    name = "orchestrator"
    description = (
        "Master agent that discovers opportunities, selects strategies, "
        "allocates resources, and monitors performance across all money-making activities."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.crm = CRM(db)
        self.finance = Finance(db)
        self.risk = RiskManager(config, db)
        self._strategy_agents: dict[str, BaseAgent] = {}

    def register_strategy(self, agent: BaseAgent):
        self._strategy_agents[agent.name] = agent
        self.log_action("register_strategy", f"Registered: {agent.name}")

    def plan(self) -> list[str]:
        """Generate the orchestrator's action plan for this cycle."""
        health = self.risk.get_portfolio_health()
        context = json.dumps(health, indent=2, default=str)

        plan_response = self.think_json(
            "Based on the current portfolio health, generate an action plan. "
            "Return JSON with: {\"actions\": [{\"action\": str, \"priority\": int, \"reason\": str}]}. "
            "Actions can include: review_strategies, discover_opportunities, "
            "rebalance, pause_underperformer, scale_winner, start_new_strategy, "
            "follow_up_clients, check_deliverables.",
            context=context,
        )
        actions = plan_response.get("actions", [])
        self.log_action("plan", f"Generated {len(actions)} actions", json.dumps(actions))
        return [a["action"] for a in sorted(actions, key=lambda x: x.get("priority", 99))]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Execute one orchestration cycle."""
        self.log_action("cycle_start", f"Starting orchestration cycle at {datetime.now()}")

        # 1. Check portfolio health
        health = self.risk.get_portfolio_health()
        self.log_action("health_check", json.dumps(health, default=str))

        # 2. Review each active strategy
        reviews = self._review_strategies()

        # 3. Generate and execute plan
        plan = self.plan()
        results = []
        for action in plan:
            result = self._execute_action(action)
            results.append({"action": action, "result": result})

        # 4. Run active strategy agents
        strategy_results = self._run_strategies()

        cycle_result = {
            "timestamp": datetime.now().isoformat(),
            "health": health,
            "reviews": reviews,
            "plan_actions": results,
            "strategy_results": strategy_results,
        }

        self.log_action("cycle_complete", json.dumps(cycle_result, default=str)[:1000])
        return cycle_result

    def discover_opportunities(self) -> list[dict[str, Any]]:
        """Use LLM to brainstorm new money-making opportunities."""
        current_strategies = self.db.execute(
            "SELECT name, category, status FROM strategies"
        )
        current = [dict(r) for r in current_strategies]

        response = self.think_json(
            "Brainstorm 5 new money-making opportunities I should explore. "
            "Consider: freelancing, digital products, content, trading, arbitrage, "
            "SaaS, consulting, automation, reselling, affiliate marketing, lead gen, "
            "and ANYTHING else that could make money legally. "
            "For each, return: {\"opportunities\": [{\"name\": str, \"category\": str, "
            "\"description\": str, \"estimated_monthly_revenue\": float, "
            "\"startup_cost\": float, \"risk_level\": str, \"time_to_first_revenue_days\": int}]}",
            context=f"Current strategies: {json.dumps(current)}",
        )
        opportunities = response.get("opportunities", [])
        self.log_action("discover", f"Found {len(opportunities)} opportunities",
                        json.dumps(opportunities))
        return opportunities

    def _review_strategies(self) -> list[dict[str, Any]]:
        reviews = []
        strategies = self.db.execute("SELECT * FROM strategies WHERE status = 'active'")
        for strategy in strategies:
            s = dict(strategy)
            pause_check = self.risk.should_pause_strategy(s["id"])
            roi = self.finance.get_roi(s["id"])
            review = {
                "strategy": s["name"],
                "roi": roi,
                "should_pause": pause_check["should_pause"],
                "reasons": pause_check["reasons"],
            }
            if pause_check["should_pause"]:
                self.log_action("pause_strategy", s["name"],
                                json.dumps(pause_check["reasons"]))
                self.db.execute(
                    "UPDATE strategies SET status = 'paused', updated_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), s["id"]),
                )
            reviews.append(review)
        return reviews

    def _execute_action(self, action: str) -> str:
        """Execute a planned action."""
        if action == "discover_opportunities":
            opportunities = self.discover_opportunities()
            return f"Discovered {len(opportunities)} opportunities"
        elif action == "rebalance":
            return self._rebalance()
        elif action == "follow_up_clients":
            return self._follow_up_clients()
        else:
            return f"Action '{action}' noted for next cycle"

    def _rebalance(self) -> str:
        """Rebalance allocation across strategies based on performance."""
        pnl = self.finance.get_strategy_pnl()
        winners = [s for s in pnl if s["net"] > 0]
        if winners:
            self.log_action("rebalance", f"{len(winners)} profitable strategies identified")
            return f"Rebalanced: {len(winners)} winners identified for scale-up"
        return "No rebalancing needed"

    def _follow_up_clients(self) -> str:
        """Check for clients needing follow-up."""
        pipeline = self.crm.get_pipeline_summary()
        contacted = pipeline.get("contacted", 0)
        negotiating = pipeline.get("negotiating", 0)
        return f"Pipeline: {contacted} contacted, {negotiating} negotiating"

    def _run_strategies(self) -> dict[str, Any]:
        """Run all registered and active strategy agents."""
        results = {}
        active = self.db.execute("SELECT name FROM strategies WHERE status = 'active'")
        active_names = {r["name"] for r in active}

        for name, agent in self._strategy_agents.items():
            if name in active_names:
                try:
                    result = agent.run()
                    results[name] = {"status": "ok", "result": result}
                except Exception as e:
                    logger.error(f"Strategy {name} failed: {e}")
                    results[name] = {"status": "error", "error": str(e)}
        return results
