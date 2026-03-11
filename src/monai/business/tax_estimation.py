"""Tax estimation module — quarterly estimated tax calculations.

Computes estimated tax liabilities for:
  - US LLC (Wyoming) pass-through income → creator's personal tax
  - Italian P.IVA forfettario (flat-rate regime)

Uses GL data + exchange rates to determine taxable income per jurisdiction.
Integrates with CorporateManager for entity awareness and Commercialista for budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

TAX_ESTIMATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS tax_estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER,
    jurisdiction TEXT NOT NULL,
    tax_year INTEGER NOT NULL,
    quarter INTEGER NOT NULL,          -- 1-4
    gross_income REAL DEFAULT 0,
    deductible_expenses REAL DEFAULT 0,
    taxable_income REAL DEFAULT 0,
    estimated_tax REAL DEFAULT 0,
    tax_rate_used REAL DEFAULT 0,
    currency TEXT DEFAULT 'EUR',
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tax_est_entity_year
    ON tax_estimates(entity_id, tax_year, quarter);
"""

# Italian forfettario flat-rate coefficients by ATECO category
_FORFETTARIO_COEFFICIENTS = {
    "digital_services": 0.78,   # 78% profitability coefficient (ATECO 62/63)
    "consulting": 0.78,
    "ecommerce": 0.40,
    "content_creation": 0.78,
    "default": 0.78,
}

# Italian forfettario tax rate (substitute tax)
_FORFETTARIO_TAX_RATE = 0.15
_FORFETTARIO_TAX_RATE_STARTUP = 0.05  # First 5 years for new businesses

# INPS contribution rate for gestione separata (forfettario)
_INPS_RATE = 0.2607  # 26.07% of taxable income

# US federal income tax brackets 2026 (simplified, single filer, estimated)
_US_FEDERAL_BRACKETS = [
    (11600, 0.10),
    (47150, 0.12),
    (100525, 0.22),
    (191950, 0.24),
    (243725, 0.32),
    (609350, 0.35),
    (float("inf"), 0.37),
]

# Self-employment tax rate (Social Security + Medicare)
_US_SE_TAX_RATE = 0.153  # 15.3% on 92.35% of net earnings


@dataclass
class TaxEstimate:
    """Result of a quarterly tax estimation."""
    jurisdiction: str
    tax_year: int
    quarter: int
    gross_income: float = 0.0
    deductible_expenses: float = 0.0
    taxable_income: float = 0.0
    estimated_tax: float = 0.0
    tax_rate_effective: float = 0.0
    currency: str = "EUR"
    breakdown: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def net_after_tax(self) -> float:
        return round(self.gross_income - self.deductible_expenses - self.estimated_tax, 2)


