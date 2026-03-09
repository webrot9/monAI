"""Tests for financial growth projection system."""

import math

import pytest

from monai.business.projections import (
    CHANNEL_MODELS,
    ChannelModel,
    GrowthProjector,
    MonthProjection,
    _revenue_at_month,
)
from monai.db.database import Database


# ── Revenue curve tests ──────────────────────────────────────


class TestRevenueCurves:
    def test_linear_ramp_zero_at_start(self):
        m = ChannelModel("test", "services", 10, ramp_months=4,
                         steady_monthly_revenue=400, curve="linear")
        assert _revenue_at_month(m, 0) == 0.0

    def test_linear_ramp_quarter(self):
        m = ChannelModel("test", "services", 10, ramp_months=4,
                         steady_monthly_revenue=400, curve="linear")
        assert _revenue_at_month(m, 1) == pytest.approx(100.0)

    def test_linear_ramp_full(self):
        m = ChannelModel("test", "services", 10, ramp_months=4,
                         steady_monthly_revenue=400, curve="linear")
        assert _revenue_at_month(m, 4) == pytest.approx(400.0)

    def test_linear_ramp_capped_after_ramp(self):
        m = ChannelModel("test", "services", 10, ramp_months=4,
                         steady_monthly_revenue=400, curve="linear")
        assert _revenue_at_month(m, 10) == pytest.approx(400.0)

    def test_s_curve_low_at_start(self):
        m = ChannelModel("test", "content", 10, ramp_months=6,
                         steady_monthly_revenue=400, curve="s_curve")
        rev_m1 = _revenue_at_month(m, 1)
        assert rev_m1 < 100  # much less than steady

    def test_s_curve_near_steady_at_end(self):
        m = ChannelModel("test", "content", 10, ramp_months=6,
                         steady_monthly_revenue=400, curve="s_curve")
        rev_m12 = _revenue_at_month(m, 12)
        assert rev_m12 > 380  # near steady state

    def test_s_curve_inflection_around_midpoint(self):
        m = ChannelModel("test", "content", 10, ramp_months=6,
                         steady_monthly_revenue=400, curve="s_curve")
        rev_m3 = _revenue_at_month(m, 3)
        rev_m4 = _revenue_at_month(m, 4)
        # Growth should be fastest around midpoint
        assert rev_m4 - rev_m3 > 0

    def test_step_zero_before_ramp(self):
        m = ChannelModel("test", "products", 10, ramp_months=3,
                         steady_monthly_revenue=300, curve="step")
        assert _revenue_at_month(m, 1) == 0.0
        assert _revenue_at_month(m, 2) == 0.0

    def test_step_launches_at_ramp(self):
        m = ChannelModel("test", "products", 10, ramp_months=3,
                         steady_monthly_revenue=300, curve="step")
        rev = _revenue_at_month(m, 3)
        assert rev == pytest.approx(300.0)

    def test_step_grows_after_launch(self):
        m = ChannelModel("test", "products", 10, ramp_months=3,
                         steady_monthly_revenue=300, curve="step")
        rev_m3 = _revenue_at_month(m, 3)
        rev_m6 = _revenue_at_month(m, 6)
        assert rev_m6 > rev_m3  # 10% MoM growth

    def test_sporadic_lower_than_steady(self):
        m = ChannelModel("test", "trading", 10, ramp_months=2,
                         steady_monthly_revenue=200, curve="sporadic")
        rev = _revenue_at_month(m, 6)
        assert rev < 200  # 0.7x multiplier
        assert rev == pytest.approx(200 * 0.7)

    def test_sporadic_ramps_in(self):
        m = ChannelModel("test", "trading", 10, ramp_months=2,
                         steady_monthly_revenue=200, curve="sporadic")
        rev_m1 = _revenue_at_month(m, 1)
        rev_m2 = _revenue_at_month(m, 2)
        assert rev_m1 < rev_m2

    def test_unknown_curve_returns_zero(self):
        m = ChannelModel("test", "x", 10, ramp_months=2,
                         steady_monthly_revenue=200, curve="unknown")
        assert _revenue_at_month(m, 5) == 0.0

    def test_negative_month_returns_zero(self):
        m = ChannelModel("test", "services", 10, ramp_months=2,
                         steady_monthly_revenue=200, curve="linear")
        assert _revenue_at_month(m, -1) == 0.0


# ── GrowthProjector tests ───────────────────────────────────


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def simple_channels():
    """Two simple channels for predictable testing."""
    return [
        ChannelModel("svc_a", "services", monthly_cost=10.0,
                     ramp_months=2, steady_monthly_revenue=100.0, curve="linear"),
        ChannelModel("prod_b", "products", monthly_cost=5.0,
                     ramp_months=3, steady_monthly_revenue=200.0, curve="step"),
    ]


