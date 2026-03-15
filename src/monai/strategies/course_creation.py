"""Course creation strategy agent.

Creates and sells online courses on platforms like Udemy, Skillshare, Gumroad.
Passive income once published. Uses existing content capabilities.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

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
    listing_url TEXT,
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

        # Deterministic progression
        if not stats:
            return ["research_topics"]
        if stats.get("planning", 0) > 0:
            return ["design_curriculum"]
        if stats.get("scripting", 0) > 0:
            return ["write_lessons"]
        if stats.get("producing", 0) > 0:
            return ["review_product"]
        if stats.get("reviewed", 0) > 0:
            return ["list_course"]
        if stats.get("published", 0) > 0:
            return ["plan_marketing"]

        # All courses at final stage — research new topics
        return ["research_topics"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting course creation cycle")
        steps = self.plan()
        results = {}

        step_methods = {
            "research_topics": self._research_topics,
            "design_curriculum": self._design_curriculum,
            "write_lessons": self._write_lessons,
            "review_product": self._review_product,
            "list_course": self._list_course,
        }
        for step in steps:
            fn = step_methods.get(step)
            if fn:
                results[step] = self.run_step(step, fn)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_topics(self) -> dict[str, Any]:
        """Research REAL trending course topics by browsing actual course platforms."""
        all_topics = []

        # Browse Udemy for trending/bestselling courses to find gaps
        udemy_data = self.browse_and_extract(
            "https://www.udemy.com/courses/development/?sort=popularity",
            "Analyze the trending and bestselling courses on this page. For each course extract:\n"
            "- title: the course title\n"
            "- instructor: who teaches it\n"
            "- rating: the star rating\n"
            "- num_students: number of enrolled students\n"
            "- price: the listed price\n"
            "- topics_covered: key topics/skills taught\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"courses\": [...]}"
        )
        for course in udemy_data.get("courses", []):
            course["source"] = "udemy"
            all_topics.append(course)

        # Browse Skillshare for trending classes
        skillshare_data = self.browse_and_extract(
            "https://www.skillshare.com/en/browse/trending",
            "Find trending classes on Skillshare. For each class extract:\n"
            "- title: the class title\n"
            "- instructor: who teaches it\n"
            "- num_students: number of students if visible\n"
            "- category: the category/topic area\n"
            "- duration: class length if shown\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"classes\": [...]}"
        )
        for cls in skillshare_data.get("classes", []):
            cls["source"] = "skillshare"
            all_topics.append(cls)

        # Browse Coursera for popular professional certificates and courses
        coursera_data = self.browse_and_extract(
            "https://www.coursera.org/courses?sortBy=BEST_MATCH",
            "Find popular courses and professional certificates. For each extract:\n"
            "- title: the course/certificate title\n"
            "- provider: university or organization offering it\n"
            "- rating: the star rating\n"
            "- num_reviews: number of reviews\n"
            "- skills: key skills taught\n"
            "- difficulty_level: beginner/intermediate/advanced\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"courses\": [...]}"
        )
        for course in coursera_data.get("courses", []):
            course["source"] = "coursera"
            all_topics.append(course)

        # Search for underserved course topics people are actively requesting
        demand_data = self.search_web(
            "online course topic ideas high demand 2025 2026 underserved",
            "Find real discussions, articles, or forum posts about course topics "
            "that are in high demand but underserved. For each extract:\n"
            "- topic: the suggested course topic\n"
            "- source_url: where this was discussed\n"
            "- evidence_of_demand: why this is in demand\n"
            "- competition_level: how many courses already exist on this\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"opportunities\": [...]}"
        )
        for opp in demand_data.get("opportunities", []):
            opp["source"] = "web_research"
            all_topics.append(opp)

        # Use LLM to ANALYZE the real data and identify the best opportunities
        analysis = self.think_json(
            f"Based on this REAL market research data from course platforms, "
            f"identify the 5 best course topic opportunities:\n\n"
            f"Market data: {json.dumps(all_topics, default=str)[:3000]}\n\n"
            "For each opportunity explain:\n"
            "- Why it's a good opportunity based on the data\n"
            "- What unique angle we could take\n"
            "- Which platform to target\n\n"
            "Return: {\"topics\": [{\"title\": str, \"niche\": str, "
            "\"target_audience\": str, \"platform\": str, \"price\": float, "
            "\"rationale\": str, \"unique_angle\": str, \"prerequisites\": [str]}]}"
        )

        self.log_action(
            "topics_researched",
            f"Scraped {len(all_topics)} courses/topics from real platforms, "
            f"identified {len(analysis.get('topics', []))} opportunities"
        )
        return {
            "market_data_collected": len(all_topics),
            "topics": analysis.get("topics", []),
            "raw_market_data": all_topics[:20],  # keep first 20 for reference
        }

    def _design_curriculum(self) -> dict[str, Any]:
        """Design a full course curriculum. LLM-based planning is legitimate."""
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

            # Check for review feedback from previous rejection
            review_context = ""
            prev_reviews = self.db.execute(
                "SELECT issues, suggestions FROM product_reviews "
                "WHERE strategy = ? AND product_name = ? ORDER BY id DESC LIMIT 1",
                (self.name, l["course_title"]),
            )
            if prev_reviews:
                from monai.agents.product_reviewer import ProductReviewer
                review_data = {
                    "issues": json.loads(prev_reviews[0].get("issues", "[]")),
                    "suggestions": json.loads(prev_reviews[0].get("suggestions", "[]")),
                    "quality_score": 0,
                }
                review_context = ProductReviewer.format_feedback_for_prompt(review_data)

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
                        f"Write the full lesson script.{review_context}"
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

    def _review_product(self) -> dict[str, Any]:
        """Quality gate: review course content before listing."""
        courses = self.db.execute(
            "SELECT * FROM courses WHERE status = 'producing' LIMIT 1"
        )
        if not courses:
            return {"status": "no_courses_to_review"}

        course = courses[0]
        lessons = self.db.execute(
            "SELECT * FROM course_lessons WHERE course_id = ? ORDER BY lesson_order",
            (course["id"],),
        )
        # Build content for review
        content_parts = []
        for lesson in lessons:
            if lesson.get("script"):
                content_parts.append({
                    "section": f"{lesson['section']} — {lesson['title']}",
                    "content": lesson["script"][:2000],
                })

        product_data = {
            "spec": {
                "title": course["title"],
                "description": course.get("description", ""),
                "target_audience": course.get("target_audience", ""),
                "features": [f"Lesson: {l['title']}" for l in lessons[:10]],
            },
            "content": content_parts,
        }

        result = self.reviewer.review_product(
            strategy=self.name,
            product_name=course["title"],
            product_data=product_data,
            product_type="course",
        )

        if result.verdict == "rejected":
            # Reset lesson statuses so _write_lessons rewrites them with feedback
            self.db.execute_insert(
                "UPDATE course_lessons SET status = 'draft' WHERE course_id = ?",
                (course["id"],),
            )
            self.db.execute_insert(
                "UPDATE courses SET status = 'scripting' WHERE id = ?",
                (course["id"],),
            )
            self.log_action("course_review_rejected",
                            f"{course['title']}: {'; '.join(result.issues[:3])}")
        elif result.verdict == "needs_revision":
            # Actively revise content before proceeding
            self.reviewer.revise_product(product_data, result, "course")
            self.db.execute_insert(
                "UPDATE courses SET status = 'reviewed' WHERE id = ?",
                (course["id"],),
            )
            self.log_action("course_revised",
                            f"{course['title']}: REVISED and proceeding (score={result.quality_score:.2f})")
        else:
            self.db.execute_insert(
                "UPDATE courses SET status = 'reviewed' WHERE id = ?",
                (course["id"],),
            )
            self.log_action("course_reviewed",
                            f"{course['title']}: {result.verdict} (score={result.quality_score:.2f})")

        return result.to_dict()

    def _list_course(self) -> dict[str, Any]:
        """List a completed course on its target platform using platform_action."""
        # Find courses that are fully scripted and ready to publish
        courses = self.db.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM course_lessons WHERE course_id = c.id AND status = 'scripted') as scripted, "
            "(SELECT COUNT(*) FROM course_lessons WHERE course_id = c.id) as total "
            "FROM courses c WHERE c.status IN ('scripting', 'producing', 'reviewed')"
        )

        listed = 0
        for course in courses:
            c = dict(course)
            # Only list if all lessons are scripted
            if c["scripted"] < c["total"]:
                logger.info(
                    "Course %s has %d/%d lessons scripted, skipping listing",
                    c["title"], c["scripted"], c["total"]
                )
                continue

            # Gather all lesson content for the listing
            lessons = self.db.execute(
                "SELECT section, title, script, duration_minutes, lesson_order "
                "FROM course_lessons WHERE course_id = ? ORDER BY lesson_order",
                (c["id"],),
            )
            lesson_data = [dict(l) for l in lessons]

            # Build a course description using LLM (legitimate creative work)
            listing_copy = self.think_json(
                f"Write a compelling course listing for a platform like {c['platform']}.\n"
                f"Course title: {c['title']}\n"
                f"Niche: {c['niche']}\n"
                f"Target audience: {c['target_audience']}\n"
                f"Price: ${c['price']}\n"
                f"Total lessons: {c['total']}\n"
                f"Sections and lessons: {json.dumps([(l['section'], l['title']) for l in lesson_data])}\n\n"
                "Return: {\"headline\": str, \"description\": str, "
                "\"what_youll_learn\": [str], \"requirements\": [str], "
                "\"who_is_this_for\": str, \"tags\": [str]}"
            )

            platform = c["platform"]

            # Ensure we have an account on the platform
            self.ensure_platform_account(platform)

            # Actually list the course on the platform
            try:
                result = self.platform_action(
                    platform,
                    f"Create a new course listing with the following details:\n"
                    f"Title: {c['title']}\n"
                    f"Price: ${c['price']}\n"
                    f"Description: {listing_copy.get('description', '')}\n"
                    f"Headline: {listing_copy.get('headline', '')}\n"
                    f"What you'll learn: {json.dumps(listing_copy.get('what_youll_learn', []))}\n"
                    f"Requirements: {json.dumps(listing_copy.get('requirements', []))}\n"
                    f"Target audience: {listing_copy.get('who_is_this_for', '')}\n"
                    f"Tags: {json.dumps(listing_copy.get('tags', []))}\n\n"
                    f"Course has {c['total']} lessons across these sections:\n"
                    + "\n".join(
                        f"  Section '{l['section']}' - Lesson {l['lesson_order']}: {l['title']} "
                        f"({l['duration_minutes']} min)"
                        for l in lesson_data
                    )
                    + "\n\nFor each lesson, upload the following script content as the lesson body.",
                    f"Listing course '{c['title']}' on {platform}. "
                    f"Lesson scripts are ready for all {c['total']} lessons."
                )

                listing_url = result.get("url", "")
                self.db.execute(
                    "UPDATE courses SET status = 'published', listing_url = ?, "
                    "published_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (listing_url, c["id"]),
                )
                listed += 1
                self.log_action(
                    "course_listed", c["title"],
                    f"platform={platform} url={listing_url}"
                )

            except Exception as e:
                self.log_action(
                    "course_listing_failed", c["title"], str(e)[:300]
                )
                self.learn_from_error(e, f"Listing course '{c['title']}' on {platform}")

        self.log_action("list_courses_complete", f"{listed} courses listed")
        return {"courses_listed": listed}

    def apply_improvements(self) -> dict[str, Any]:
        """Apply pending improvements from ProductIterator to existing courses.

        Rewrites lesson scripts based on improvement plans (competitor gaps,
        quality issues, etc.) and sends courses back through the review pipeline.
        """
        pending = self.product_iterator.get_pending_improvements(self.name)
        if not pending:
            return {"status": "no_pending_improvements"}

        applied = 0
        for improvement in pending[:2]:  # Max 2 per cycle
            product_name = improvement["product_name"]
            improvements_json = improvement.get("improvements", "[]")
            try:
                plan = json.loads(improvements_json) if isinstance(improvements_json, str) else improvements_json
            except (json.JSONDecodeError, TypeError):
                plan = {}

            improvement_items = plan.get("improvements", []) if isinstance(plan, dict) else []

            # Find the course in DB
            courses = self.db.execute(
                "SELECT * FROM courses WHERE title = ? LIMIT 1",
                (product_name,),
            )
            if not courses:
                self.product_iterator.mark_applied(improvement["id"])
                continue

            course = dict(courses[0])
            lessons = self.db.execute(
                "SELECT * FROM course_lessons WHERE course_id = ? ORDER BY lesson_order",
                (course["id"],),
            )

            change_descriptions = [
                f"- {item.get('area', 'general')}: {item.get('specific_change', item.get('current_issue', ''))}"
                for item in improvement_items
            ]
            changes_text = "\n".join(change_descriptions) if change_descriptions else "General quality improvements"

            # Rewrite lesson scripts with improvements
            rewritten = 0
            for lesson in lessons:
                lesson_dict = dict(lesson)
                if not lesson_dict.get("script"):
                    continue

                revised_script = self.llm.chat(
                    [
                        {"role": "system", "content": (
                            "You are improving an existing course lesson. Apply the "
                            "required improvements while preserving the core educational "
                            "content. The result must be premium quality."
                        )},
                        {"role": "user", "content": (
                            f"Course: {course['title']}\n"
                            f"Lesson: {lesson_dict['title']}\n"
                            f"Section: {lesson_dict['section']}\n\n"
                            f"CURRENT SCRIPT:\n{lesson_dict['script'][:3000]}\n\n"
                            f"REQUIRED IMPROVEMENTS:\n{changes_text}\n\n"
                            "Rewrite this lesson incorporating the improvements."
                        )},
                    ],
                    model=self.config.llm.model,
                )

                self.db.execute(
                    "UPDATE course_lessons SET script = ?, status = 'scripted' WHERE id = ?",
                    (revised_script, lesson_dict["id"]),
                )
                rewritten += 1

            # Send course back through review pipeline
            if rewritten > 0:
                self.db.execute(
                    "UPDATE courses SET status = 'producing' WHERE id = ?",
                    (course["id"],),
                )

            self.product_iterator.mark_applied(improvement["id"])
            applied += 1
            self.log_action(
                "course_improved",
                f"{product_name}: {rewritten} lessons rewritten with improvements",
            )

        return {"applied": applied, "total_pending": len(pending)}
