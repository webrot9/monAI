"""Tests for monai.agents.memory."""

import json

import pytest

from monai.agents.memory import SharedMemory


class TestSharedMemoryKnowledge:
    @pytest.fixture
    def memory(self, db):
        return SharedMemory(db)

    def test_store_and_query_knowledge(self, memory):
        kid = memory.store_knowledge(
            category="discovery",
            topic="upwork pricing",
            content="Average blog post rate: $50-150",
            source_agent="researcher",
            tags=["pricing", "freelance"],
        )
        assert kid >= 1

        results = memory.query_knowledge(topic="upwork")
        assert len(results) == 1
        assert results[0]["content"] == "Average blog post rate: $50-150"

    def test_query_by_category(self, memory):
        memory.store_knowledge("fact", "market size", "Big", "agent1")
        memory.store_knowledge("warning", "scam alert", "Be careful", "agent2")

        facts = memory.query_knowledge(category="fact")
        assert len(facts) == 1
        assert facts[0]["topic"] == "market size"

    def test_query_by_tags(self, memory):
        memory.store_knowledge("fact", "t1", "c1", "a1", tags=["python", "code"])
        memory.store_knowledge("fact", "t2", "c2", "a2", tags=["marketing"])

        results = memory.query_knowledge(tags=["python"])
        assert len(results) == 1
        assert results[0]["topic"] == "t1"

    def test_mark_knowledge_used(self, memory):
        kid = memory.store_knowledge("fact", "topic", "content", "agent")
        memory.mark_knowledge_used(kid)
        memory.mark_knowledge_used(kid)

        results = memory.query_knowledge(topic="topic")
        assert results[0]["referenced_by"] == 2

    def test_knowledge_summary(self, memory):
        memory.store_knowledge("fact", "t1", "c1", "a1")
        memory.store_knowledge("fact", "t2", "c2", "a2")
        memory.store_knowledge("warning", "t3", "c3", "a3")

        summary = memory.get_knowledge_summary()
        assert summary["fact"] == 2
        assert summary["warning"] == 1


class TestSharedMemoryMessaging:
    @pytest.fixture
    def memory(self, db):
        return SharedMemory(db)

    def test_send_and_receive_message(self, memory):
        mid = memory.send_message("agent_a", "agent_b", "request", "Help needed", "Can you help?")
        assert mid >= 1

        msgs = memory.get_messages("agent_b")
        assert len(msgs) == 1
        assert msgs[0]["from_agent"] == "agent_a"
        assert msgs[0]["subject"] == "Help needed"
        assert msgs[0]["status"] == "unread"

    def test_mark_message_read(self, memory):
        mid = memory.send_message("a", "b", "info", "Test", "Body")
        memory.mark_message_read(mid)

        unread = memory.get_messages("b", unread_only=True)
        assert len(unread) == 0

        all_msgs = memory.get_messages("b", unread_only=False)
        assert len(all_msgs) == 1
        assert all_msgs[0]["status"] == "read"

    def test_broadcast(self, memory):
        memory.broadcast("orchestrator", "alert", "System Update", "New cycle starting")

        # All agents should see broadcast
        msgs_a = memory.get_messages("agent_a")
        msgs_b = memory.get_messages("agent_b")
        assert len(msgs_a) == 1
        assert len(msgs_b) == 1
        assert msgs_a[0]["to_agent"] == "all"

    def test_message_priority(self, memory):
        memory.send_message("a", "b", "info", "Low", "low priority", priority=10)
        memory.send_message("a", "b", "alert", "High", "high priority", priority=1)

        msgs = memory.get_messages("b")
        assert msgs[0]["priority"] == 1  # Higher priority first

    def test_thread(self, memory):
        parent_id = memory.send_message("a", "b", "request", "Help", "Need help")
        memory.send_message("b", "a", "response", "Re: Help", "Here's help", parent_id=parent_id)

        thread = memory.get_thread(parent_id)
        assert len(thread) == 2
        assert thread[0]["subject"] == "Help"
        assert thread[1]["subject"] == "Re: Help"


class TestSharedMemoryLessons:
    @pytest.fixture
    def memory(self, db):
        return SharedMemory(db)

    def test_record_and_get_lessons(self, memory):
        lid = memory.record_lesson(
            "researcher", "mistake", "Used wrong API",
            "Always check API version", "Verify API version before calling",
            severity="high",
        )
        assert lid >= 1

        lessons = memory.get_lessons()
        assert len(lessons) >= 1
        assert lessons[0]["lesson"] == "Always check API version"

    def test_lessons_shared_across_agents(self, memory):
        memory.record_lesson("agent_a", "discovery", "Situation A", "Lesson A")
        memory.record_lesson("agent_b", "discovery", "Situation B", "Lesson B")

        # Both agents should see all lessons when include_shared=True
        all_lessons = memory.get_lessons(agent_name="agent_a", include_shared=True)
        assert len(all_lessons) >= 2

    def test_private_lessons(self, memory):
        memory.record_lesson("agent_a", "mistake", "S", "L")
        memory.record_lesson("agent_b", "mistake", "S", "L")

        private = memory.get_lessons(agent_name="agent_a", include_shared=False)
        assert all(l["agent_name"] == "agent_a" for l in private)

    def test_get_rules_for_agent(self, memory):
        memory.record_lesson("a", "rule", "S", "L", rule="Always check budget")
        memory.record_lesson("b", "rule", "S", "L", rule="Never skip tests")

        rules = memory.get_rules_for_agent("a")
        assert "Always check budget" in rules
        assert "Never skip tests" in rules  # All rules visible to all agents

    def test_apply_lesson(self, memory):
        lid = memory.record_lesson("a", "tip", "S", "L", rule="rule")
        memory.apply_lesson(lid)
        memory.apply_lesson(lid)

        lessons = memory.get_lessons()
        matched = [l for l in lessons if l["id"] == lid]
        assert matched[0]["times_applied"] == 2


class TestSharedMemoryJournal:
    @pytest.fixture
    def memory(self, db):
        return SharedMemory(db)

    def test_journal_entry(self, memory):
        eid = memory.journal_entry(
            "orchestrator", "plan", "Planned next actions",
            details={"steps": ["a", "b"]}, outcome="success", cycle=1,
        )
        assert eid >= 1

    def test_get_journal(self, memory):
        memory.journal_entry("a", "execute", "Did X")
        memory.journal_entry("b", "execute", "Did Y")

        entries = memory.get_journal(agent_name="a")
        assert len(entries) == 1
        assert entries[0]["summary"] == "Did X"

    def test_get_recent_activity(self, memory):
        memory.journal_entry("a", "plan", "Step 1")
        memory.journal_entry("b", "execute", "Step 2")

        recent = memory.get_recent_activity(limit=10)
        assert len(recent) == 2
