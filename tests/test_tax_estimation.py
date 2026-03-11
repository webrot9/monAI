"""Tests for monai.business.tax_estimation — quarterly tax estimates."""

from __future__ import annotations

import pytest

from monai.business.tax_estimation import TaxEstimator, TaxEstimate


@pytest.fixture
def estimator(db):
    return TaxEstimator(db)


def _record_tx(db, amount, tx_type="revenue", date="2026-02-15"):
    """Insert a transaction with a specific date."""
    db.execute_insert(
        "INSERT INTO transactions (amount, type, category, description, created_at) "
        "VALUES (?, ?, ?, 'test', ?)",
        (amount, tx_type, tx_type, f"{date} 12:00:00"),
    )


class TestForfettarioEstimation:
    def test_basic_forfettario(self, estimator, db):
        """Standard forfettario: 78% coefficient, 15% tax + INPS."""
        _record_tx(db, 10000.0, "revenue", "2026-01-15")
        est = estimator.estimate_quarterly_tax("it_forfettario", 2026, 1)

        assert est.gross_income == 10000.0
        assert est.taxable_income == 7800.0  # 10000 * 0.78
        # substitute tax: 7800 * 0.15 = 1170
        assert est.breakdown["substitute_tax"] == 1170.0
        # INPS: 7800 * 0.2607 = 2033.46
        assert abs(est.breakdown["inps_contribution"] - 2033.46) < 0.01
        assert est.estimated_tax == round(1170.0 + 2033.46, 2)

    def test_forfettario_startup_rate(self, estimator, db):
        """Startup forfettario uses 5% instead of 15%."""
        _record_tx(db, 5000.0, "revenue", "2026-01-15")
        est = estimator.estimate_quarterly_tax(
            "it_forfettario", 2026, 1, is_startup=True,
        )
        assert est.breakdown["substitute_tax_rate"] == 0.05
        # 5000 * 0.78 = 3900 taxable, 3900 * 0.05 = 195
        assert est.breakdown["substitute_tax"] == 195.0

    def test_forfettario_ecommerce_coefficient(self, estimator, db):
        """Ecommerce uses 40% coefficient."""
        _record_tx(db, 10000.0, "revenue", "2026-01-15")
        est = estimator.estimate_quarterly_tax(
            "it_forfettario", 2026, 1, ateco_category="ecommerce",
        )
        assert est.taxable_income == 4000.0  # 10000 * 0.40
        assert est.breakdown["profitability_coefficient"] == 0.40

    def test_forfettario_ignores_expenses(self, estimator, db):
        """Forfettario doesn't deduct actual expenses (flat coefficient instead)."""
        _record_tx(db, 10000.0, "revenue", "2026-01-15")
        _record_tx(db, 3000.0, "expense", "2026-02-15")
        est = estimator.estimate_quarterly_tax("it_forfettario", 2026, 1)
        assert est.deductible_expenses == 0.0
        assert "not deductible under forfettario" in est.notes[-1]

    def test_forfettario_zero_income(self, estimator, db):
        """No income → no tax."""
        est = estimator.estimate_quarterly_tax("it_forfettario", 2026, 1)
        assert est.gross_income == 0.0
        assert est.estimated_tax == 0.0
        assert est.tax_rate_effective == 0.0

    def test_forfettario_effective_rate(self, estimator, db):
        """Effective rate is total tax / gross income."""
        _record_tx(db, 20000.0, "revenue", "2026-03-01")
        est = estimator.estimate_quarterly_tax("it_forfettario", 2026, 1)
        assert est.tax_rate_effective == round(est.estimated_tax / 20000.0, 4)


class TestUSFederalEstimation:
    def test_basic_us_federal(self, estimator, db):
        """US LLC: net profit minus SE tax deduction → brackets."""
        _record_tx(db, 20000.0, "revenue", "2026-01-15")
        _record_tx(db, 5000.0, "expense", "2026-02-01")
        est = estimator.estimate_quarterly_tax("us_federal", 2026, 1)

        assert est.gross_income == 20000.0
        assert est.deductible_expenses == 5000.0
        assert est.taxable_income == 15000.0
        assert est.estimated_tax > 0
        assert est.currency == "USD"
        assert "Wyoming" in est.notes[0]

    def test_us_zero_profit(self, estimator, db):
        """No profit → no tax."""
        _record_tx(db, 5000.0, "revenue", "2026-01-15")
        _record_tx(db, 6000.0, "expense", "2026-02-01")
        est = estimator.estimate_quarterly_tax("us_federal", 2026, 1)
        assert est.estimated_tax == 0.0
        assert "net profit <= 0" in est.notes[0]

    def test_us_se_tax_computed(self, estimator, db):
        """Self-employment tax is calculated."""
        _record_tx(db, 10000.0, "revenue", "2026-01-15")
        est = estimator.estimate_quarterly_tax("us_federal", 2026, 1)
        # SE tax on 92.35% of 10000 at 15.3%
        expected_se = round(10000 * 0.9235 * 0.153, 2)
        assert est.breakdown["se_tax"] == expected_se

    def test_us_bracket_calculation(self, estimator):
        """Tax brackets compute correctly."""
        # First bracket: $11600 at 10% = $1160
        assert estimator._compute_bracket_tax(11600) == 1160.0
        # $0 income
        assert estimator._compute_bracket_tax(0) == 0.0
        # Negative
        assert estimator._compute_bracket_tax(-1000) == 0.0

    def test_us_with_other_income(self, estimator, db):
        """Other income shifts brackets up."""
        _record_tx(db, 10000.0, "revenue", "2026-01-15")
        est_alone = estimator.estimate_quarterly_tax("us_federal", 2026, 1)
        est_with = estimator.estimate_quarterly_tax(
            "us_federal", 2026, 1, other_income=50000.0,
        )
        # More other income → higher bracket → more tax
        assert est_with.estimated_tax > est_alone.estimated_tax


