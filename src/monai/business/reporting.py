"""Automated financial reporting — generates and sends periodic reports.

Reports:
  - Monthly P&L (income statement)
  - Monthly balance sheet
  - Strategy performance breakdown
  - Bootstrap funding progress
  - Sent to creator via Telegram with formatted tables
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from monai.business.bootstrap import BootstrapWallet
from monai.business.finance import Finance, GeneralLedger
from monai.db.database import Database

logger = logging.getLogger(__name__)


class FinancialReporter:
    """Generates formatted financial reports and sends via Telegram."""

    def __init__(self, db: Database, ledger: GeneralLedger,
                 finance: Finance, bootstrap: BootstrapWallet | None = None):
        self.db = db
        self.ledger = ledger
        self.finance = finance
        self.bootstrap = bootstrap

    # ── Report Generation ────────────────────────────────────────

    def generate_monthly_report(self, year: int | None = None,
                                month: int | None = None) -> dict[str, Any]:
        """Generate a full monthly financial report."""
        now = datetime.now()
        if not year:
            year = now.year
        if not month:
            month = now.month

        start_date = f"{year}-{month:02d}-01"
        if month == 12:
            end_date = f"{year + 1}-01-01"
        else:
            end_date = f"{year}-{month + 1:02d}-01"
        # Last day of month
        end_inclusive = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

        income = self.ledger.get_income_statement(start_date, end_inclusive)
        balance = self.ledger.get_balance_sheet()
        integrity = self.ledger.verify_integrity()
        strategy_pnl = self.finance.get_strategy_pnl()

        report: dict[str, Any] = {
            "period": f"{year}-{month:02d}",
            "income_statement": income,
            "balance_sheet": balance,
            "integrity": integrity,
            "strategy_pnl": strategy_pnl,
        }

        if self.bootstrap:
            report["bootstrap"] = self.bootstrap.get_bootstrap_summary()

        return report

    def format_telegram_report(self, report: dict[str, Any]) -> str:
        """Format a financial report as a Telegram message."""
        lines: list[str] = []
        period = report.get("period", "current")

        lines.append(f"*Financial Report — {period}*")
        lines.append("")

        # Income Statement
        income = report.get("income_statement", {})
        lines.append("*P&L (Income Statement)*")
        lines.append("```")
        lines.append(f"{'Revenue':.<30} €{income.get('total_revenue', 0):>10,.2f}")

        for rev in income.get("revenue", []):
            name = rev["name"].replace("Revenue - ", "")
            lines.append(f"  {name:.<28} €{rev['balance']:>10,.2f}")

        lines.append(f"{'Expenses':.<30} €{income.get('total_expenses', 0):>10,.2f}")

        for exp in income.get("expenses", []):
            name = exp["name"].replace("Expense - ", "")
            lines.append(f"  {name:.<28} €{exp['balance']:>10,.2f}")

        lines.append("─" * 42)
        net = income.get("net_income", 0)
        emoji = "+" if net >= 0 else ""
        lines.append(f"{'NET INCOME':.<30} €{emoji}{net:>9,.2f}")
        lines.append("```")
        lines.append("")

        # Balance Sheet
        bs = report.get("balance_sheet", {})
        lines.append("*Balance Sheet*")
        lines.append("```")
        lines.append(f"{'Assets':.<30} €{bs.get('assets', 0):>10,.2f}")

        for acct in bs.get("detail", {}).get("asset_accounts", []):
            if acct["balance"] != 0:
                name = acct["name"].replace("Cash - ", "")
                lines.append(f"  {name:.<28} €{acct['balance']:>10,.2f}")

        lines.append(f"{'Liabilities':.<30} €{bs.get('liabilities', 0):>10,.2f}")
        lines.append(f"{'Equity':.<30} €{bs.get('total_equity', 0):>10,.2f}")
        lines.append("─" * 42)
        balanced = "BALANCED" if bs.get("balanced") else "UNBALANCED!"
        lines.append(f"Status: {balanced}")
        lines.append("```")
        lines.append("")

        # Strategy Performance
        strategy_pnl = report.get("strategy_pnl", [])
        if strategy_pnl:
            lines.append("*Strategy Performance*")
            lines.append("```")
            for s in strategy_pnl[:10]:
                name = s.get("name", "?")[:20]
                rev = s.get("revenue", 0)
                exp = s.get("expenses", 0)
                net_s = rev - exp
                sign = "+" if net_s >= 0 else ""
                lines.append(f"{name:.<22} €{sign}{net_s:>8,.2f}")
            lines.append("```")
            lines.append("")

        # Bootstrap Status
        bootstrap = report.get("bootstrap")
        if bootstrap and bootstrap.get("phase") != "self_sustaining":
            lines.append("*Bootstrap Funding*")
            lines.append("```")
            cf = bootstrap.get("crowdfunding", {})
            lines.append(f"{'Raised':.<20} €{cf.get('total_raised', 0):>10,.2f}")
            lines.append(f"{'  Creator seed':.<20} €{cf.get('creator_seed', 0):>10,.2f}")
            lines.append(f"{'  Organic':.<20} €{cf.get('organic_raised', 0):>10,.2f}")
            lines.append(f"{'Spent':.<20} €{cf.get('total_spent', 0):>10,.2f}")
            lines.append(f"{'Available':.<20} €{cf.get('available', 0):>10,.2f}")
            lines.append(f"Phase: {bootstrap.get('phase', '?')}")
            lines.append("```")
            lines.append("")

        # Integrity
        integrity = report.get("integrity", {})
        if not integrity.get("balanced"):
            lines.append("⚠ *INTEGRITY WARNING: Books are NOT balanced!*")

        return "\n".join(lines)

    def generate_daily_snapshot(self) -> str:
        """Quick daily financial snapshot for Telegram."""
        today = datetime.now().strftime("%Y-%m-%d")
        month_start = datetime.now().strftime("%Y-%m-01")

        income = self.ledger.get_income_statement(month_start, today)
        bs = self.ledger.get_balance_sheet()
        daily = self.finance.get_daily_summary(today)

        lines = [
            f"*Daily Snapshot — {today}*",
            "```",
            f"Today:  €{daily.get('revenue', 0):>8,.2f} rev  €{daily.get('expense', 0):>8,.2f} exp",
            f"MTD:    €{income.get('total_revenue', 0):>8,.2f} rev  €{income.get('total_expenses', 0):>8,.2f} exp",
            f"Net:    €{income.get('net_income', 0):>+8,.2f}",
            f"Assets: €{bs.get('assets', 0):>8,.2f}",
            "```",
        ]
        return "\n".join(lines)

    def should_send_monthly_report(self) -> bool:
        """Check if it's time for the monthly report (1st of month)."""
        return datetime.now().day == 1

    def should_send_weekly_report(self) -> bool:
        """Check if it's time for the weekly report (Monday)."""
        return datetime.now().weekday() == 0

    # ── Strategy Dashboard ───────────────────────────────────────

    def generate_strategy_dashboard(self) -> str:
        """Generate a strategy performance dashboard for Telegram."""
        pnl = self.finance.get_strategy_pnl()
        if not pnl:
            return "*Strategy Dashboard*\nNo active strategies."

        # Get strategy statuses
        strategies = self.db.execute(
            "SELECT id, name, category, status, allocated_budget FROM strategies "
            "ORDER BY status, name"
        )
        status_map = {s["id"]: dict(s) for s in strategies}

        lines = ["*Strategy Performance Dashboard*", "```"]

        # Header
        lines.append(f"{'Strategy':<18} {'Status':<8} {'Rev':>8} {'Exp':>8} {'Net':>9}")
        lines.append("─" * 55)

        total_rev = 0.0
        total_exp = 0.0

        for s in pnl:
            name = s.get("name", "?")[:17]
            rev = s.get("revenue", 0)
            exp = s.get("expenses", 0)
            net = rev - exp
            total_rev += rev
            total_exp += exp

            info = status_map.get(s.get("id", 0), {})
            status = info.get("status", "?")[:7]
            sign = "+" if net >= 0 else ""
            lines.append(
                f"{name:<18} {status:<8} €{rev:>7,.0f} €{exp:>7,.0f} €{sign}{net:>7,.0f}"
            )

        lines.append("─" * 55)
        total_net = total_rev - total_exp
        sign = "+" if total_net >= 0 else ""
        lines.append(
            f"{'TOTAL':<18} {'':8} €{total_rev:>7,.0f} €{total_exp:>7,.0f} €{sign}{total_net:>7,.0f}"
        )
        lines.append("```")

        # ROI summary
        if total_exp > 0:
            roi = (total_rev / total_exp - 1) * 100
            lines.append(f"\nOverall ROI: {roi:+.1f}%")

        return "\n".join(lines)

    def get_strategy_performance(self) -> dict[str, Any]:
        """Detailed strategy performance data for orchestrator decision-making.

        Returns metrics per strategy including:
        - Revenue, expenses, net, ROI
        - 7-day and 30-day trends
        - Status and recommendation (continue, review, pause, scale)
        """
        pnl = self.finance.get_strategy_pnl()
        strategies = self.db.execute(
            "SELECT id, name, category, status, allocated_budget, created_at "
            "FROM strategies ORDER BY name"
        )
        status_map = {s["id"]: dict(s) for s in strategies}

        results = []
        for s in pnl:
            sid = s.get("id", 0)
            info = status_map.get(sid, {})
            rev = s.get("revenue", 0)
            exp = s.get("expenses", 0)
            net = rev - exp
            budget = info.get("allocated_budget", 0)

            # 7-day trend
            rev_7d = self._get_strategy_revenue(sid, 7)
            exp_7d = self._get_strategy_expenses(sid, 7)
            net_7d = rev_7d - exp_7d

            # 30-day trend
            rev_30d = self._get_strategy_revenue(sid, 30)
            exp_30d = self._get_strategy_expenses(sid, 30)
            net_30d = rev_30d - exp_30d

            # ROI calculation
            roi = ((rev / exp) - 1) * 100 if exp > 0 else 0

            # Recommendation logic (later rules override earlier)
            recommendation = "continue"
            if rev_7d > rev_30d / 4 and rev_7d > 0 and net >= 0:
                recommendation = "scale"
            if net < 0 and net_7d < 0:
                recommendation = "review"
            if net < 0 and net_30d < 0 and exp > budget * 0.5:
                recommendation = "pause"

            results.append({
                "id": sid,
                "name": s.get("name", "?"),
                "category": s.get("category", "?"),
                "status": info.get("status", "?"),
                "revenue": rev,
                "expenses": exp,
                "net": net,
                "roi_pct": round(roi, 1),
                "budget": budget,
                "budget_used_pct": round((exp / budget) * 100, 1) if budget > 0 else 0,
                "trend_7d": {"revenue": rev_7d, "expenses": exp_7d, "net": net_7d},
                "trend_30d": {"revenue": rev_30d, "expenses": exp_30d, "net": net_30d},
                "recommendation": recommendation,
            })

        # Sort by net descending
        results.sort(key=lambda x: x["net"], reverse=True)

        total_rev = sum(r["revenue"] for r in results)
        total_exp = sum(r["expenses"] for r in results)

        return {
            "strategies": results,
            "total_revenue": total_rev,
            "total_expenses": total_exp,
            "total_net": total_rev - total_exp,
            "overall_roi_pct": round(((total_rev / total_exp) - 1) * 100, 1) if total_exp > 0 else 0,
            "strategies_to_review": [r for r in results if r["recommendation"] == "review"],
            "strategies_to_pause": [r for r in results if r["recommendation"] == "pause"],
            "strategies_to_scale": [r for r in results if r["recommendation"] == "scale"],
        }

    def _get_strategy_revenue(self, strategy_id: int, days: int) -> float:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM transactions "
            "WHERE type = 'revenue' AND strategy_id = ? AND created_at >= ?",
            (strategy_id, since),
        )
        return rows[0]["total"] if rows else 0.0

    def _get_strategy_expenses(self, strategy_id: int, days: int) -> float:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM transactions "
            "WHERE type = 'expense' AND strategy_id = ? AND created_at >= ?",
            (strategy_id, since),
        )
        return rows[0]["total"] if rows else 0.0