class TestGrowthProjector:
    def test_project_returns_correct_months(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        results = proj.project(months=6)
        assert len(results) == 6
        assert results[0].month == 1
        assert results[5].month == 6

    def test_month_1_revenue_only_from_linear(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        results = proj.project(months=1)
        # svc_a at month 1: 100 * (1/2) = 50, prod_b at month 1: 0 (step, ramp=3)
        assert results[0].revenue == pytest.approx(50.0)

    def test_expenses_constant(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        results = proj.project(months=6)
        for r in results:
            assert r.expenses == pytest.approx(15.0)  # 10 + 5

    def test_cumulative_net_accumulates(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        results = proj.project(months=4)
        assert results[0].cumulative_net == results[0].net
        assert results[1].cumulative_net == pytest.approx(
            results[0].net + results[1].net
        )

    def test_balance_includes_initial_capital(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        results = proj.project(months=1)
        assert results[0].balance == pytest.approx(500 + results[0].net)

    def test_channel_breakdown_present(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        results = proj.project(months=1)
        assert "svc_a" in results[0].channel_breakdown
        assert "prod_b" in results[0].channel_breakdown

    def test_category_breakdown_present(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        results = proj.project(months=1)
        assert "services" in results[0].category_breakdown
        assert "products" in results[0].category_breakdown

    def test_break_even_month_found(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        month = proj.get_break_even_month()
        # At some point revenue > 15 (expenses)
        assert month is not None
        assert month >= 1

    def test_break_even_impossible_returns_none(self, db):
        hopeless = [
            ChannelModel("x", "services", monthly_cost=1000,
                         ramp_months=24, steady_monthly_revenue=1, curve="linear"),
        ]
        proj = GrowthProjector(db, channels=hopeless)
        assert proj.get_break_even_month() is None

    def test_capital_recovery_month(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=100, channels=simple_channels)
        month = proj.get_capital_recovery_month()
        assert month is not None

    def test_summary_structure(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        summary = proj.get_summary(months=12)
        assert "initial_capital" in summary
        assert "break_even_month" in summary
        assert "capital_recovery_month" in summary
        assert "month_1" in summary
        assert "month_6" in summary
        assert "month_12" in summary
        assert "top_channels_month_12" in summary
        assert "annual_projected_revenue" in summary
        assert "annual_projected_profit" in summary

    def test_summary_revenue_positive_at_12(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        summary = proj.get_summary(months=12)
        assert summary["month_12"]["revenue"] > 0

    def test_format_report_returns_string(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        report = proj.format_report(months=6)
        assert isinstance(report, str)
        assert "monAI" in report
        assert "Break-even" in report

    def test_format_report_contains_all_months(self, db, simple_channels):
        proj = GrowthProjector(db, initial_capital=500, channels=simple_channels)
        report = proj.format_report(months=6)
        for m in range(1, 7):
            assert f"  {m:>5}" in report


# ── Full model sanity checks ────────────────────────────────


class TestFullModel:
    def test_all_channels_have_positive_steady_revenue(self):
        for ch in CHANNEL_MODELS:
            assert ch.steady_monthly_revenue > 0, f"{ch.name} has no revenue"

    def test_all_channels_have_positive_cost(self):
        for ch in CHANNEL_MODELS:
            assert ch.monthly_cost > 0, f"{ch.name} has no cost"

    def test_full_model_break_even_within_6_months(self, db):
        proj = GrowthProjector(db, initial_capital=500)
        month = proj.get_break_even_month()
        assert month is not None
        assert month <= 6, f"Break-even too late: month {month}"

    def test_full_model_profitable_by_month_12(self, db):
        proj = GrowthProjector(db, initial_capital=500)
        results = proj.project(months=12)
        assert results[-1].net > 0

    def test_full_model_capital_recovered(self, db):
        proj = GrowthProjector(db, initial_capital=500)
        month = proj.get_capital_recovery_month()
        assert month is not None
        assert month <= 12

    def test_total_monthly_cost_reasonable(self):
        total = sum(ch.monthly_cost for ch in CHANNEL_MODELS)
        assert total < 250, f"Monthly cost too high: €{total}"

    def test_channel_count_matches_agents(self):
        assert len(CHANNEL_MODELS) == 14

    def test_summary_annual_profit_positive(self, db):
        proj = GrowthProjector(db, initial_capital=500)
        summary = proj.get_summary(12)
        assert summary["annual_projected_profit"] > 0