class TestTaxEstimatorGeneral:
    def test_quarter_dates(self, estimator):
        """Quarter date ranges are correct."""
        assert estimator._quarter_dates(2026, 1) == ("2026-01-01", "2026-03-31")
        assert estimator._quarter_dates(2026, 2) == ("2026-04-01", "2026-06-30")
        assert estimator._quarter_dates(2026, 3) == ("2026-07-01", "2026-09-30")
        assert estimator._quarter_dates(2026, 4) == ("2026-10-01", "2026-12-31")

    def test_estimate_persisted(self, estimator, db):
        """Estimates are saved to DB."""
        _record_tx(db, 1000.0, "revenue", "2026-01-15")
        estimator.estimate_quarterly_tax("it_forfettario", 2026, 1)

        rows = db.execute("SELECT * FROM tax_estimates")
        assert len(rows) == 1
        assert rows[0]["jurisdiction"] == "it_forfettario"
        assert rows[0]["quarter"] == 1

    def test_get_estimates_filtered(self, estimator, db):
        """Filtering by year and jurisdiction works."""
        _record_tx(db, 1000.0, "revenue", "2026-01-15")
        _record_tx(db, 2000.0, "revenue", "2026-04-15")
        estimator.estimate_quarterly_tax("it_forfettario", 2026, 1)
        estimator.estimate_quarterly_tax("us_federal", 2026, 1)
        estimator.estimate_quarterly_tax("it_forfettario", 2026, 2)

        all_est = estimator.get_estimates()
        assert len(all_est) == 3

        it_only = estimator.get_estimates(jurisdiction="it_forfettario")
        assert len(it_only) == 2

        q1_all = estimator.get_estimates(year=2026)
        assert len(q1_all) == 3

    def test_annual_summary(self, estimator, db):
        """Annual summary aggregates quarterly estimates."""
        for q in range(1, 5):
            month = (q - 1) * 3 + 1
            _record_tx(db, 5000.0, "revenue", f"2026-{month:02d}-15")
            estimator.estimate_quarterly_tax("it_forfettario", 2026, q)

        summary = estimator.get_annual_summary(2026, "it_forfettario")
        assert len(summary["quarters"]) == 4
        assert summary["total_income"] == 20000.0
        assert summary["total_tax"] > 0
        assert summary["effective_rate"] > 0

    def test_annual_summary_empty(self, estimator):
        """Annual summary with no data returns zeros."""
        summary = estimator.get_annual_summary(2025, "it_forfettario")
        assert summary["total_income"] == 0
        assert summary["total_tax"] == 0

    def test_unknown_jurisdiction(self, estimator, db):
        """Unknown jurisdiction creates estimate with warning note."""
        _record_tx(db, 1000.0, "revenue", "2026-01-15")
        est = estimator.estimate_quarterly_tax("mars_colony", 2026, 1)
        assert "Unknown jurisdiction" in est.notes[0]
        assert est.estimated_tax == 0.0

    def test_net_after_tax_property(self):
        """TaxEstimate.net_after_tax computes correctly."""
        est = TaxEstimate(
            jurisdiction="test", tax_year=2026, quarter=1,
            gross_income=10000, deductible_expenses=2000, estimated_tax=1500,
        )
        assert est.net_after_tax == 6500.0

    def test_format_telegram_report(self, estimator, db):
        """Telegram report formats correctly."""
        _record_tx(db, 8000.0, "revenue", "2026-01-15")
        est = estimator.estimate_quarterly_tax("it_forfettario", 2026, 1)
        msg = estimator.format_telegram_report(est)
        assert "Tax Estimate" in msg
        assert "IT_FORFETTARIO" in msg
        assert "Q1/2026" in msg
        assert "Gross Income" in msg
        assert "```" in msg

    def test_defaults_to_current_quarter(self, estimator, db):
        """Omitting year/quarter uses current date."""
        est = estimator.estimate_quarterly_tax("it_forfettario")
        from datetime import datetime
        now = datetime.now()
        assert est.tax_year == now.year
        expected_q = (now.month - 1) // 3 + 1
        assert est.quarter == expected_q
