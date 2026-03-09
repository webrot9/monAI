"""Task Router — intelligent dispatch of work to the right agent.

Routes tasks based on:
- Content analysis (what kind of task is this?)
- Agent capabilities (who can handle it?)
- Agent load (who's least busy?)
- Historical performance (who does this best?)
- Priority-based queuing
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

ROUTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_description TEXT NOT NULL,
    task_type TEXT,                           -- content, code, marketing, research, sales, design
    priority INTEGER DEFAULT 5,              -- 1=critical, 10=low
    routed_to TEXT,                          -- agent name
    routing_reason TEXT,
    status TEXT DEFAULT 'queued',             -- queued, routed, executing, completed, failed, dead_letter
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    routed_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    capability TEXT NOT NULL,                -- what this agent can do
    proficiency REAL DEFAULT 0.5,            -- 0-1 how good (updated from performance data)
    avg_duration_ms INTEGER,                 -- average task completion time
    success_rate REAL DEFAULT 0.5,           -- historical success rate
    tasks_completed INTEGER DEFAULT 0,
    UNIQUE(agent_name, capability)
);
"""

# Default agent → capability mapping
DEFAULT_CAPABILITIES: dict[str, list[str]] = {
    "freelance_writing": ["content", "copywriting", "blogging", "seo_content", "proposals"],
    "digital_products": ["ebooks", "templates", "guides", "digital_assets"],
    "content_sites": ["seo", "keywords", "articles", "affiliate_content"],
    "micro_saas": ["code", "api", "tools", "automation", "saas_mvp"],
    "telegram_bots": ["code", "bots", "telegram", "automation"],
    "affiliate": ["reviews", "comparisons", "affiliate_content", "product_research"],
    "newsletter": ["email", "content_curation", "subscriber_growth", "sponsorship"],
    "lead_gen": ["research", "data", "leads", "prospecting", "enrichment"],
    "social_media": ["social_posts", "engagement", "brand_management", "scheduling"],
    "course_creation": ["education", "curriculum", "lessons", "online_courses"],
    "domain_flipping": ["domains", "valuation", "marketplace", "trading"],
    "print_on_demand": ["design", "merchandise", "creative", "ecommerce"],
    "saas": ["code", "product_design", "market_research", "saas_full", "architecture"],
    "cold_outreach": ["sales", "outreach", "b2b", "lead_nurturing"],
    "finance_expert": ["finance", "investment", "roi_analysis", "forecasting", "budgeting"],
    "research_team": ["market_research", "trend_analysis", "competitor_analysis", "niche_discovery"],
    "marketing_team": ["marketing", "campaigns", "growth", "content_marketing", "outreach"],
}

# Task type → capabilities needed
TASK_TYPE_CAPABILITIES: dict[str, list[str]] = {
    "content": ["content", "copywriting", "blogging", "seo_content", "articles"],
    "code": ["code", "api", "tools", "automation", "saas_mvp", "saas_full"],
    "marketing": ["social_posts", "outreach", "engagement", "affiliate_content"],
    "research": ["research", "market_research", "keywords", "product_research"],
    "sales": ["sales", "outreach", "proposals", "lead_nurturing", "prospecting"],
    "design": ["design", "creative", "templates", "digital_assets"],
    "data": ["data", "leads", "enrichment", "research"],
    "education": ["education", "curriculum", "lessons"],
    "finance": ["finance", "investment", "roi_analysis", "forecasting", "budgeting"],
}


