"""Tests for monai.agents.collaboration — collaboration hub."""

import json

import pytest

from monai.agents.collaboration import CollaborationHub, SKILL_REGISTRY


class TestCollaborationHub:
    @pytest.fixture
    def hub(self, config, db):
        return CollaborationHub(config, db)

    # ── Schema ────────────────────────────────────────────────

    def test_schema_created(self, hub, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='help_requests'"
        )
        assert len(rows) == 1

    # ── Request Help ──────────────────────────────────────────

    def test_request_help(self, hub):
        rid = hub.request_help(
            "writer", "legal",
            "Review freelance writing legality in EU",
        )
        assert rid >= 1

    def test_request_with_context(self, hub):
        rid = hub.request_help(
            "writer", "design",
            "Create a logo for our business",
            context={"style": "modern", "colors": ["blue", "white"]},
        )
        req = hub.get_request(rid)
        assert req is not None
        assert json.loads(req["context"])["style"] == "modern"

    def test_request_with_target(self, hub):
        rid = hub.request_help(
            "writer", "code",
            "Build a web scraper",
            target_agent="coder",
        )
        req = hub.get_request(rid)
        assert req["target_agent"] == "coder"

    def test_request_priority(self, hub):
        hub.request_help("a", "legal", "Urgent legal question", priority=1)
        hub.request_help("b", "code", "Low priority task", priority=10)

        open_reqs = hub.get_open_requests()
        assert open_reqs[0]["priority"] == 1  # Urgent first
        assert open_reqs[1]["priority"] == 10

    # ── Claim & Fulfill ───────────────────────────────────────

    def test_claim_request(self, hub):
        rid = hub.request_help("writer", "legal", "Review this")
        assert hub.claim_request(rid, "legal_advisor") is True

        req = hub.get_request(rid)
        assert req["status"] == "claimed"
        assert req["claimed_by"] == "legal_advisor"

    def test_cannot_claim_already_claimed(self, hub):
        rid = hub.request_help("writer", "legal", "Review this")
        hub.claim_request(rid, "legal_advisor")
        assert hub.claim_request(rid, "another_agent") is False

    def test_start_work(self, hub):
        rid = hub.request_help("writer", "code", "Build tool")
        hub.claim_request(rid, "coder")
        hub.start_work(rid)

        req = hub.get_request(rid)
        assert req["status"] == "in_progress"

    def test_complete_request(self, hub):
        rid = hub.request_help("writer", "design", "Create logo")
        hub.claim_request(rid, "designer")
        hub.start_work(rid)
        hub.complete_request(rid, "Logo created: logo.png")

        req = hub.get_request(rid)
        assert req["status"] == "completed"
        assert "logo.png" in req["result"]
        assert req["completed_at"] is not None

    def test_fail_request(self, hub):
        rid = hub.request_help("writer", "devops", "Deploy server")
        hub.claim_request(rid, "devops_agent")
        hub.fail_request(rid, "Server unavailable")

        req = hub.get_request(rid)
        assert req["status"] == "failed"
        assert "Server unavailable" in req["result"]

    def test_rate_result(self, hub):
        rid = hub.request_help("writer", "content", "Proofread article")
        hub.claim_request(rid, "editor")
        hub.complete_request(rid, "Proofread complete")
        hub.rate_result(rid, 0.9)

        req = hub.get_request(rid)
        assert req["quality_score"] == 0.9

    def test_rate_clamped(self, hub):
        rid = hub.request_help("a", "code", "task")
        hub.complete_request(rid, "done")
        hub.rate_result(rid, 1.5)  # Over 1.0

        req = hub.get_request(rid)
        assert req["quality_score"] == 1.0

    # ── Queries ───────────────────────────────────────────────

    def test_get_open_requests(self, hub):
        hub.request_help("a", "legal", "Legal review 1")
        hub.request_help("b", "code", "Code task")
        rid = hub.request_help("c", "design", "Design task")
        hub.claim_request(rid, "designer")  # No longer open

        open_reqs = hub.get_open_requests()
        assert len(open_reqs) == 2

    def test_get_open_requests_by_skill(self, hub):
        hub.request_help("a", "legal", "Legal 1")
        hub.request_help("b", "code", "Code 1")
        hub.request_help("c", "legal", "Legal 2")

        legal = hub.get_open_requests(skill="legal")
        assert len(legal) == 2

    def test_get_open_requests_by_target(self, hub):
        hub.request_help("a", "code", "Task 1", target_agent="coder")
        hub.request_help("b", "code", "Task 2")  # No target
        hub.request_help("c", "code", "Task 3", target_agent="another")

        # Should get tasks targeted at coder OR with no target
        coder_tasks = hub.get_open_requests(target_agent="coder")
        assert len(coder_tasks) == 2

    def test_get_agent_requests(self, hub):
        hub.request_help("writer", "legal", "Legal 1")
        hub.request_help("writer", "code", "Code 1")
        hub.request_help("coder", "design", "Design 1")

        writer_reqs = hub.get_agent_requests("writer")
        assert len(writer_reqs) == 2

    def test_get_agent_requests_by_status(self, hub):
        rid = hub.request_help("writer", "legal", "Legal 1")
        hub.request_help("writer", "code", "Code 1")
        hub.claim_request(rid, "legal_advisor")
        hub.complete_request(rid, "Done")

        completed = hub.get_agent_requests("writer", status="completed")
        assert len(completed) == 1

    def test_get_agent_claims(self, hub):
        r1 = hub.request_help("a", "legal", "Task 1")
        r2 = hub.request_help("b", "legal", "Task 2")
        hub.claim_request(r1, "legal_advisor")
        hub.claim_request(r2, "legal_advisor")

        claims = hub.get_agent_claims("legal_advisor")
        assert len(claims) == 2

    def test_get_pending_legal_reviews(self, hub):
        hub.request_help("a", "legal", "Legal review")
        hub.request_help("b", "code", "Code task")

        legal = hub.get_pending_legal_reviews()
        assert len(legal) == 1
        assert legal[0]["skill_needed"] == "legal"

    def test_get_request_nonexistent(self, hub):
        assert hub.get_request(9999) is None

    # ── Statistics ────────────────────────────────────────────

    def test_stats_empty(self, hub):
        stats = hub.get_collaboration_stats()
        assert stats["total_requests"] == 0

    def test_stats_with_data(self, hub):
        r1 = hub.request_help("a", "legal", "Legal 1")
        r2 = hub.request_help("b", "code", "Code 1")
        hub.claim_request(r1, "legal_advisor")
        hub.complete_request(r1, "Done")
        hub.rate_result(r1, 0.95)

        stats = hub.get_collaboration_stats()
        assert stats["total_requests"] == 2
        assert stats["by_skill"]["legal"] == 1
        assert stats["by_skill"]["code"] == 1
        assert stats["avg_quality"] == 0.95

    # ── Skill Registry ────────────────────────────────────────

    def test_legal_auto_spawns(self, hub):
        assert hub.needs_legal_skill("legal") is True

    def test_other_skills_no_auto_spawn(self, hub):
        assert hub.needs_legal_skill("marketing") is False
        assert hub.needs_legal_skill("code") is False


class TestSkillRegistry:
    def test_all_skills_have_description(self):
        for skill, info in SKILL_REGISTRY.items():
            assert "description" in info, f"Skill {skill} missing description"
            assert len(info["description"]) > 0

    def test_legal_auto_spawn(self):
        assert SKILL_REGISTRY["legal"]["auto_spawn"] is True

    def test_minimum_skills(self):
        expected = ["legal", "marketing", "design", "code", "research", "finance", "content"]
        for s in expected:
            assert s in SKILL_REGISTRY, f"Missing skill: {s}"
