"""Tests for monai.business.payments."""

import pytest

from monai.business.payments import PaymentManager, TRANSFER_METHODS


class TestPaymentManager:
    @pytest.fixture
    def pm(self, config, db):
        return PaymentManager(config, db)

    def test_add_receiving_account(self, pm):
        aid = pm.add_receiving_account("stripe", "acct_123")
        assert aid >= 1

    def test_get_receiving_accounts(self, pm):
        pm.add_receiving_account("stripe", "acct_1")
        pm.add_receiving_account("paypal", "agent@business.com")
        accounts = pm.get_receiving_accounts()
        assert len(accounts) == 2
        providers = {a["provider"] for a in accounts}
        assert "stripe" in providers
        assert "paypal" in providers

    def test_add_transfer_account(self, pm):
        aid = pm.add_transfer_account("crypto_btc", "bc1q...", metadata={"network": "mainnet"})
        assert aid >= 1

    def test_record_profit_transfer(self, pm, db):
        tid = pm.record_profit_transfer(100.0, "crypto_btc", tx_reference="tx_abc123")
        assert tid >= 1

        # Should also create a transaction in the main ledger
        rows = db.execute("SELECT * FROM transactions WHERE category = 'profit_transfer'")
        assert len(rows) == 1
        assert rows[0]["amount"] == 100.0

    def test_mark_transfer_completed(self, pm):
        tid = pm.record_profit_transfer(50.0, "crypto_monero")
        pm.mark_transfer_completed(tid, tx_reference="xmr_hash_abc")

        history = pm.get_transfer_history()
        completed = [t for t in history if t["id"] == tid]
        assert completed[0]["status"] == "completed"
        assert completed[0]["tx_reference"] == "xmr_hash_abc"

    def test_get_total_transferred(self, pm):
        tid1 = pm.record_profit_transfer(100.0, "crypto_btc")
        pm.mark_transfer_completed(tid1)
        tid2 = pm.record_profit_transfer(50.0, "crypto_btc")
        pm.mark_transfer_completed(tid2)
        pm.record_profit_transfer(200.0, "crypto_btc")  # pending — not counted

        assert pm.get_total_transferred() == 150.0

    def test_get_transferable_balance(self, pm, db):
        # Add some revenue
        db.execute_insert(
            "INSERT INTO transactions (type, category, amount) VALUES ('revenue', 'client_payment', ?)",
            (500.0,),
        )
        db.execute_insert(
            "INSERT INTO transactions (type, category, amount) VALUES ('expense', 'api_cost', ?)",
            (50.0,),
        )
        # Net profit = 450, nothing transferred yet
        assert pm.get_transferable_balance() == 450.0

        # Transfer 200
        tid = pm.record_profit_transfer(200.0, "crypto_btc")
        pm.mark_transfer_completed(tid)
        # The profit_transfer is also an expense, so net profit is now 250
        # But already transferred 200
        assert pm.get_transferable_balance() == 50.0

    def test_get_available_methods(self, pm):
        methods = pm.get_available_methods()
        assert len(methods) >= 3
        # Monero should be ranked highest in anonymity
        assert methods[0]["method"] == "crypto_monero"
        assert methods[0]["anonymity"] == "high"


class TestTransferMethods:
    def test_all_methods_have_required_fields(self):
        for method in TRANSFER_METHODS:
            assert "method" in method
            assert "name" in method
            assert "anonymity" in method
            assert "description" in method
            assert "requires" in method
