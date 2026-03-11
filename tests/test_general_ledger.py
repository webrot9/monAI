"""Tests for monai.business.finance.GeneralLedger — double-entry bookkeeping."""

import pytest

from monai.business.finance import AccountType, GeneralLedger


class TestGeneralLedger:
    @pytest.fixture
    def ledger(self, db):
        return GeneralLedger(db)

    def test_chart_of_accounts_seeded(self, ledger):
        coa = ledger.get_chart_of_accounts()
        assert len(coa) > 0
        codes = {a["code"] for a in coa}
        # Check key accounts exist
        assert "1000" in codes  # Cash - Operating
        assert "4000" in codes  # Revenue - Services
        assert "5000" in codes  # Expense - API Costs

    def test_add_custom_account(self, ledger):
        ledger.add_account("1999", "Test Asset", AccountType.ASSET)
        coa = ledger.get_chart_of_accounts()
        codes = {a["code"] for a in coa}
        assert "1999" in codes

    def test_record_balanced_entry(self, ledger):
        entry_id = ledger.record_entry(
            date="2026-03-11",
            description="Test revenue from Gumroad",
            lines=[
                {"account_code": "1020", "debit": 100.0, "currency": "EUR"},
                {"account_code": "4100", "credit": 100.0, "currency": "EUR"},
            ],
            reference="gumroad_sale_123",
            source="webhook",
            brand="test_brand",
        )
        assert entry_id > 0

    def test_reject_unbalanced_entry(self, ledger):
        with pytest.raises(ValueError, match="not balanced"):
            ledger.record_entry(
                date="2026-03-11",
                description="Bad entry",
                lines=[
                    {"account_code": "1020", "debit": 100.0},
                    {"account_code": "4100", "credit": 50.0},
                ],
            )

    def test_reject_empty_entry(self, ledger):
        with pytest.raises(ValueError, match="at least one line"):
            ledger.record_entry(
                date="2026-03-11",
                description="Empty",
                lines=[],
            )

    def test_reject_negative_amounts(self, ledger):
        with pytest.raises(ValueError, match="Negative"):
            ledger.record_entry(
                date="2026-03-11",
                description="Negative",
                lines=[
                    {"account_code": "1020", "debit": -50.0},
                    {"account_code": "4100", "credit": -50.0},
                ],
            )

    def test_reject_both_debit_and_credit(self, ledger):
        with pytest.raises(ValueError, match="both debit and credit"):
            ledger.record_entry(
                date="2026-03-11",
                description="Both sides",
                lines=[
                    {"account_code": "1020", "debit": 50.0, "credit": 50.0},
                ],
            )

    def test_account_balance_asset(self, ledger):
        # Debit cash (asset) — balance increases
        ledger.record_entry(
            date="2026-03-11",
            description="Receive payment",
            lines=[
                {"account_code": "1000", "debit": 200.0},
                {"account_code": "4000", "credit": 200.0},
            ],
        )
        assert ledger.get_account_balance("1000") == 200.0
        assert ledger.get_account_balance("4000") == 200.0

    def test_account_balance_expense(self, ledger):
        # Fund operating cash, then pay expense
        ledger.record_entry(
            date="2026-03-11",
            description="Seed capital",
            lines=[
                {"account_code": "1000", "debit": 500.0},
                {"account_code": "3000", "credit": 500.0},
            ],
        )
        ledger.record_entry(
            date="2026-03-11",
            description="API costs",
            lines=[
                {"account_code": "5000", "debit": 30.0},
                {"account_code": "1000", "credit": 30.0},
            ],
        )
        assert ledger.get_account_balance("1000") == 470.0
        assert ledger.get_account_balance("5000") == 30.0

    def test_trial_balance(self, ledger):
        ledger.record_entry(
            date="2026-03-11",
            description="Revenue",
            lines=[
                {"account_code": "1010", "debit": 150.0},
                {"account_code": "4100", "credit": 150.0},
            ],
        )
        tb = ledger.get_trial_balance()
        total_debit = sum(r["total_debit"] for r in tb)
        total_credit = sum(r["total_credit"] for r in tb)
        assert abs(total_debit - total_credit) < 0.01

    def test_balance_sheet(self, ledger):
        # Seed capital
        ledger.record_entry(
            date="2026-03-11",
            description="Seed capital",
            lines=[
                {"account_code": "1000", "debit": 500.0},
                {"account_code": "3000", "credit": 500.0},
            ],
        )
        # Revenue
        ledger.record_entry(
            date="2026-03-11",
            description="Sale",
            lines=[
                {"account_code": "1010", "debit": 100.0},
                {"account_code": "4100", "credit": 100.0},
            ],
        )
        bs = ledger.get_balance_sheet()
        assert bs["assets"] == 600.0
        assert bs["equity"] == 500.0
        assert bs["net_income"] == 100.0
        assert bs["balanced"]

    def test_income_statement(self, ledger):
        ledger.record_entry(
            date="2026-03-11",
            description="Revenue",
            lines=[
                {"account_code": "1010", "debit": 300.0},
                {"account_code": "4000", "credit": 300.0},
            ],
        )
        ledger.record_entry(
            date="2026-03-11",
            description="API costs",
            lines=[
                {"account_code": "5000", "debit": 50.0},
                {"account_code": "1000", "credit": 50.0},
            ],
        )
        income = ledger.get_income_statement("2026-03-01", "2026-03-31")
        assert income["total_revenue"] == 300.0
        assert income["total_expenses"] == 50.0
        assert income["net_income"] == 250.0

    def test_record_revenue_convenience(self, ledger):
        entry_id = ledger.record_revenue(
            amount=99.99,
            revenue_account="4100",
            cash_account="1020",
            description="Gumroad sale",
            brand="test_brand",
            reference="gum_123",
        )
        assert entry_id > 0
        assert ledger.get_account_balance("1020") == 99.99
        assert ledger.get_account_balance("4100") == 99.99

    def test_record_expense_convenience(self, ledger):
        # Seed first
        ledger.record_entry(
            date="2026-03-11",
            description="Seed",
            lines=[
                {"account_code": "1000", "debit": 500.0},
                {"account_code": "3000", "credit": 500.0},
            ],
        )
        entry_id = ledger.record_expense(
            amount=15.0,
            expense_account="5100",
            cash_account="1000",
            description="Hosting fee",
        )
        assert entry_id > 0
        assert ledger.get_account_balance("5100") == 15.0
        assert ledger.get_account_balance("1000") == 485.0

    def test_record_platform_fee(self, ledger):
        entry_id = ledger.record_platform_fee(
            gross=100.0,
            fee=3.50,
            revenue_account="4100",
            cash_account="1020",
            description="Gumroad sale with fee",
        )
        assert entry_id > 0
        assert ledger.get_account_balance("1020") == 96.50  # net
        assert ledger.get_account_balance("5200") == 3.50   # fee
        assert ledger.get_account_balance("4100") == 100.0  # gross revenue

    def test_record_sweep(self, ledger):
        # Seed cash
        ledger.record_entry(
            date="2026-03-11",
            description="Revenue",
            lines=[
                {"account_code": "1050", "debit": 200.0},
                {"account_code": "4000", "credit": 200.0},
            ],
        )
        entry_id = ledger.record_sweep(
            amount=200.0,
            from_account="1050",
            description="Sweep XMR to creator",
        )
        assert entry_id > 0
        assert ledger.get_account_balance("1050") == 0.0
        assert ledger.get_account_balance("2300") == -200.0  # debit reduces liability

    def test_verify_integrity_clean(self, ledger):
        ledger.record_entry(
            date="2026-03-11",
            description="Clean entry",
            lines=[
                {"account_code": "1000", "debit": 100.0},
                {"account_code": "3000", "credit": 100.0},
            ],
        )
        integrity = ledger.verify_integrity()
        assert integrity["balanced"]
        assert integrity["trial_balance_ok"]
        assert len(integrity["unbalanced_entries"]) == 0

    def test_journal_entries_query(self, ledger):
        ledger.record_entry(
            date="2026-03-11",
            description="Test entry",
            lines=[
                {"account_code": "1000", "debit": 50.0},
                {"account_code": "3000", "credit": 50.0},
            ],
            brand="test_brand",
        )
        entries = ledger.get_journal_entries(brand="test_brand")
        assert len(entries) == 1
        assert entries[0]["description"] == "Test entry"
        assert len(entries[0]["lines"]) == 2

    def test_reconciliation(self, ledger):
        entry_id = ledger.record_entry(
            date="2026-03-11",
            description="To reconcile",
            lines=[
                {"account_code": "1000", "debit": 100.0},
                {"account_code": "4000", "credit": 100.0},
            ],
            source="webhook",
        )
        unreconciled = ledger.get_unreconciled(source="webhook")
        assert len(unreconciled) == 1

        ledger.reconcile_entry(entry_id)
        unreconciled = ledger.get_unreconciled(source="webhook")
        assert len(unreconciled) == 0

    def test_multiple_entries_balance(self, ledger):
        """Multiple entries must keep the books balanced."""
        # Seed
        ledger.record_entry(
            date="2026-03-01",
            description="Seed capital",
            lines=[
                {"account_code": "1000", "debit": 500.0},
                {"account_code": "3000", "credit": 500.0},
            ],
        )
        # Revenue
        ledger.record_revenue(100.0, "4000", "1010", "Stripe sale")
        # Expense
        ledger.record_expense(25.0, "5000", "1000", "API costs")
        # Platform fee revenue
        ledger.record_platform_fee(50.0, 2.5, "4100", "1020", "Gumroad sale")
        # Sweep
        ledger.record_sweep(50.0, "1010", "Sweep to creator")

        integrity = ledger.verify_integrity()
        assert integrity["balanced"]
        assert integrity["trial_balance_ok"]

        bs = ledger.get_balance_sheet()
        assert bs["balanced"]
