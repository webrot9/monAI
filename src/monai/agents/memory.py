"""Shared memory and knowledge base for agent collaboration.

Three layers:
1. Agent Memory — each agent's private working memory (what it did, what it learned)
2. Shared Knowledge — facts, discoveries, and insights visible to all agents
3. Lessons Learned — mistakes and rules, per-agent and collective

Agents read from shared memory to coordinate, avoid duplicate work,
and build on each other's discoveries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

MEMORY_SCHEMA = """
-- Shared knowledge base accessible by all agents
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,      -- fact, discovery, insight, warning, opportunity, contact_intel
    topic TEXT NOT NULL,          -- short topic identifier
    content TEXT NOT NULL,        -- the actual knowledge
    source_agent TEXT NOT NULL,   -- which agent contributed this
    confidence REAL DEFAULT 1.0, -- 0.0-1.0 how confident
    tags TEXT,                   -- JSON array of tags for search
    referenced_by INTEGER DEFAULT 0,  -- how many times other agents used this
    expires_at TIMESTAMP,        -- optional expiry for time-sensitive info
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Inter-agent messages for collaboration
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,       -- target agent name, or 'all' for broadcast
    msg_type TEXT NOT NULL,       -- request, response, info, alert, handoff, question
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    priority INTEGER DEFAULT 5,  -- 1=critical, 5=normal, 10=low
    status TEXT DEFAULT 'unread', -- unread, read, acted_on, archived
    parent_id INTEGER,           -- for threaded conversations
    metadata TEXT,               -- JSON extra data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_id) REFERENCES agent_messages(id)
);