class TaskRouter:
    """Routes tasks to the most appropriate agent."""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm

        with db.connect() as conn:
            conn.executescript(ROUTER_SCHEMA)

        self._ensure_capabilities()

    def _ensure_capabilities(self):
        """Seed default capabilities if empty."""
        existing = self.db.execute("SELECT COUNT(*) as c FROM agent_capabilities")
        if existing[0]["c"] > 0:
            return

        for agent, caps in DEFAULT_CAPABILITIES.items():
            for cap in caps:
                self.db.execute_insert(
                    "INSERT OR IGNORE INTO agent_capabilities "
                    "(agent_name, capability, proficiency) VALUES (?, ?, 0.5)",
                    (agent, cap),
                )

    def route(self, task: str, task_type: str = "",
              priority: int = 5) -> dict[str, Any]:
        """Route a task to the best agent.

        Args:
            task: Natural language task description
            task_type: Optional hint (content, code, marketing, etc.)
            priority: 1=critical, 10=low

        Returns:
            Routing decision with agent name and reasoning
        """
        # Step 1: Classify the task if no type given
        if not task_type:
            task_type = self._classify_task(task)

        # Step 2: Find capable agents
        candidates = self._find_candidates(task_type)

        if not candidates:
            # Fallback: use LLM to pick
            candidates = self._llm_route(task, task_type)

        # Step 3: Rank by proficiency and success rate
        ranked = sorted(candidates, key=lambda c: (
            c.get("proficiency", 0) * 0.4 +
            c.get("success_rate", 0) * 0.4 +
            (1 - c.get("tasks_completed", 0) / max(c.get("total_tasks", 1), 1)) * 0.2  # Load balance
        ), reverse=True)

        if not ranked:
            return self._queue_unroutable(task, task_type, priority)

        best = ranked[0]
        reason = (
            f"Best match for task_type='{task_type}': "
            f"proficiency={best.get('proficiency', 0):.2f}, "
            f"success_rate={best.get('success_rate', 0):.2f}"
        )

        # Record routing
        task_id = self.db.execute_insert(
            "INSERT INTO task_queue (task_description, task_type, priority, "
            "routed_to, routing_reason, status, routed_at) "
            "VALUES (?, ?, ?, ?, ?, 'routed', CURRENT_TIMESTAMP)",
            (task[:500], task_type, priority, best["agent_name"], reason),
        )

        return {
            "task_id": task_id,
            "routed_to": best["agent_name"],
            "task_type": task_type,
            "priority": priority,
            "reason": reason,
            "candidates": len(ranked),
            "alternatives": [c["agent_name"] for c in ranked[1:4]],
        }

    def _classify_task(self, task: str) -> str:
        """Classify a task into a type using keyword matching (fast) or LLM (fallback)."""
        task_lower = task.lower()

        # Fast keyword matching
        keyword_scores: dict[str, int] = {}
        keywords = {
            "content": ["write", "article", "blog", "copy", "content", "text", "post"],
            "code": ["build", "code", "api", "deploy", "bug", "feature", "app", "software"],
            "marketing": ["promote", "market", "campaign", "advertise", "social", "brand"],
            "research": ["research", "analyze", "find", "discover", "investigate", "trend"],
            "sales": ["sell", "pitch", "proposal", "client", "deal", "close", "outreach"],
            "design": ["design", "logo", "visual", "creative", "graphic", "template"],
            "data": ["data", "scrape", "enrich", "leads", "list", "database"],
            "education": ["course", "teach", "lesson", "tutorial", "curriculum"],
        }

        for task_type, words in keywords.items():
            score = sum(1 for w in words if w in task_lower)
            if score > 0:
                keyword_scores[task_type] = score

        if keyword_scores:
            return max(keyword_scores, key=keyword_scores.get)

        return "content"  # Default fallback

    def _find_candidates(self, task_type: str) -> list[dict[str, Any]]:
        """Find agents capable of handling a task type."""
        needed_caps = TASK_TYPE_CAPABILITIES.get(task_type, [])
        if not needed_caps:
            return []

        placeholders = ",".join("?" * len(needed_caps))
        rows = self.db.execute(
            f"SELECT agent_name, AVG(proficiency) as proficiency, "
            f"AVG(success_rate) as success_rate, SUM(tasks_completed) as tasks_completed "
            f"FROM agent_capabilities WHERE capability IN ({placeholders}) "
            f"GROUP BY agent_name ORDER BY proficiency DESC",
            tuple(needed_caps),
        )
        return [dict(r) for r in rows]

    def _llm_route(self, task: str, task_type: str) -> list[dict[str, Any]]:
        """Use LLM as fallback router for ambiguous tasks."""
        agents = list(DEFAULT_CAPABILITIES.keys())
        response = self.llm.chat_json(
            [
                {"role": "system", "content": "You are a task router for an AI agent system."},
                {"role": "user", "content": (
                    f"Route this task to the best agent.\n"
                    f"Task: {task}\nType: {task_type}\n"
                    f"Available agents: {json.dumps(agents)}\n\n"
                    "Return: {\"agent\": str, \"confidence\": float}"
                )},
            ],
            temperature=0.1,
        )
        agent = response.get("agent", "")
        if agent in agents:
            return [{"agent_name": agent, "proficiency": response.get("confidence", 0.5),
                      "success_rate": 0.5, "tasks_completed": 0}]
        return []

    def _queue_unroutable(self, task: str, task_type: str,
                          priority: int) -> dict[str, Any]:
        """Queue a task that couldn't be routed."""
        task_id = self.db.execute_insert(
            "INSERT INTO task_queue (task_description, task_type, priority, status) "
            "VALUES (?, ?, ?, 'queued')",
            (task[:500], task_type, priority),
        )
        return {
            "task_id": task_id,
            "routed_to": None,
            "task_type": task_type,
            "reason": "No capable agent found — queued for manual routing",
        }

    def update_performance(self, agent_name: str, capability: str,
                           success: bool, duration_ms: int = 0):
        """Update agent performance metrics after task completion."""
        self.db.execute(
            "UPDATE agent_capabilities SET "
            "tasks_completed = tasks_completed + 1, "
            "success_rate = (success_rate * tasks_completed + ?) / (tasks_completed + 1), "
            "avg_duration_ms = COALESCE((avg_duration_ms * tasks_completed + ?) / (tasks_completed + 1), ?), "
            "proficiency = CASE WHEN ? = 1 "
            "  THEN MIN(1.0, proficiency + 0.01) "
            "  ELSE MAX(0.0, proficiency - 0.02) END "
            "WHERE agent_name = ? AND capability = ?",
            (1.0 if success else 0.0, duration_ms, duration_ms,
             1 if success else 0, agent_name, capability),
        )

    def get_queue(self, status: str = "queued") -> list[dict[str, Any]]:
        """Get tasks in the queue by status."""
        rows = self.db.execute(
            "SELECT * FROM task_queue WHERE status = ? ORDER BY priority, created_at",
            (status,),
        )
        return [dict(r) for r in rows]

    def get_agent_stats(self) -> list[dict[str, Any]]:
        """Get performance stats per agent."""
        rows = self.db.execute(
            "SELECT agent_name, COUNT(*) as capabilities, "
            "AVG(proficiency) as avg_proficiency, "
            "AVG(success_rate) as avg_success_rate, "
            "SUM(tasks_completed) as total_tasks "
            "FROM agent_capabilities GROUP BY agent_name "
            "ORDER BY avg_proficiency DESC"
        )
        return [dict(r) for r in rows]

    def get_routing_stats(self) -> dict[str, Any]:
        """Get routing statistics."""
        rows = self.db.execute(
            "SELECT routed_to, COUNT(*) as tasks, "
            "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed, "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed "
            "FROM task_queue WHERE routed_to IS NOT NULL GROUP BY routed_to"
        )
        return {r["routed_to"]: dict(r) for r in rows}
