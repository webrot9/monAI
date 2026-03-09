"""Tests for EmailMarketing — per-brand email campaigns, sequences, tracking."""

import json

import pytest

from monai.business.email_marketing import EmailMarketing
from monai.db.database import Database
from tests.conftest_schema import TEST_SCHEMA


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    with d.connect() as conn:
        conn.executescript(TEST_SCHEMA)
    return d


@pytest.fixture
def em(db):
    return EmailMarketing(db)


# ── Schema ────────────────────────────────────────────────────


class TestSchema:
    def test_creates_tables(self, em, db):
        for table in ("email_subscribers", "email_campaigns",
                      "email_sequences", "email_sequence_steps", "email_sends"):
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            assert len(rows) == 1, f"Table {table} not created"


# ── Subscribers ───────────────────────────────────────────────


class TestSubscribers:
    def test_add_subscriber(self, em):
        sub_id = em.add_subscriber(
            "newsletter", "reader@example.com", name="Reader",
            source="website", tags=["early_adopter"],
        )
        assert sub_id > 0

    def test_add_duplicate_ignored(self, em):
        em.add_subscriber("newsletter", "reader@example.com")
        sub_id = em.add_subscriber("newsletter", "reader@example.com")
        # INSERT OR IGNORE returns 0 on duplicate
        subs = em.get_active_subscribers("newsletter")
        assert len(subs) == 1

    def test_brand_isolation(self, em):
        em.add_subscriber("newsletter", "reader@example.com")
        em.add_subscriber("micro_saas", "user@example.com")

        assert len(em.get_active_subscribers("newsletter")) == 1
        assert len(em.get_active_subscribers("micro_saas")) == 1

    def test_unsubscribe(self, em):
        em.add_subscriber("newsletter", "reader@example.com")
        result = em.unsubscribe("newsletter", "reader@example.com")

        assert result["status"] == "unsubscribed"
        assert len(em.get_active_subscribers("newsletter")) == 0

    def test_get_active_subscribers_with_tags(self, em):
        em.add_subscriber("newsletter", "a@test.com", tags=["vip", "early"])
        em.add_subscriber("newsletter", "b@test.com", tags=["free"])
        em.add_subscriber("newsletter", "c@test.com", tags=["vip"])

        vip = em.get_active_subscribers("newsletter", tags=["vip"])
        assert len(vip) == 2

    def test_subscriber_count(self, em):
        em.add_subscriber("newsletter", "a@test.com")
        em.add_subscriber("newsletter", "b@test.com")
        em.add_subscriber("newsletter", "c@test.com")
        em.unsubscribe("newsletter", "c@test.com")

        counts = em.get_subscriber_count("newsletter")
        assert counts["active"] == 2
        assert counts["unsubscribed"] == 1

    def test_tag_subscriber(self, em):
        em.add_subscriber("newsletter", "reader@example.com", tags=["early"])
        em.tag_subscriber("newsletter", "reader@example.com", ["vip", "beta"])

        subs = em.get_active_subscribers("newsletter")
        tags = json.loads(subs[0]["tags"])
        assert "early" in tags
        assert "vip" in tags
        assert "beta" in tags

    def test_tag_nonexistent_subscriber(self, em):
        # Should not raise
        em.tag_subscriber("newsletter", "nobody@test.com", ["vip"])


# ── Campaigns ─────────────────────────────────────────────────


class TestCampaigns:
    def test_create_campaign(self, em):
        cid = em.create_campaign(
            "newsletter", "March Update", "What's new in March",
            body_html="<h1>Hey!</h1>", from_name="MonAI Weekly",
            from_email="weekly@example.com",
        )
        assert cid > 0

    def test_schedule_campaign(self, em):
        cid = em.create_campaign(
            "newsletter", "March Update", "What's new in March",
        )
        result = em.schedule_campaign(cid, "2026-03-15T10:00:00")
        assert result["status"] == "scheduled"

    def test_queue_campaign_sends(self, em):
        em.add_subscriber("newsletter", "a@test.com")
        em.add_subscriber("newsletter", "b@test.com")
        em.add_subscriber("newsletter", "c@test.com")
        em.unsubscribe("newsletter", "c@test.com")

        cid = em.create_campaign(
            "newsletter", "March Update", "What's new in March",
        )
        queued = em.queue_campaign_sends(cid)
        assert queued == 2  # Only active subscribers

    def test_queue_campaign_with_segment(self, em):
        em.add_subscriber("newsletter", "a@test.com", tags=["vip"])
        em.add_subscriber("newsletter", "b@test.com", tags=["free"])

        cid = em.create_campaign(
            "newsletter", "VIP Only", "Exclusive content",
            segment_tags=["vip"],
        )
        queued = em.queue_campaign_sends(cid)
        assert queued == 1

    def test_queue_nonexistent_campaign(self, em):
        assert em.queue_campaign_sends(999) == 0


