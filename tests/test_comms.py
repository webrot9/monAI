"""Tests for monai.business.comms."""

import pytest

from monai.business.comms import CommsEngine


class TestCommsEngine:
    @pytest.fixture
    def comms(self, config, db):
        return CommsEngine(config, db)

    def test_send_email_without_smtp_logs_draft(self, comms, db):
        result = comms.send_email(
            to_email="client@example.com",
            subject="Proposal",
            body="Here's my proposal...",
        )
        assert result is False  # No SMTP configured

        rows = db.execute("SELECT * FROM messages WHERE subject = 'Proposal'")
        assert len(rows) == 1
        assert rows[0]["status"] == "draft"
        assert rows[0]["direction"] == "outbound"
        assert rows[0]["channel"] == "email"

    def test_log_platform_message(self, comms, db):
        cid = db.execute_insert(
            "INSERT INTO contacts (name, stage) VALUES (?, ?)", ("Test", "lead")
        )
        comms.log_platform_message(
            contact_id=cid,
            channel="upwork",
            body="Sent a proposal on Upwork",
            subject="Proposal",
        )
        rows = db.execute("SELECT * FROM messages WHERE contact_id = ?", (cid,))
        assert len(rows) == 1
        assert rows[0]["channel"] == "upwork"

    def test_get_conversation(self, comms, db):
        cid = db.execute_insert(
            "INSERT INTO contacts (name, stage) VALUES (?, ?)", ("Client", "client")
        )
        comms.log_platform_message(cid, "email", "Hello", subject="Hi")
        comms.log_platform_message(cid, "email", "Thanks", direction="inbound", subject="Re: Hi")

        convo = comms.get_conversation(cid)
        assert len(convo) == 2

    def test_get_unread_inbound(self, comms, db):
        cid = db.execute_insert(
            "INSERT INTO contacts (name, stage) VALUES (?, ?)", ("X", "lead")
        )
        # Inbound message is logged as 'sent' status by log_platform_message
        db.execute_insert(
            "INSERT INTO messages (contact_id, direction, channel, body, status) "
            "VALUES (?, 'inbound', 'email', 'New inquiry', 'unread')",
            (cid,),
        )
        unread = comms.get_unread_inbound()
        assert len(unread) == 1
        assert unread[0]["body"] == "New inquiry"
