"""Tests for monai.business.finance."""

import pytest

from monai.business.finance import Finance


class TestFinance:
    @pytest.fixture
    def finance(self, db):
        return Finance(db)

    def _add_strategy(self, db, name="test", category="freelance"):
        return db.execute_insert(
            "INSERT INTO strategies (name, category) VALUES (?, ?)", (name, category)
        )

    def _add_transaction(self, db, tx_type, amount, strategy_id=None, category="misc"):
        db.execute_insert(
            "INSERT INTO transactions (strategy_id, type, category, amount) VALUES (?, ?, ?, ?)",
            (strategy_id, tx_type, category, amount),
        )

    def test_zero_initial(self, finance):
        assert finance.get_total_revenue() == 0.0
        assert finance.get_total_expenses() == 0.0
        assert finance.get_net_profit() == 0.0

    def test_total_revenue(self, finance, db):
        self._add_transaction(db, "revenue", 100.0)
        self._add_transaction(db, "revenue", 50.0)
        assert finance.get_total_revenue() == 150.0

    def test_total_expenses(self, finance, db):
        self._add_transaction(db, "expense", 30.0)
        self._add_transaction(db, "expense", 20.0)
        assert finance.get_total_expenses() == 50.0

    def test_net_profit(self, finance, db):
        self._add_transaction(db, "revenue", 200.0)
        self._add_transaction(db, "expense", 75.0)
        assert finance.get_net_profit() == 125.0

    def test_revenue_by_strategy(self, finance, db):
        sid1 = self._add_strategy(db, "writing", "freelance")
        sid2 = self._add_strategy(db, "trading", "finance")
        self._add_transaction(db, "revenue", 100.0, strategy_id=sid1)
        self._add_transaction(db, "revenue", 200.0, strategy_id=sid2)

        assert finance.get_total_revenue(strategy_id=sid1) == 100.0
        assert finance.get_total_revenue(strategy_id=sid2) == 200.0

    def test_strategy_pnl(self, finance, db):
        sid = self._add_strategy(db, "writing", "freelance")
        self._add_transaction(db, "revenue", 500.0, strategy_id=sid)
        self._add_transaction(db, "expense", 100.0, strategy_id=sid)

        pnl = finance.get_strategy_pnl()
        assert len(pnl) == 1
        assert pnl[0]["name"] == "writing"
        assert pnl[0]["revenue"] == 500.0
        assert pnl[0]["expenses"] == 100.0
        assert pnl[0]["net"] == 400.0

    def test_roi_zero_expenses(self, finance):
        assert finance.get_roi() == 0.0

    def test_roi_calculation(self, finance, db):
        self._add_transaction(db, "expense", 100.0)
        self._add_transaction(db, "revenue", 250.0)
        assert finance.get_roi() == 2.5

    def test_daily_summary(self, finance, db):
        self._add_transaction(db, "revenue", 100.0)
        self._add_transaction(db, "expense", 30.0)
        summary = finance.get_daily_summary()
        assert summary["revenue"] == 100.0
        assert summary["expense"] == 30.0
        assert summary["net"] == 70.0
