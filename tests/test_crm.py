"""Tests for monai.business.crm."""

import pytest

from monai.business.crm import CRM, STAGES


class TestCRM:
    @pytest.fixture
    def crm(self, db):
        return CRM(db)

    def test_add_contact(self, crm):
        cid = crm.add_contact("Alice", email="alice@example.com", platform="upwork")
        assert cid >= 1

    def test_get_contact(self, crm):
        cid = crm.add_contact("Bob", email="bob@test.com", company="BobCorp")
        contact = crm.get_contact(cid)
        assert contact is not None
        assert contact["name"] == "Bob"
        assert contact["email"] == "bob@test.com"
        assert contact["company"] == "BobCorp"
        assert contact["stage"] == "lead"

    def test_get_nonexistent_contact(self, crm):
        assert crm.get_contact(9999) is None

    def test_update_stage(self, crm):
        cid = crm.add_contact("Carol")
        crm.update_stage(cid, "prospect")
        contact = crm.get_contact(cid)
        assert contact["stage"] == "prospect"

    def test_update_stage_invalid(self, crm):
        cid = crm.add_contact("Dave")
        with pytest.raises(ValueError, match="Invalid stage"):
            crm.update_stage(cid, "invalid_stage")

    def test_get_contacts_by_stage(self, crm):
        crm.add_contact("Lead1")
        crm.add_contact("Lead2")
        cid3 = crm.add_contact("Client1")
        crm.update_stage(cid3, "client")

        leads = crm.get_contacts_by_stage("lead")
        assert len(leads) == 2

        clients = crm.get_contacts_by_stage("client")
        assert len(clients) == 1
        assert clients[0]["name"] == "Client1"

    def test_search_contacts(self, crm):
        crm.add_contact("Alice Smith", email="alice@acme.com", company="ACME")
        crm.add_contact("Bob Jones", email="bob@other.com", company="Other")

        results = crm.search_contacts("alice")
        assert len(results) == 1
        assert results[0]["name"] == "Alice Smith"

        results = crm.search_contacts("ACME")
        assert len(results) == 1

    def test_pipeline_summary(self, crm):
        crm.add_contact("A")
        crm.add_contact("B")
        cid = crm.add_contact("C")
        crm.update_stage(cid, "client")

        summary = crm.get_pipeline_summary()
        assert summary.get("lead", 0) == 2
        assert summary.get("client", 0) == 1

    def test_all_stages_valid(self):
        assert "lead" in STAGES
        assert "prospect" in STAGES
        assert "contacted" in STAGES
        assert "negotiating" in STAGES
        assert "client" in STAGES
        assert "churned" in STAGES