class TaxEstimator:
    """Quarterly tax estimation engine.

    Pulls revenue/expense data from the GL and transactions table,
    applies jurisdiction-specific tax rules, and stores estimates.
    """

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(TAX_ESTIMATION_SCHEMA)

    def estimate_quarterly_tax(
        self,
        jurisdiction: str,
        year: int | None = None,
        quarter: int | None = None,
        entity_id: int | None = None,
        *,
        ateco_category: str = "digital_services",
        is_startup: bool = False,
        other_income: float = 0.0,
    ) -> TaxEstimate:
        """Compute estimated tax for a quarter.

        Args:
            jurisdiction: "it_forfettario", "us_llc", or "us_federal".
            year: Tax year (default: current year).
            quarter: Quarter 1-4 (default: current quarter).
            entity_id: Optional LLC entity ID for filtering.
            ateco_category: Italian ATECO category for forfettario coefficient.
            is_startup: If True, uses 5% startup rate (forfettario only).
            other_income: Additional income outside monAI (for bracket calculation).

        Returns:
            TaxEstimate with computed tax liability.
        """
        now = datetime.now()
        year = year or now.year
        quarter = quarter or ((now.month - 1) // 3 + 1)

        # Get quarter date range
        q_start, q_end = self._quarter_dates(year, quarter)

        # Pull income and expenses for the quarter
        income = self._get_quarterly_income(q_start, q_end, entity_id)
        expenses = self._get_quarterly_expenses(q_start, q_end, entity_id)

        if jurisdiction == "it_forfettario":
            estimate = self._estimate_forfettario(
                income, expenses, year, quarter,
                ateco_category=ateco_category, is_startup=is_startup,
            )
        elif jurisdiction in ("us_llc", "us_federal"):
            estimate = self._estimate_us_federal(
                income, expenses, year, quarter,
                other_income=other_income,
            )
        else:
            estimate = TaxEstimate(
                jurisdiction=jurisdiction, tax_year=year, quarter=quarter,
                gross_income=income, deductible_expenses=expenses,
                notes=[f"Unknown jurisdiction: {jurisdiction}"],
            )

        # Persist estimate
        self._save_estimate(estimate, entity_id)

        logger.info(
            f"Tax estimate {jurisdiction} Q{quarter}/{year}: "
            f"income={estimate.gross_income:.2f}, tax={estimate.estimated_tax:.2f}"
        )
        return estimate

    def _estimate_forfettario(
        self, income: float, expenses: float,
        year: int, quarter: int,
        ateco_category: str = "digital_services",
        is_startup: bool = False,
    ) -> TaxEstimate:
        """Italian forfettario (flat-rate) tax estimation.

        Forfettario doesn't deduct actual expenses — instead applies a
        profitability coefficient to gross revenue. Tax is on the result.
        """
        coefficient = _FORFETTARIO_COEFFICIENTS.get(
            ateco_category, _FORFETTARIO_COEFFICIENTS["default"]
        )
        taxable_income = round(income * coefficient, 2)
        tax_rate = _FORFETTARIO_TAX_RATE_STARTUP if is_startup else _FORFETTARIO_TAX_RATE
        substitute_tax = round(taxable_income * tax_rate, 2)

        # INPS on taxable income
        inps = round(taxable_income * _INPS_RATE, 2)

        total_tax = round(substitute_tax + inps, 2)
        effective_rate = round(total_tax / income, 4) if income > 0 else 0.0

        return TaxEstimate(
            jurisdiction="it_forfettario",
            tax_year=year,
            quarter=quarter,
            gross_income=income,
            deductible_expenses=0.0,  # Forfettario doesn't use actual expenses
            taxable_income=taxable_income,
            estimated_tax=total_tax,
            tax_rate_effective=effective_rate,
            currency="EUR",
            breakdown={
                "profitability_coefficient": coefficient,
                "taxable_income": taxable_income,
                "substitute_tax_rate": tax_rate,
                "substitute_tax": substitute_tax,
                "inps_rate": _INPS_RATE,
                "inps_contribution": inps,
            },
            notes=[
                f"ATECO category: {ateco_category} (coeff {coefficient})",
                f"{'Startup' if is_startup else 'Standard'} rate: {tax_rate*100:.0f}%",
                f"INPS gestione separata: {_INPS_RATE*100:.2f}%",
                f"Actual expenses ({expenses:.2f}) not deductible under forfettario",
            ],
        )

    def _estimate_us_federal(
        self, income: float, expenses: float,
        year: int, quarter: int,
        other_income: float = 0.0,
    ) -> TaxEstimate:
        """US federal estimated tax for LLC pass-through income.

        Wyoming LLC has no state tax. Federal tax is on net profit
        (passed through to owner's personal return).
        """
        net_profit = round(income - expenses, 2)
        if net_profit <= 0:
            return TaxEstimate(
                jurisdiction="us_federal",
                tax_year=year, quarter=quarter,
                gross_income=income, deductible_expenses=expenses,
                taxable_income=0.0, estimated_tax=0.0,
                currency="USD",
                notes=["No tax owed — net profit <= 0"],
            )

        # Self-employment tax (on 92.35% of net)
        se_taxable = round(net_profit * 0.9235, 2)
        se_tax = round(se_taxable * _US_SE_TAX_RATE, 2)

        # Deduct half of SE tax from income for income tax calculation
        se_deduction = round(se_tax / 2, 2)
        adjusted_income = net_profit - se_deduction + other_income

        # Annualize quarterly income for bracket calculation
        annual_income = adjusted_income * 4
        annual_federal = self._compute_bracket_tax(annual_income)
        # Quarterly portion
        federal_tax = round(annual_federal / 4, 2)

        total_tax = round(federal_tax + se_tax, 2)
        effective_rate = round(total_tax / net_profit, 4) if net_profit > 0 else 0.0

        return TaxEstimate(
            jurisdiction="us_federal",
            tax_year=year, quarter=quarter,
            gross_income=income,
            deductible_expenses=expenses,
            taxable_income=net_profit,
            estimated_tax=total_tax,
            tax_rate_effective=effective_rate,
            currency="USD",
            breakdown={
                "net_profit": net_profit,
                "se_taxable_base": se_taxable,
                "se_tax": se_tax,
                "se_deduction": se_deduction,
                "adjusted_income": adjusted_income,
                "annual_income_projected": annual_income,
                "annual_federal_projected": annual_federal,
                "quarterly_federal": federal_tax,
            },
            notes=[
                "Wyoming LLC — no state income tax",
                f"SE tax: {_US_SE_TAX_RATE*100:.1f}% on 92.35% of net",
                f"Federal brackets applied to annualized income (÷4 for quarterly)",
                f"Other income included: ${other_income:.2f}",
            ],
        )

    def _compute_bracket_tax(self, annual_income: float) -> float:
        """Compute US federal income tax using progressive brackets."""
        if annual_income <= 0:
            return 0.0

        tax = 0.0
        prev_limit = 0.0
        for limit, rate in _US_FEDERAL_BRACKETS:
            taxable_in_bracket = min(annual_income, limit) - prev_limit
            if taxable_in_bracket <= 0:
                break
            tax += taxable_in_bracket * rate
            prev_limit = limit

        return round(tax, 2)

    # ── Data Queries ───────────────────────────────────────────

    def _get_quarterly_income(self, start: str, end: str,
                              entity_id: int | None = None) -> float:
        """Sum revenue transactions for the quarter."""
        query = (
            "SELECT COALESCE(SUM(amount), 0) as total FROM transactions "
            "WHERE type = 'revenue' AND DATE(created_at) >= ? AND DATE(created_at) <= ?"
        )
        params: list[Any] = [start, end]
        if entity_id is not None:
            query += " AND strategy_id IN (SELECT id FROM strategies WHERE entity_id = ?)"
            params.append(entity_id)
        rows = self.db.execute(query, tuple(params))
        return rows[0]["total"]

    def _get_quarterly_expenses(self, start: str, end: str,
                                entity_id: int | None = None) -> float:
        """Sum expense transactions for the quarter."""
        query = (
            "SELECT COALESCE(SUM(amount), 0) as total FROM transactions "
            "WHERE type = 'expense' AND DATE(created_at) >= ? AND DATE(created_at) <= ?"
        )
        params: list[Any] = [start, end]
        if entity_id is not None:
            query += " AND strategy_id IN (SELECT id FROM strategies WHERE entity_id = ?)"
            params.append(entity_id)
        rows = self.db.execute(query, tuple(params))
        return rows[0]["total"]

    def _quarter_dates(self, year: int, quarter: int) -> tuple[str, str]:
        """Return (start_date, end_date) strings for a quarter."""
        starts = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
        ends = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
        return f"{year}-{starts[quarter]}", f"{year}-{ends[quarter]}"

    def _save_estimate(self, est: TaxEstimate, entity_id: int | None) -> None:
        """Persist a tax estimate to the DB."""
        self.db.execute_insert(
            "INSERT INTO tax_estimates "
            "(entity_id, jurisdiction, tax_year, quarter, gross_income, "
            "deductible_expenses, taxable_income, estimated_tax, tax_rate_used, "
            "currency, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                est.jurisdiction,
                est.tax_year,
                est.quarter,
                est.gross_income,
                est.deductible_expenses,
                est.taxable_income,
                est.estimated_tax,
                est.tax_rate_effective,
                est.currency,
                "; ".join(est.notes),
            ),
        )

    # ── Reporting ──────────────────────────────────────────────

    def get_estimates(self, year: int | None = None,
                      jurisdiction: str | None = None) -> list[dict[str, Any]]:
        """Get stored tax estimates, optionally filtered."""
        query = "SELECT * FROM tax_estimates WHERE 1=1"
        params: list[Any] = []
        if year:
            query += " AND tax_year = ?"
            params.append(year)
        if jurisdiction:
            query += " AND jurisdiction = ?"
            params.append(jurisdiction)
        query += " ORDER BY tax_year DESC, quarter DESC"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def get_annual_summary(self, year: int,
                           jurisdiction: str) -> dict[str, Any]:
        """Get full-year tax summary for a jurisdiction."""
        estimates = self.get_estimates(year=year, jurisdiction=jurisdiction)
        if not estimates:
            return {
                "year": year, "jurisdiction": jurisdiction,
                "quarters": [], "total_income": 0, "total_tax": 0,
            }

        total_income = sum(e["gross_income"] for e in estimates)
        total_expenses = sum(e["deductible_expenses"] for e in estimates)
        total_tax = sum(e["estimated_tax"] for e in estimates)

        return {
            "year": year,
            "jurisdiction": jurisdiction,
            "quarters": estimates,
            "total_income": round(total_income, 2),
            "total_expenses": round(total_expenses, 2),
            "total_tax": round(total_tax, 2),
            "effective_rate": round(total_tax / total_income, 4) if total_income > 0 else 0,
        }

    def format_telegram_report(self, estimate: TaxEstimate) -> str:
        """Format a tax estimate for Telegram notification."""
        lines = [
            f"*Tax Estimate — {estimate.jurisdiction.upper()} Q{estimate.quarter}/{estimate.tax_year}*",
            "```",
            f"Gross Income:     {estimate.currency} {estimate.gross_income:>10.2f}",
        ]
        if estimate.deductible_expenses > 0:
            lines.append(
                f"Deductions:       {estimate.currency} {estimate.deductible_expenses:>10.2f}"
            )
        lines.extend([
            f"Taxable Income:   {estimate.currency} {estimate.taxable_income:>10.2f}",
            f"Estimated Tax:    {estimate.currency} {estimate.estimated_tax:>10.2f}",
            f"Effective Rate:   {estimate.tax_rate_effective*100:>10.1f}%",
            f"Net After Tax:    {estimate.currency} {estimate.net_after_tax:>10.2f}",
            "```",
        ])

        if estimate.breakdown:
            lines.append("\n*Breakdown:*")
            for key, val in estimate.breakdown.items():
                label = key.replace("_", " ").title()
                if isinstance(val, float) and val < 1:
                    lines.append(f"- {label}: {val*100:.2f}%")
                else:
                    lines.append(f"- {label}: {val:,.2f}")

        return "\n".join(lines)