-- Lessons learned — both per-agent and shared
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,    -- which agent learned this, or 'shared' for collective
    category TEXT NOT NULL,      -- mistake, optimization, discovery, rule, pattern
    situation TEXT NOT NULL,     -- what happened
    lesson TEXT NOT NULL,        -- what was learned
    rule TEXT,                   -- concrete rule to follow going forward
    severity TEXT DEFAULT 'medium', -- low, medium, high, critical
    times_applied INTEGER DEFAULT 0, -- how many times this lesson was used
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Agent activity journal — detailed log of what each agent did and why
CREATE TABLE IF NOT EXISTS agent_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    cycle INTEGER,              -- which orchestration cycle
    action_type TEXT NOT NULL,  -- plan, execute, decide, collaborate, learn, error
    summary TEXT NOT NULL,      -- brief description
    details TEXT,               -- full details (JSON)
    outcome TEXT,               -- what happened as a result
    duration_ms INTEGER,        -- how long it took
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class SharedMemory:
    """Shared knowledge base that all agents can read from and write to."""

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(MEMORY_SCHEMA)

    # ── Knowledge Base ──────────────────────────────────────────

    def store_knowledge(self, category: str, topic: str, content: str,
                        source_agent: str, confidence: float = 1.0,
                        tags: list[str] | None = None,
                        expires_at: str | None = None) -> int:
        return self.db.execute_insert(
            "INSERT INTO knowledge (category, topic, content, source_agent, "
            "confidence, tags, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (category, topic, content, source_agent, confidence,
             json.dumps(tags or []), expires_at),
        )

    def query_knowledge(self, topic: str = "", category: str = "",
                        tags: list[str] | None = None,
                        limit: int = 20) -> list[dict[str, Any]]:
        """Search the shared knowledge base."""
        query = "SELECT * FROM knowledge WHERE 1=1"
        params: list = []

        if topic:
            query += " AND topic LIKE ?"
            params.append(f"%{topic}%")
        if category:
            query += " AND category = ?"
            params.append(category)
        if tags:
            for tag in tags:
                query += " AND tags LIKE ?"
                params.append(f"%{tag}%")

        # Exclude expired
        query += " AND (expires_at IS NULL OR expires_at > ?)"
        params.append(datetime.now().isoformat())

        query += " ORDER BY confidence DESC, referenced_by DESC LIMIT ?"
        params.append(limit)

        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def mark_knowledge_used(self, knowledge_id: int):
        """Track when an agent references a piece of knowledge."""
        self.db.execute(
            "UPDATE knowledge SET referenced_by = referenced_by + 1 WHERE id = ?",
            (knowledge_id,),
        )

    def get_knowledge_summary(self) -> dict[str, int]:
        """Get counts by category."""
        rows = self.db.execute(
            "SELECT category, COUNT(*) as count FROM knowledge GROUP BY category"
        )
        return {r["category"]: r["count"] for r in rows}

    # ── Inter-Agent Messaging ───────────────────────────────────

    def send_message(self, from_agent: str, to_agent: str, msg_type: str,
                     subject: str, body: str, priority: int = 5,
                     parent_id: int | None = None,
                     metadata: dict | None = None) -> int:
        return self.db.execute_insert(
            "INSERT INTO agent_messages (from_agent, to_agent, msg_type, subject, "
            "body, priority, parent_id, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (from_agent, to_agent, msg_type, subject, body, priority, parent_id,
             json.dumps(metadata) if metadata else None),
        )

    def get_messages(self, agent_name: str, unread_only: bool = True,
                     limit: int = 50) -> list[dict[str, Any]]:
        """Get messages for an agent (direct + broadcasts)."""
        status_filter = "AND status = 'unread'" if unread_only else ""
        rows = self.db.execute(
            f"SELECT * FROM agent_messages "
            f"WHERE (to_agent = ? OR to_agent = 'all') {status_filter} "
            f"ORDER BY priority ASC, created_at DESC LIMIT ?",
            (agent_name, limit),
        )
        return [dict(r) for r in rows]

    def mark_message_read(self, message_id: int):
        self.db.execute(
            "UPDATE agent_messages SET status = 'read' WHERE id = ?", (message_id,),
        )

    def mark_message_acted_on(self, message_id: int):
        self.db.execute(
            "UPDATE agent_messages SET status = 'acted_on' WHERE id = ?", (message_id,),
        )

    def broadcast(self, from_agent: str, msg_type: str, subject: str,
                  body: str, priority: int = 5) -> int:
        """Send a message to all agents."""
        return self.send_message(from_agent, "all", msg_type, subject, body, priority)

    def get_thread(self, message_id: int) -> list[dict[str, Any]]:
        """Get a full conversation thread."""
        # Get root
        root = self.db.execute("SELECT * FROM agent_messages WHERE id = ?", (message_id,))
        if not root:
            return []
        # Get replies
        replies = self.db.execute(
            "SELECT * FROM agent_messages WHERE parent_id = ? ORDER BY created_at ASC",
            (message_id,),
        )
        return [dict(root[0])] + [dict(r) for r in replies]

    # ── Lessons Learned ─────────────────────────────────────────

    def record_lesson(self, agent_name: str, category: str, situation: str,
                      lesson: str, rule: str = "", severity: str = "medium") -> int:
        """Record a lesson learned by an agent."""
        lesson_id = self.db.execute_insert(
            "INSERT INTO lessons (agent_name, category, situation, lesson, rule, severity) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent_name, category, situation, lesson, rule, severity),
        )
        # Broadcast the lesson so other agents know
        self.broadcast(
            agent_name, "info",
            f"Lesson learned: {category}",
            f"Situation: {situation}\nLesson: {lesson}\nRule: {rule}",
            priority=3,
        )
        logger.info(f"[{agent_name}] Lesson recorded: {lesson[:100]}")
        return lesson_id

    def get_lessons(self, agent_name: str | None = None,
                    category: str | None = None,
                    include_shared: bool = True) -> list[dict[str, Any]]:
        """Get lessons. By default returns ALL lessons (full collaboration).

        All agents learn from each other's mistakes and discoveries.
        """
        query = "SELECT * FROM lessons WHERE 1=1"
        params: list = []

        if agent_name and not include_shared:
            # Only this agent's private lessons
            query += " AND agent_name = ?"
            params.append(agent_name)
        # When include_shared=True (default), return ALL lessons from all agents

        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY severity DESC, times_applied DESC"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def apply_lesson(self, lesson_id: int):
        """Mark a lesson as applied (used to make a decision)."""
        self.db.execute(
            "UPDATE lessons SET times_applied = times_applied + 1 WHERE id = ?",
            (lesson_id,),
        )

    def get_rules_for_agent(self, agent_name: str) -> list[str]:
        """Get all active rules. Every agent sees every rule — full collaboration."""
        rows = self.db.execute(
            "SELECT DISTINCT rule FROM lessons WHERE rule != '' ORDER BY severity DESC",
        )
        return [r["rule"] for r in rows]

    # ── Agent Journal ───────────────────────────────────────────

    def journal_entry(self, agent_name: str, action_type: str, summary: str,
                      details: dict | None = None, outcome: str = "",
                      cycle: int | None = None, duration_ms: int | None = None) -> int:
        return self.db.execute_insert(
            "INSERT INTO agent_journal (agent_name, cycle, action_type, summary, "
            "details, outcome, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_name, cycle, action_type, summary,
             json.dumps(details) if details else None, outcome, duration_ms),
        )

    def get_journal(self, agent_name: str | None = None,
                    action_type: str | None = None,
                    limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM agent_journal WHERE 1=1"
        params: list = []
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if action_type:
            query += " AND action_type = ?"
            params.append(action_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def get_recent_activity(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent activity across all agents — for situational awareness."""
        rows = self.db.execute(
            "SELECT * FROM agent_journal ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
