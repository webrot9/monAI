"""Newsletter monetization strategy agent.

Builds email subscriber lists around specific niches, then monetizes via:
- Paid sponsorships
- Premium/paid tiers
- Affiliate links in content
- Driving traffic to other monAI properties

Compounds over time — subscribers are an owned asset.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

NEWSLETTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS newsletters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    niche TEXT NOT NULL,
    description TEXT,
    frequency TEXT DEFAULT 'weekly',        -- daily, weekly, biweekly, monthly
    subscriber_count INTEGER DEFAULT 0,
    platform TEXT DEFAULT 'substack',        -- substack, beehiiv, convertkit, ghost
    monetization TEXT,                       -- JSON: sponsors, premium, affiliate
    status TEXT DEFAULT 'planning',          -- planning, launched, growing, monetizing, paused
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS newsletter_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newsletter_id INTEGER REFERENCES newsletters(id),
    subject TEXT NOT NULL,
    content_path TEXT,                       -- path to the full content file
    status TEXT DEFAULT 'draft',             -- draft, reviewed, scheduled, sent
    open_rate REAL,
    click_rate REAL,
    sponsor_revenue REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class NewsletterAgent(BaseAgent):
    name = "newsletter"
    description = (
        "Builds and monetizes email newsletters. Picks profitable niches, "
        "creates consistent high-quality content, grows subscriber lists, "
        "and monetizes via sponsorships, premium tiers, and affiliate links."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(NEWSLETTER_SCHEMA)

    def plan(self) -> list[str]:
        newsletters = self.db.execute("SELECT * FROM newsletters WHERE status != 'paused'")
        count = len(newsletters)
        plan = self.think_json(
            f"I run {count} newsletters. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_niches, plan_newsletter, write_issue, "
            "grow_subscribers, find_sponsors, analyze_performance.",
        )
        return plan.get("steps", ["research_niches"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting newsletter cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_niches":
                results["niches"] = self._research_niches()
            elif step == "plan_newsletter":
                results["planned"] = self._plan_newsletter()
            elif step == "write_issue":
                results["issue"] = self._write_issue()
            elif step == "find_sponsors":
                results["sponsors"] = self._find_sponsors()
            elif step == "grow_subscribers":
                results["growth"] = self._grow_subscribers()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Find newsletter niches with monetization potential."""
        return self.think_json(
            "Research 5 newsletter niches. Requirements:\n"
            "- Audience willing to pay for info (professionals, hobbyists)\n"
            "- Sponsors exist in the space (B2B SaaS, tools, courses)\n"
            "- Can publish weekly with consistent value\n"
            "- Not oversaturated (avoid generic 'AI news')\n"
            "- Can hit 1,000 subscribers in 3 months with good content\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"audience\": str, "
            "\"content_angle\": str, \"monetization_path\": str, "
            "\"potential_sponsors\": [str], \"estimated_cpm\": float, "
            "\"platform\": str, \"competition_level\": str}]}"
        )

    def _plan_newsletter(self) -> dict[str, Any]:
        """Plan a new newsletter from scratch."""
        plan = self.think_json(
            "Design a newsletter to launch. Include:\n"
            "- Name, tagline, niche\n"
            "- Content format (curated links, original analysis, tutorials, etc.)\n"
            "- Publishing schedule\n"
            "- First 4 issue topics\n"
            "- Growth strategy for first 500 subscribers\n"
            "- Monetization timeline\n\n"
            "Return: {\"name\": str, \"tagline\": str, \"niche\": str, "
            "\"format\": str, \"frequency\": str, \"platform\": str, "
            "\"first_issues\": [str], \"growth_strategy\": str, "
            "\"monetization_timeline\": str}"
        )

        name = plan.get("name", "untitled")
        self.db.execute_insert(
            "INSERT OR IGNORE INTO newsletters (name, niche, description, frequency, platform, status) "
            "VALUES (?, ?, ?, ?, ?, 'planning')",
            (name, plan.get("niche", ""), plan.get("tagline", ""),
             plan.get("frequency", "weekly"), plan.get("platform", "substack")),
        )
        self.log_action("newsletter_planned", name)
        return plan

    def _write_issue(self) -> dict[str, Any]:
        """Write a newsletter issue for an active newsletter."""
        newsletters = self.db.execute(
            "SELECT * FROM newsletters WHERE status IN ('launched', 'growing', 'monetizing') LIMIT 1"
        )
        if not newsletters:
            return {"status": "no_active_newsletters"}

        nl = dict(newsletters[0])
        issue = self.think_json(
            f"Write a newsletter issue for: {nl['name']}\n"
            f"Niche: {nl['niche']}\n"
            f"Description: {nl['description']}\n\n"
            "Create a compelling issue with:\n"
            "- Attention-grabbing subject line\n"
            "- Opening hook (1-2 sentences)\n"
            "- 3-5 content sections with real value\n"
            "- Call to action\n\n"
            "Return: {\"subject\": str, \"hook\": str, "
            "\"sections\": [{\"heading\": str, \"content\": str}], "
            "\"cta\": str}"
        )

        self.db.execute_insert(
            "INSERT INTO newsletter_issues (newsletter_id, subject, status) VALUES (?, ?, 'draft')",
            (nl["id"], issue.get("subject", "Untitled")),
        )
        self.log_action("issue_written", issue.get("subject", ""))
        return issue

    def _find_sponsors(self) -> dict[str, Any]:
        """Research potential sponsors for newsletters."""
        newsletters = self.db.execute(
            "SELECT * FROM newsletters WHERE status IN ('growing', 'monetizing')"
        )
        if not newsletters:
            return {"status": "no_newsletters_ready_for_sponsors"}

        nl = dict(newsletters[0])
        return self.think_json(
            f"Find 5 potential sponsors for newsletter: {nl['name']}\n"
            f"Niche: {nl['niche']}, Subscribers: {nl['subscriber_count']}\n\n"
            "Look for:\n"
            "- SaaS tools the audience uses\n"
            "- Courses/educational products\n"
            "- Services relevant to the niche\n"
            "- Companies with existing newsletter sponsorship programs\n\n"
            "Return: {\"sponsors\": [{\"company\": str, \"product\": str, "
            "\"relevance\": str, \"estimated_cpm\": float, "
            "\"contact_method\": str}]}"
        )

    def _grow_subscribers(self) -> dict[str, Any]:
        """Plan subscriber growth tactics."""
        return self.think_json(
            "Generate 5 specific, actionable growth tactics for a newsletter. "
            "No generic advice. Specific platforms, communities, and methods.\n\n"
            "Return: {\"tactics\": [{\"tactic\": str, \"platform\": str, "
            "\"expected_subscribers\": int, \"effort\": str, \"cost\": float}]}"
        )
