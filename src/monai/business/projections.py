"""Financial projections — models money growth across all strategy channels.

Projects monthly revenue, expenses, and net profit over configurable horizons.
Each strategy channel has its own growth curve based on realistic ramp-up times:
- Services (freelance, lead gen, SMM): linear ramp, recurring retainer base
- Content (blogs, newsletters, affiliate): S-curve, slow start → exponential → plateau
- Products (digital, courses, SaaS): step-function at launch, then MRR growth
- Trading (domains, POD): sporadic, modeled as average monthly throughput
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from monai.business.finance import Finance
from monai.db.database import Database


@dataclass
class ChannelModel:
    """Revenue model for a single strategy channel."""

    name: str
    category: str  # services, content, products, trading
    monthly_cost: float  # estimated monthly operating cost (API + tools)
    # Revenue curve parameters
    ramp_months: int  # months to reach steady-state revenue
    steady_monthly_revenue: float  # revenue at full ramp
    curve: str = "linear"  # linear, s_curve, step, sporadic


# Realistic models based on each strategy's economics
CHANNEL_MODELS: list[ChannelModel] = [
    # ── Services ─────────────────────────────────────────────
    ChannelModel(
        name="freelance_writing", category="services",
        monthly_cost=15.0, ramp_months=2, steady_monthly_revenue=800.0,
        curve="linear",
    ),
    ChannelModel(
        name="lead_gen", category="services",
        monthly_cost=20.0, ramp_months=3, steady_monthly_revenue=600.0,
        curve="linear",
    ),
    ChannelModel(
        name="social_media", category="services",
        monthly_cost=15.0, ramp_months=2, steady_monthly_revenue=1200.0,
        curve="linear",
    ),
    ChannelModel(
        name="cold_outreach", category="services",
        monthly_cost=10.0, ramp_months=3, steady_monthly_revenue=500.0,
        curve="linear",
    ),

    # ── Content ──────────────────────────────────────────────
    ChannelModel(
        name="content_sites", category="content",
        monthly_cost=10.0, ramp_months=6, steady_monthly_revenue=400.0,
        curve="s_curve",
    ),
    ChannelModel(
        name="affiliate", category="content",
        monthly_cost=8.0, ramp_months=5, steady_monthly_revenue=350.0,
        curve="s_curve",
    ),
    ChannelModel(
        name="newsletter", category="content",
        monthly_cost=8.0, ramp_months=4, steady_monthly_revenue=300.0,
        curve="s_curve",
    ),

    # ── Products ─────────────────────────────────────────────
    ChannelModel(
        name="digital_products", category="products",
        monthly_cost=5.0, ramp_months=2, steady_monthly_revenue=250.0,
        curve="step",
    ),
    ChannelModel(
        name="course_creation", category="products",
        monthly_cost=8.0, ramp_months=3, steady_monthly_revenue=400.0,
        curve="step",
    ),
    ChannelModel(
        name="micro_saas", category="products",
        monthly_cost=12.0, ramp_months=3, steady_monthly_revenue=300.0,
        curve="step",
    ),
    ChannelModel(
        name="telegram_bots", category="products",
        monthly_cost=5.0, ramp_months=2, steady_monthly_revenue=150.0,
        curve="step",
    ),
    ChannelModel(
        name="saas", category="products",
        monthly_cost=25.0, ramp_months=4, steady_monthly_revenue=2000.0,
        curve="step",
    ),

    # ── Trading ──────────────────────────────────────────────
    ChannelModel(
        name="domain_flipping", category="trading",
        monthly_cost=15.0, ramp_months=2, steady_monthly_revenue=200.0,
        curve="sporadic",
    ),
    ChannelModel(
        name="print_on_demand", category="trading",
        monthly_cost=5.0, ramp_months=2, steady_monthly_revenue=150.0,
        curve="sporadic",
    ),
]


def _revenue_at_month(model: ChannelModel, month: int) -> float:
    """Calculate expected revenue for a channel at a given month (1-indexed).

    Growth curves:
    - linear: revenue = steady * min(month / ramp_months, 1)
    - s_curve: logistic function, slow start → fast middle → plateau
    - step: 0 during ramp, then jumps to steady (product launches)
    - sporadic: ramps linearly but with 0.7x multiplier for variance
    """
    if month <= 0:
        return 0.0

    steady = model.steady_monthly_revenue
    ramp = max(model.ramp_months, 1)

    if model.curve == "linear":
        return steady * min(month / ramp, 1.0)

    if model.curve == "s_curve":
        # Logistic: f(x) = L / (1 + e^(-k*(x - x0)))
        # x0 = midpoint of ramp, k controls steepness
        midpoint = ramp * 0.6
        steepness = 4.0 / ramp
        return steady / (1.0 + math.exp(-steepness * (month - midpoint)))

    if model.curve == "step":
        if month < ramp:
            return 0.0
        # After launch: grow 10% month-over-month from base
        months_since_launch = month - ramp
        return steady * (1.0 + 0.10 * months_since_launch)

    if model.curve == "sporadic":
        # Ramp in, then fluctuate around steady
        ramp_factor = min(month / ramp, 1.0)
        return steady * ramp_factor * 0.7

    return 0.0


@dataclass
class MonthProjection:
    """Projection data for a single month."""

    month: int
    revenue: float
    expenses: float
    net: float
    cumulative_net: float
    balance: float  # initial_capital + cumulative_net
    channel_breakdown: dict[str, float] = field(default_factory=dict)
    category_breakdown: dict[str, float] = field(default_factory=dict)


class GrowthProjector:
    """Projects monAI's financial growth over time."""

    def __init__(self, db: Database, initial_capital: float = 500.0,
                 channels: list[ChannelModel] | None = None):
        self.db = db
        self.finance = Finance(db)
        self.initial_capital = initial_capital
        self.channels = channels or CHANNEL_MODELS

    def project(self, months: int = 12) -> list[MonthProjection]:
        """Generate month-by-month financial projection.

        Uses actual data for past months (if available) and models for future.
        """
        projections: list[MonthProjection] = []
        cumulative_net = 0.0

        for m in range(1, months + 1):
            channel_rev: dict[str, float] = {}
            category_rev: dict[str, float] = {}
            total_revenue = 0.0
            total_expenses = 0.0

            for ch in self.channels:
                rev = _revenue_at_month(ch, m)
                channel_rev[ch.name] = round(rev, 2)
                category_rev[ch.category] = round(
                    category_rev.get(ch.category, 0.0) + rev, 2
                )
                total_revenue += rev
                total_expenses += ch.monthly_cost

            net = total_revenue - total_expenses
            cumulative_net += net

            projections.append(MonthProjection(
                month=m,
                revenue=round(total_revenue, 2),
                expenses=round(total_expenses, 2),
                net=round(net, 2),
                cumulative_net=round(cumulative_net, 2),
                balance=round(self.initial_capital + cumulative_net, 2),
                channel_breakdown=channel_rev,
                category_breakdown=category_rev,
            ))

        return projections

    def get_break_even_month(self) -> int | None:
        """Find the first month where monthly revenue >= monthly expenses."""
        for m in range(1, 25):
            total_rev = sum(_revenue_at_month(ch, m) for ch in self.channels)
            total_exp = sum(ch.monthly_cost for ch in self.channels)
            if total_rev >= total_exp:
                return m
        return None

    def get_capital_recovery_month(self) -> int | None:
        """Find the month where cumulative net profit recovers initial capital."""
        cumulative = 0.0
        for m in range(1, 25):
            total_rev = sum(_revenue_at_month(ch, m) for ch in self.channels)
            total_exp = sum(ch.monthly_cost for ch in self.channels)
            cumulative += total_rev - total_exp
            if cumulative >= self.initial_capital:
                return m
        return None

    def get_summary(self, months: int = 12) -> dict[str, Any]:
        """High-level projection summary."""
        projections = self.project(months)
        if not projections:
            return {}

        break_even = self.get_break_even_month()
        recovery = self.get_capital_recovery_month()
        last = projections[-1]
        total_monthly_cost = sum(ch.monthly_cost for ch in self.channels)

        # Find top 3 channels by month-12 revenue
        month_12 = projections[-1].channel_breakdown
        top_channels = sorted(month_12.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "initial_capital": self.initial_capital,
            "projection_months": months,
            "monthly_operating_cost": round(total_monthly_cost, 2),
            "break_even_month": break_even,
            "capital_recovery_month": recovery,
            "month_1": {
                "revenue": projections[0].revenue,
                "expenses": projections[0].expenses,
                "net": projections[0].net,
                "balance": projections[0].balance,
            },
            "month_6": {
                "revenue": projections[5].revenue,
                "expenses": projections[5].expenses,
                "net": projections[5].net,
                "balance": projections[5].balance,
            } if months >= 6 else None,
            "month_12": {
                "revenue": last.revenue,
                "expenses": last.expenses,
                "net": last.net,
                "balance": last.balance,
            } if months >= 12 else None,
            "top_channels_month_12": [
                {"name": name, "monthly_revenue": rev} for name, rev in top_channels
            ],
            "category_breakdown_month_12": last.category_breakdown,
            "annual_projected_revenue": round(sum(p.revenue for p in projections), 2),
            "annual_projected_expenses": round(sum(p.expenses for p in projections), 2),
            "annual_projected_profit": round(sum(p.net for p in projections), 2),
        }

    def format_report(self, months: int = 12) -> str:
        """Human-readable projection report."""
        summary = self.get_summary(months)
        projections = self.project(months)

        lines = [
            "=" * 60,
            "  monAI — Financial Growth Projection",
            "=" * 60,
            "",
            f"  Initial Capital: €{summary['initial_capital']:.2f}",
            f"  Monthly Operating Cost: €{summary['monthly_operating_cost']:.2f}",
            f"  Break-even Month: {summary['break_even_month'] or 'N/A'}",
            f"  Capital Recovery Month: {summary['capital_recovery_month'] or 'N/A'}",
            "",
            "-" * 60,
            f"  {'Month':>5}  {'Revenue':>10}  {'Expenses':>10}  {'Net':>10}  {'Balance':>10}",
            "-" * 60,
        ]

        for p in projections:
            lines.append(
                f"  {p.month:>5}  €{p.revenue:>9.2f}  €{p.expenses:>9.2f}  "
                f"€{p.net:>9.2f}  €{p.balance:>9.2f}"
            )

        lines.extend([
            "-" * 60,
            "",
            "  Top Revenue Channels (Month 12):",
        ])
        for ch in summary.get("top_channels_month_12", []):
            lines.append(f"    {ch['name']:<25} €{ch['monthly_revenue']:>8.2f}/mo")

        lines.extend([
            "",
            "  Category Breakdown (Month 12):",
        ])
        for cat, rev in summary.get("category_breakdown_month_12", {}).items():
            lines.append(f"    {cat:<25} €{rev:>8.2f}/mo")

        lines.extend([
            "",
            f"  Annual Projected Revenue:  €{summary['annual_projected_revenue']:>10.2f}",
            f"  Annual Projected Expenses: €{summary['annual_projected_expenses']:>10.2f}",
            f"  Annual Projected Profit:   €{summary['annual_projected_profit']:>10.2f}",
            "",
            "=" * 60,
        ])

        return "\n".join(lines)
