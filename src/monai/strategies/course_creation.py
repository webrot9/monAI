"""Course creation strategy agent.

Creates and sells online courses on platforms like Udemy, Skillshare, Gumroad.
Passive income once published. Uses existing content capabilities.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

COURSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    niche TEXT NOT NULL,
    platform TEXT DEFAULT 'udemy',           -- udemy, skillshare, gumroad, teachable
    price REAL DEFAULT 0.0,
    description TEXT,
    target_audience TEXT,
    total_lessons INTEGER DEFAULT 0,
    total_duration_hours REAL DEFAULT 0.0,
    status TEXT DEFAULT 'planning',          -- planning, scripting, producing, published, paused
    enrollments INTEGER DEFAULT 0,
    rating REAL DEFAULT 0.0,
    revenue REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS course_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id),
    section TEXT NOT NULL,
    title TEXT NOT NULL,
    lesson_order INTEGER NOT NULL,
    script TEXT,                             -- full lesson script
    duration_minutes REAL DEFAULT 0.0,
    status TEXT DEFAULT 'draft',             -- draft, scripted, reviewed, produced
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class CourseCreationAgent(BaseAgent):
    name = "course_creation"
    description = (
        "Creates and sells online courses on Udemy, Skillshare, and Gumroad. "
        "Researches profitable topics, creates detailed curricula, writes lesson "
        "scripts, and publishes for passive income."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(COURSE_SCHEMA)

    def plan(self) -> list[str]:
        courses = self.db.execute("SELECT status, COUNT(*) as c FROM courses GROUP BY status")
        stats = {r["status"]: r["c"] for r in courses}
        plan = self.think_json(
            f"Course stats: {json.dumps(stats)}. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_topics, design_curriculum, write_lessons, "
            "review_content, plan_marketing, analyze_performance.",
        )
        return plan.get("steps", ["research_topics"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting course creation cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_topics":
                results["topics"] = self._research_topics()
            elif step == "design_curriculum":
                results["curriculum"] = self._design_curriculum()
            elif step == "write_lessons":
                results["lessons"] = self._write_lessons()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_topics(self) -> dict[str, Any]:
        """Find course topics with high demand and low competition."""
        return self.think_json(
            "Research 5 online course topics. Requirements:\n"
            "- High demand on Udemy/Skillshare (people search for this)\n"
            "- Can be taught without video (text + code + diagrams)\n"
            "- Practical, skill-based (not theoretical)\n"
            "- Students willing to pay $20-100\n"
            "- Can be completed in 3-8 hours\n\n"
            "Return: {\"topics\": [{\"title\": str, \"niche\": str, "
            "\"target_audience\": str, \"platform\": str, \"price\": float, "
            "\"estimated_enrollments_monthly\": int, \"competition\": str, "
            "\"unique_angle\": str, \"prerequisites\": [str]}]}"
        )

    def _design_curriculum(self) -> dict[str, Any]:
        """Design a full course curriculum."""
        curriculum = self.think_json(
            "Design a complete course curriculum. Include:\n"
            "- Course title and tagline\n"
            "- 5-8 sections, each with 3-6 lessons\n"
            "- Each lesson: title, learning objective, duration estimate\n"
            "- Practical exercises and projects\n\n"
            "Return: {\"title\": str, \"tagline\": str, \"niche\": str, "
            "\"price\": float, \"platform\": str, \"target_audience\": str, "
            "\"sections\": [{\"name\": str, \"lessons\": [{\"title\": str, "
            "\"objective\": str, \"duration_minutes\": int, \"type\": str}]}], "
            "\"final_project\": str}"
        )

        title = curriculum.get("title", "Untitled Course")
        total_lessons = sum(
            len(s.get("lessons", [])) for s in curriculum.get("sections", [])
        )
        course_id = self.db.execute_insert(
            "INSERT INTO courses (title, niche, platform, price, description, "
            "target_audience, total_lessons, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'planning')",
            (title, curriculum.get("niche", ""), curriculum.get("platform", "udemy"),
             curriculum.get("price", 29.99), curriculum.get("tagline", ""),
             curriculum.get("target_audience", ""), total_lessons),
        )

        # Store lessons
        order = 0
        for section in curriculum.get("sections", []):
            for lesson in section.get("lessons", []):
                order += 1
                self.db.execute_insert(
                    "INSERT INTO course_lessons (course_id, section, title, lesson_order, "
                    "duration_minutes) VALUES (?, ?, ?, ?, ?)",
                    (course_id, section["name"], lesson["title"], order,
                     lesson.get("duration_minutes", 10)),
                )

        self.log_action("curriculum_designed", title, f"{total_lessons} lessons")
        return {**curriculum, "course_id": course_id}

    def _write_lessons(self) -> dict[str, Any]:
        """Write lesson scripts for courses in scripting phase."""
        # Find lessons that need scripts
        lessons = self.db.execute(
            "SELECT cl.*, c.title as course_title, c.niche, c.target_audience "
            "FROM course_lessons cl JOIN courses c ON cl.course_id = c.id "
            "WHERE cl.status = 'draft' AND c.status IN ('planning', 'scripting') "
            "ORDER BY cl.course_id, cl.lesson_order LIMIT 3"
        )

        written = 0
        for lesson in lessons:
            l = dict(lesson)
            script = self.llm.chat(
                [
                    {"role": "system", "content": (
                        "You are an expert course instructor. Write a clear, engaging "
                        "lesson script that teaches the concept step by step. Include "
                        "examples, exercises, and key takeaways. Write as if speaking "
                        "to the student directly."
                    )},
                    {"role": "user", "content": (
                        f"Course: {l['course_title']}\n"
                        f"Section: {l['section']}\n"
                        f"Lesson: {l['title']}\n"
                        f"Audience: {l.get('target_audience', 'beginners')}\n"
                        f"Duration: ~{l['duration_minutes']} minutes\n\n"
                        "Write the full lesson script."
                    )},
                ],
                model=self.config.llm.model,
            )

            self.db.execute(
                "UPDATE course_lessons SET script = ?, status = 'scripted' WHERE id = ?",
                (script, l["id"]),
            )
            written += 1

            # Update course status
            self.db.execute(
                "UPDATE courses SET status = 'scripting' WHERE id = ? AND status = 'planning'",
                (l["course_id"],),
            )

        self.log_action("lessons_written", f"{written} lessons scripted")
        return {"lessons_written": written}