class TestSendTracking:
    def test_mark_send_sent(self, em):
        em.add_subscriber("newsletter", "a@test.com")
        cid = em.create_campaign("newsletter", "Test", "Test Subject")
        em.queue_campaign_sends(cid)

        stats = em.get_campaign_stats(cid)
        assert stats["total"] == 1

        # Get the send ID
        rows = em.db.execute(
            "SELECT id FROM email_sends WHERE campaign_id = ?", (cid,)
        )
        send_id = rows[0]["id"]
        em.mark_send_sent(send_id)

        stats = em.get_campaign_stats(cid)
        assert stats["sent"] >= 1

    def test_mark_send_opened(self, em):
        em.add_subscriber("newsletter", "a@test.com")
        cid = em.create_campaign("newsletter", "Test", "Test Subject")
        em.queue_campaign_sends(cid)

        rows = em.db.execute(
            "SELECT id FROM email_sends WHERE campaign_id = ?", (cid,)
        )
        send_id = rows[0]["id"]
        em.mark_send_sent(send_id)
        em.mark_send_opened(send_id)

        stats = em.get_campaign_stats(cid)
        assert stats["opened"] >= 1

    def test_mark_send_clicked(self, em):
        em.add_subscriber("newsletter", "a@test.com")
        cid = em.create_campaign("newsletter", "Test", "Test Subject")
        em.queue_campaign_sends(cid)

        rows = em.db.execute(
            "SELECT id FROM email_sends WHERE campaign_id = ?", (cid,)
        )
        send_id = rows[0]["id"]
        em.mark_send_clicked(send_id)

        stats = em.get_campaign_stats(cid)
        assert stats["clicked"] >= 1

    def test_campaign_stats_rates(self, em):
        for i in range(5):
            em.add_subscriber("newsletter", f"user{i}@test.com")

        cid = em.create_campaign("newsletter", "Test", "Test Subject")
        em.queue_campaign_sends(cid)

        rows = em.db.execute(
            "SELECT id FROM email_sends WHERE campaign_id = ?", (cid,)
        )
        # Mark 3 sent, 2 opened, 1 clicked
        for i, r in enumerate(rows):
            em.mark_send_sent(r["id"])
            if i < 2:
                em.mark_send_opened(r["id"])
            if i == 0:
                em.mark_send_clicked(r["id"])

        stats = em.get_campaign_stats(cid)
        assert stats["total"] == 5
        assert stats["open_rate"] > 0
        assert stats["click_rate"] > 0


# ── Sequences ─────────────────────────────────────────────────


class TestSequences:
    def test_create_sequence(self, em):
        seq_id = em.create_sequence("newsletter", "welcome_series", "subscribe")
        assert seq_id > 0

    def test_add_sequence_steps(self, em):
        seq_id = em.create_sequence("newsletter", "welcome_series")
        step1 = em.add_sequence_step(
            seq_id, 1, 0, "Welcome!",
            body_html="<h1>Thanks for joining</h1>",
        )
        step2 = em.add_sequence_step(
            seq_id, 2, 24, "Getting started",
            body_html="<h1>Here's how to start</h1>",
        )

        assert step1 > 0
        assert step2 > 0

    def test_get_sequence_steps(self, em):
        seq_id = em.create_sequence("newsletter", "welcome_series")
        em.add_sequence_step(seq_id, 1, 0, "Welcome!")
        em.add_sequence_step(seq_id, 2, 24, "Day 2")
        em.add_sequence_step(seq_id, 3, 72, "Day 4")

        steps = em.get_sequence_steps(seq_id)
        assert len(steps) == 3
        assert steps[0]["step_number"] == 1
        assert steps[1]["delay_hours"] == 24

    def test_get_pending_sequence_sends(self, em):
        sub_id = em.add_subscriber("newsletter", "reader@test.com")
        seq_id = em.create_sequence("newsletter", "welcome_series")
        step_id = em.add_sequence_step(seq_id, 1, 0, "Welcome!")

        # Manually create a queued sequence send
        em.db.execute_insert(
            "INSERT INTO email_sends "
            "(subscriber_id, sequence_step_id, subject, status) "
            "VALUES (?, ?, 'Welcome!', 'queued')",
            (sub_id, step_id),
        )

        pending = em.get_pending_sequence_sends("newsletter")
        assert len(pending) == 1
        assert pending[0]["email"] == "reader@test.com"


# ── Cross-Brand ───────────────────────────────────────────────


class TestCrossBrand:
    def test_all_brands_stats(self, em):
        em.add_subscriber("newsletter", "a@test.com")
        em.add_subscriber("newsletter", "b@test.com")
        em.add_subscriber("micro_saas", "c@test.com")

        em.create_campaign("newsletter", "Test", "Subject")

        stats = em.get_all_brands_stats()
        brands = {s["brand"] for s in stats}
        assert "newsletter" in brands
        assert "micro_saas" in brands
