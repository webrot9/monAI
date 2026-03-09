"""Finance Expert — strategic financial advisor for the monAI system.

Works alongside the Commercialista (who tracks costs/budget) to provide:
- Investment allocation recommendations across strategies
- ROI analysis and strategy ranking by profit efficiency
- Cash flow forecasting using actual transaction data
- Capital reallocation signals (double down vs. cut losses)
- Revenue opportunity scoring for the orchestrator
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from monai.agents.base import BaseAgent
from monai.business.commercialista import Commercialista
from monai.business.finance import Finance
from monai.business.projections import GrowthProjector
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

FINANCE_EXPERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS investment_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    action TEXT NOT NULL,          -- scale_up, maintain, reduce, pause, kill
    current_allocation REAL,
    recommended_allocation REAL,
    reasoning TEXT,
    confidence REAL DEFAULT 0.5,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cash_flow_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_date TEXT NOT NULL,
    month_offset INTEGER NOT NULL,   -- months from now
    projected_revenue REAL,
    projected_expenses REAL,
    projected_net REAL,
    assumptions TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunity_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity TEXT NOT NULL,
    revenue_potential REAL,
    cost_estimate REAL,
    time_to_revenue_days INTEGER,
    risk_score REAL,                 -- 0-1, higher = riskier
    composite_score REAL,            -- weighted final score
    recommendation TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class FinanceExpert(BaseAgent):
    """Strategic finance advisor — tells the orchestrator where to put money."""

    name = "finance_expert"
    description = (
        "Analyzes financial performance across all strategies, recommends capital allocation, "
        "scores revenue opportunities, and produces cash flow forecasts."
    )

    def __init__(self, config: Config, db: Database, llm: LLM,
                 commercialista: Commercialista | None = None):
        super().__init__(config, db, llm)
        self.finance = Finance(db)
        self.commercialista = commercialista
        self.projector = GrowthProjector(db, initial_capital=config.initial_capital)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(FINANCE_EXPERT_SCHEMA)

    def plan(self) -> list[str]:
        return [
            "Collect current P&L data per strategy",
            "Analyze ROI efficiency and revenue trends",
            "Generate investment reallocation recommendations",
            "Update cash flow forecast",
            "Score any pending opportunities",
        ]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Full finance cycle: analyze → recommend → forecast."""
        pnl_data = self.finance.get_strategy_pnl()
        budget = self.commercialista.get_budget() if self.commercialista else {}

        recommendations = self._generate_recommendations(pnl_data, budget)
        forecast = self._update_forecast()
        portfolio_health = self._assess_portfolio_health(pnl_data, budget)

        self.journal("finance_cycle", "Completed financial analysis", {
            "recommendations": len(recommendations),
            "portfolio_health": portfolio_health["status"],
        })

        return {
            "recommendations": recommendations,
            "forecast_months": len(forecast),
            "portfolio_health": portfolio_health,
        }

    # ── Recommendations ──────────────────────────────────────

    def _generate_recommendations(self, pnl_data: list[dict],
                                  budget: dict) -> list[dict[str, Any]]:
        """Analyze each strategy and recommend action."""
        if not pnl_data:
            return []

        recommendations = []
        total_revenue = sum(s["revenue"] for s in pnl_data)

        for strategy in pnl_data:
            action = self._decide_action(strategy, total_revenue, budget)
            rec = {
                "strategy": strategy["name"],
                "action": action["action"],
                "reasoning": action["reasoning"],
                "confidence": action["confidence"],
            }
            recommendations.append(rec)

            self.db.execute_insert(
                "INSERT INTO investment_recommendations "
                "(strategy_name, action, current_allocation, recommended_allocation, "
                "reasoning, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                (strategy["name"], action["action"],
                 strategy.get("expenses", 0), action.get("recommended_allocation", 0),
                 action["reasoning"], action["confidence"]),
            )

        # Share top recommendations with all agents
        top = [r for r in recommendations if r["action"] in ("scale_up", "pause", "kill")]
        if top:
            summary = "; ".join(f"{r['strategy']}: {r['action']}" for r in top)
            self.share_knowledge("finance", "investment_signals", summary, confidence=0.8)

        return recommendations

    def _decide_action(self, strategy: dict, total_revenue: float,
                       budget: dict) -> dict[str, Any]:
        """Decide what to do with a strategy based on its numbers."""
        revenue = strategy.get("revenue", 0)
        expenses = strategy.get("expenses", 0)
        net = strategy.get("net", 0)

        # Pure math first, LLM only for edge cases
        roi = revenue / expenses if expenses > 0 else 0

        if expenses == 0 and revenue == 0:
            return {
                "action": "maintain",
                "reasoning": "Not yet active — no data to judge. Keep running.",
                "confidence": 0.3,
                "recommended_allocation": budget.get("balance", 0) * 0.02,
            }

        if roi >= 3.0:
            return {
                "action": "scale_up",
                "reasoning": f"ROI {roi:.1f}x — high performer, increase allocation.",
                "confidence": 0.9,
                "recommended_allocation": expenses * 1.5,
            }

        if roi >= 1.5:
            return {
                "action": "maintain",
                "reasoning": f"ROI {roi:.1f}x — profitable, maintain current allocation.",
                "confidence": 0.8,
                "recommended_allocation": expenses,
            }

        if roi >= 1.0:
            return {
                "action": "maintain",
                "reasoning": f"ROI {roi:.1f}x — breaking even. Monitor closely.",
                "confidence": 0.6,
                "recommended_allocation": expenses,
            }

        if expenses > 0 and revenue > 0 and roi >= 0.5:
            return {
                "action": "reduce",
                "reasoning": f"ROI {roi:.1f}x — underperforming. Reduce spend, optimize.",
                "confidence": 0.7,
                "recommended_allocation": expenses * 0.5,
            }

        if expenses > 20 and revenue == 0:
            return {
                "action": "pause",
                "reasoning": f"€{expenses:.2f} spent with zero revenue. Pause and reassess.",
                "confidence": 0.85,
                "recommended_allocation": 0,
            }

        return {
            "action": "reduce",
            "reasoning": f"ROI {roi:.1f}x — poor return. Cut allocation.",
            "confidence": 0.7,
            "recommended_allocation": max(expenses * 0.3, 0),
        }

    # ── Cash Flow Forecast ───────────────────────────────────

    def _update_forecast(self, months: int = 6) -> list[dict[str, Any]]:
        """Generate cash flow forecast using projection models + actual data."""
        projections = self.projector.project(months)
        forecasts = []

        for p in projections:
            forecast = {
                "month_offset": p.month,
                "projected_revenue": p.revenue,
                "projected_expenses": p.expenses,
                "projected_net": p.net,
            }
            forecasts.append(forecast)

            self.db.execute_insert(
                "INSERT INTO cash_flow_forecasts "
                "(forecast_date, month_offset, projected_revenue, projected_expenses, "
                "projected_net, assumptions) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now().strftime("%Y-%m-%d"), p.month,
                 p.revenue, p.expenses, p.net,
                 "Based on channel growth models"),
            )

        return forecasts

    # ── Portfolio Health ─────────────────────────────────────

    def _assess_portfolio_health(self, pnl_data: list[dict],
                                 budget: dict) -> dict[str, Any]:
        """Overall portfolio health assessment."""
        active = [s for s in pnl_data if s["revenue"] > 0 or s["expenses"] > 0]
        profitable = [s for s in active if s["net"] > 0]
        total_revenue = sum(s["revenue"] for s in pnl_data)
        total_expenses = sum(s["expenses"] for s in pnl_data)

        # Concentration risk: any single strategy > 50% of revenue
        concentration_risk = False
        if total_revenue > 0:
            for s in pnl_data:
                if s["revenue"] / total_revenue > 0.5:
                    concentration_risk = True
                    break

        balance = budget.get("balance", 0)
        burn_rate = budget.get("burn_rate_daily", 0)

        if balance <= 0:
            status = "critical"
        elif len(profitable) == 0 and len(active) > 0:
            status = "warning"
        elif concentration_risk:
            status = "caution"
        elif len(profitable) >= 3 and total_revenue > total_expenses:
            status = "healthy"
        else:
            status = "growing"

        return {
            "status": status,
            "active_strategies": len(active),
            "profitable_strategies": len(profitable),
            "total_revenue": round(total_revenue, 2),
            "total_expenses": round(total_expenses, 2),
            "net_profit": round(total_revenue - total_expenses, 2),
            "concentration_risk": concentration_risk,
            "balance": round(balance, 2),
            "runway_days": budget.get("days_until_broke"),
        }

    # ── Opportunity Scoring ──────────────────────────────────

    def score_opportunity(self, opportunity: str,
                          revenue_potential: float,
                          cost_estimate: float,
                          time_to_revenue_days: int) -> dict[str, Any]:
        """Score a revenue opportunity for the orchestrator.

        Composite score weights:
        - Revenue/cost ratio: 35%
        - Time to revenue (faster = better): 25%
        - Risk (lower = better): 20%
        - Scale potential: 20%
        """
        # Revenue efficiency
        rev_ratio = revenue_potential / max(cost_estimate, 1)
        rev_score = min(rev_ratio / 5, 1.0)  # 5x ROI = perfect score

        # Time score: 30 days = 1.0, 180 days = 0.0
        time_score = max(1.0 - (time_to_revenue_days - 30) / 150, 0.0)

        # Risk: use LLM for qualitative assessment
        risk_assessment = self.think_json(
            f"Assess the risk of this opportunity on a scale of 0 to 1 "
            f"(0 = no risk, 1 = very risky). Return JSON with fields: "
            f"risk_score (float), risk_factors (list of strings).\n\n"
            f"Opportunity: {opportunity}\n"
            f"Revenue potential: €{revenue_potential}\n"
            f"Cost: €{cost_estimate}\n"
            f"Time to revenue: {time_to_revenue_days} days"
        )
        risk_score = risk_assessment.get("risk_score", 0.5)

        # Composite
        composite = (
            rev_score * 0.35
            + time_score * 0.25
            + (1 - risk_score) * 0.20
            + min(revenue_potential / 1000, 1.0) * 0.20
        )

        recommendation = "pursue" if composite >= 0.5 else "skip"
        if composite >= 0.75:
            recommendation = "high_priority"

        result = {
            "opportunity": opportunity,
            "revenue_potential": revenue_potential,
            "cost_estimate": cost_estimate,
            "time_to_revenue_days": time_to_revenue_days,
            "risk_score": round(risk_score, 2),
            "composite_score": round(composite, 2),
            "recommendation": recommendation,
        }

        self.db.execute_insert(
            "INSERT INTO opportunity_scores "
            "(opportunity, revenue_potential, cost_estimate, time_to_revenue_days, "
            "risk_score, composite_score, recommendation) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (opportunity, revenue_potential, cost_estimate,
             time_to_revenue_days, risk_score, composite, recommendation),
        )

        self.log_action("score_opportunity", opportunity[:100],
                        f"score={composite:.2f} rec={recommendation}")
        return result

    def get_top_opportunities(self, limit: int = 5) -> list[dict[str, Any]]:
        """Get highest-scored recent opportunities."""
        rows = self.db.execute(
            "SELECT * FROM opportunity_scores ORDER BY composite_score DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_latest_recommendations(self) -> list[dict[str, Any]]:
        """Get the most recent batch of investment recommendations."""
        rows = self.db.execute(
            "SELECT * FROM investment_recommendations "
            "WHERE DATE(created_at) = DATE('now') ORDER BY confidence DESC"
        )
        return [dict(r) for r in rows]
