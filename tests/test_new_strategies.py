"""Tests for all new strategy agents (batch 2).

Covers: Newsletter, LeadGen, SocialMedia, CourseCreation,
        DomainFlipping, PrintOnDemand, SaaS.
"""

import json
from unittest.mock import MagicMock

import pytest

from monai.config import Config
from monai.db.database import Database
from monai.strategies.newsletter import NewsletterAgent
from monai.strategies.lead_gen import LeadGenAgent
from monai.strategies.social_media import SocialMediaAgent
from monai.strategies.course_creation import CourseCreationAgent
from monai.strategies.domain_flipping import DomainFlippingAgent
from monai.strategies.print_on_demand import PrintOnDemandAgent
from monai.strategies.saas import SaaSAgent


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.data_dir = tmp_path
    return c


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.quick.return_value = "test"
    llm.chat_json.return_value = {"steps": []}
    llm.chat.return_value = "test content"
    return llm


# ── Newsletter ────────────────────────────────────────────────


class TestNewsletterAgent:
    def test_schema_created(self, config, db, mock_llm):
        agent = NewsletterAgent(config, db, mock_llm)
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='newsletters'")
        assert len(rows) == 1
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='newsletter_issues'")
        assert len(rows) == 1

    def test_name(self, config, db, mock_llm):
        agent = NewsletterAgent(config, db, mock_llm)
        assert agent.name == "newsletter"

    def test_run_returns_dict(self, config, db, mock_llm):
        agent = NewsletterAgent(config, db, mock_llm)
        result = agent.run()
        assert isinstance(result, dict)

    def test_plan_newsletter_inserts(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {
            "name": "Test Weekly", "tagline": "test", "niche": "tech",
            "format": "curated", "frequency": "weekly", "platform": "substack",
            "first_issues": ["a", "b"], "growth_strategy": "twitter",
            "monetization_timeline": "month 3"
        }
        agent = NewsletterAgent(config, db, mock_llm)
        result = agent._plan_newsletter()
        assert result["name"] == "Test Weekly"
        rows = db.execute("SELECT * FROM newsletters WHERE name = 'Test Weekly'")
        assert len(rows) == 1

    def test_write_issue_no_newsletters(self, config, db, mock_llm):
        agent = NewsletterAgent(config, db, mock_llm)
        result = agent._write_issue()
        assert result["status"] == "no_active_newsletters"


# ── Lead Generation ───────────────────────────────────────────


class TestLeadGenAgent:
    def test_schema_created(self, config, db, mock_llm):
        agent = LeadGenAgent(config, db, mock_llm)
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='lead_lists'")
        assert len(rows) == 1
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='leads'")
        assert len(rows) == 1

    def test_name(self, config, db, mock_llm):
        agent = LeadGenAgent(config, db, mock_llm)
        assert agent.name == "lead_gen"

    def test_build_list_inserts(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {
            "name": "SaaS CTOs", "niche": "b2b_saas", "source": "linkedin",
            "data_points": ["name", "email"], "target_count": 500,
            "qualification_criteria": ["decision maker"], "price_per_lead": 2.0
        }
        agent = LeadGenAgent(config, db, mock_llm)
        result = agent._build_list()
        assert result["list_id"] > 0
        rows = db.execute("SELECT * FROM lead_lists WHERE name = 'SaaS CTOs'")
        assert len(rows) == 1


# ── Social Media ──────────────────────────────────────────────


class TestSocialMediaAgent:
    def test_schema_created(self, config, db, mock_llm):
        agent = SocialMediaAgent(config, db, mock_llm)
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='smm_clients'")
        assert len(rows) == 1
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='social_posts'")
        assert len(rows) == 1

    def test_name(self, config, db, mock_llm):
        agent = SocialMediaAgent(config, db, mock_llm)
        assert agent.name == "social_media"

    def test_content_batch_no_clients(self, config, db, mock_llm):
        agent = SocialMediaAgent(config, db, mock_llm)
        result = agent._create_content_batch()
        assert result["status"] == "no_active_clients"

    def test_content_batch_with_client(self, config, db, mock_llm):
        agent = SocialMediaAgent(config, db, mock_llm)
        db.execute_insert(
            "INSERT INTO smm_clients (client_name, industry, platforms, "
            "content_per_week, status) VALUES (?, ?, ?, ?, 'active')",
            ("Acme Corp", "tech", '["twitter"]', 2),
        )
        mock_llm.chat_json.return_value = {
            "posts": [
                {"content": "Post 1", "hashtags": ["#tech"], "media_suggestion": "", "best_time": "9am"},
                {"content": "Post 2", "hashtags": ["#ai"], "media_suggestion": "", "best_time": "2pm"},
            ]
        }
        result = agent._create_content_batch()
        assert result["posts_created"] == 2
        rows = db.execute("SELECT * FROM social_posts")
        assert len(rows) == 2


# ── Course Creation ───────────────────────────────────────────


class TestCourseCreationAgent:
    def test_schema_created(self, config, db, mock_llm):
        agent = CourseCreationAgent(config, db, mock_llm)
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='courses'")
        assert len(rows) == 1
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='course_lessons'")
        assert len(rows) == 1

    def test_name(self, config, db, mock_llm):
        agent = CourseCreationAgent(config, db, mock_llm)
        assert agent.name == "course_creation"

    def test_design_curriculum_creates_records(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {
            "title": "Python Mastery", "tagline": "Learn Python fast",
            "niche": "programming", "price": 49.99, "platform": "udemy",
            "target_audience": "beginners",
            "sections": [
                {"name": "Basics", "lessons": [
                    {"title": "Variables", "objective": "Learn vars", "duration_minutes": 10, "type": "lesson"},
                    {"title": "Functions", "objective": "Learn funcs", "duration_minutes": 15, "type": "lesson"},
                ]},
            ],
            "final_project": "Build a CLI app",
        }
        agent = CourseCreationAgent(config, db, mock_llm)
        result = agent._design_curriculum()
        assert result["course_id"] > 0

        courses = db.execute("SELECT * FROM courses")
        assert len(courses) == 1
        assert courses[0]["title"] == "Python Mastery"
        assert courses[0]["total_lessons"] == 2

        lessons = db.execute("SELECT * FROM course_lessons")
        assert len(lessons) == 2


# ── Domain Flipping ───────────────────────────────────────────


class TestDomainFlippingAgent:
    def test_schema_created(self, config, db, mock_llm):
        agent = DomainFlippingAgent(config, db, mock_llm)
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='domains'")
        assert len(rows) == 1

    def test_name(self, config, db, mock_llm):
        agent = DomainFlippingAgent(config, db, mock_llm)
        assert agent.name == "domain_flipping"

    def test_list_for_sale(self, config, db, mock_llm):
        agent = DomainFlippingAgent(config, db, mock_llm)
        db.execute_insert(
            "INSERT INTO domains (domain_name, tld, status, estimated_value, niche) "
            "VALUES ('cooltools.io', 'io', 'acquired', 500, 'tech')"
        )
        mock_llm.chat_json.return_value = {
            "listing_title": "cooltools.io", "description": "Great tech domain",
            "asking_price": 999, "min_offer": 500,
            "marketplaces": ["sedo", "afternic"], "target_buyer_profile": "SaaS founders"
        }
        result = agent._list_for_sale()
        assert result["listed"] == 1
        rows = db.execute("SELECT * FROM domains WHERE status = 'listed'")
        assert len(rows) == 1
        assert rows[0]["listed_price"] == 999


# ── Print on Demand ───────────────────────────────────────────


class TestPrintOnDemandAgent:
    def test_schema_created(self, config, db, mock_llm):
        agent = PrintOnDemandAgent(config, db, mock_llm)
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pod_designs'")
        assert len(rows) == 1

    def test_name(self, config, db, mock_llm):
        agent = PrintOnDemandAgent(config, db, mock_llm)
        assert agent.name == "print_on_demand"

    def test_generate_concepts_inserts(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {
            "concepts": [
                {"title": "Code & Coffee", "niche": "developers",
                 "design_text": "I turn coffee into code", "design_style": "minimal",
                 "products": ["t-shirt", "mug"], "tags": ["developer", "coffee"],
                 "audience": "programmers"},
            ]
        }
        agent = PrintOnDemandAgent(config, db, mock_llm)
        result = agent._generate_concepts()
        rows = db.execute("SELECT * FROM pod_designs")
        assert len(rows) == 1
        assert rows[0]["title"] == "Code & Coffee"
        assert rows[0]["status"] == "concept"

    def test_create_listings(self, config, db, mock_llm):
        agent = PrintOnDemandAgent(config, db, mock_llm)
        db.execute_insert(
            "INSERT INTO pod_designs (title, niche, description, products, tags, status) "
            "VALUES ('Test', 'test', 'desc', '[\"t-shirt\"]', '[\"tag\"]', 'concept')"
        )
        mock_llm.chat_json.return_value = {
            "listing_title": "Test", "description": "Great design",
            "tags": ["test"], "platforms": ["redbubble"], "pricing_strategy": "default"
        }
        result = agent._create_listings()
        assert result["listed"] == 1
        rows = db.execute("SELECT * FROM pod_designs WHERE status = 'listed'")
        assert len(rows) == 1


# ── SaaS ──────────────────────────────────────────────────────


class TestSaaSAgent:
    def test_schema_created(self, config, db, mock_llm):
        agent = SaaSAgent(config, db, mock_llm)
        for table in ["saas_products", "saas_features", "market_research"]:
            rows = db.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            assert len(rows) == 1, f"Table {table} not created"

    def test_name(self, config, db, mock_llm):
        agent = SaaSAgent(config, db, mock_llm)
        assert agent.name == "saas"

    def test_discover_stores_promising(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {
            "opportunities": [
                {"name": "InboxZero", "problem": "Email overload",
                 "target_market": "Founders", "current_solutions": ["Superhuman"],
                 "why_switch": "Cheaper", "market_size": "$1B",
                 "revenue_model": "subscription", "estimated_mrr_potential": 2000,
                 "build_complexity": "medium", "moat": "AI-powered"},
                {"name": "TinyTool", "problem": "minor issue",
                 "target_market": "nobody", "current_solutions": [],
                 "why_switch": "", "market_size": "$1K",
                 "revenue_model": "one-time", "estimated_mrr_potential": 50,
                 "build_complexity": "low", "moat": "none"},
            ]
        }
        agent = SaaSAgent(config, db, mock_llm)
        result = agent._discover_opportunities()
        # Only the >$500 MRR one should be stored
        rows = db.execute("SELECT * FROM saas_products")
        assert len(rows) == 1
        assert rows[0]["name"] == "InboxZero"

    def test_validate_updates_score(self, config, db, mock_llm):
        agent = SaaSAgent(config, db, mock_llm)
        db.execute_insert(
            "INSERT INTO saas_products (name, problem, solution, target_market, status) "
            "VALUES ('TestSaaS', 'problem', 'solution', 'devs', 'researching')"
        )
        mock_llm.chat_json.return_value = {
            "scores": {"problem_severity": 8, "market_size": 7, "willingness_to_pay": 8,
                       "competition": 7, "buildability": 9, "distribution": 6, "retention": 8},
            "total_score": 53, "verdict": "build",
            "reasoning": "Strong opportunity", "risks": ["competition"],
            "suggested_pivot": ""
        }
        result = agent._validate_idea()
        assert result["verdict"] == "build"

        rows = db.execute("SELECT * FROM saas_products WHERE name = 'TestSaaS'")
        assert rows[0]["status"] == "validated"
        assert rows[0]["validation_score"] > 0.7

        research = db.execute("SELECT * FROM market_research")
        assert len(research) == 1

    def test_validate_kills_weak_idea(self, config, db, mock_llm):
        agent = SaaSAgent(config, db, mock_llm)
        db.execute_insert(
            "INSERT INTO saas_products (name, problem, solution, target_market, status) "
            "VALUES ('WeakIdea', 'meh', 'not great', 'nobody', 'researching')"
        )
        mock_llm.chat_json.return_value = {
            "scores": {"problem_severity": 3, "market_size": 2, "willingness_to_pay": 2,
                       "competition": 3, "buildability": 5, "distribution": 2, "retention": 3},
            "total_score": 20, "verdict": "kill",
            "reasoning": "No demand", "risks": ["everything"],
            "suggested_pivot": "different market"
        }
        result = agent._validate_idea()
        assert result["verdict"] == "kill"
        rows = db.execute("SELECT * FROM saas_products WHERE name = 'WeakIdea'")
        assert rows[0]["status"] == "researching"  # stays in researching, not promoted

    def test_design_architecture_stores_features(self, config, db, mock_llm):
        agent = SaaSAgent(config, db, mock_llm)
        db.execute_insert(
            "INSERT INTO saas_products (name, problem, solution, target_market, status) "
            "VALUES ('ArchTest', 'problem', 'solution', 'devs', 'validated')"
        )
        mock_llm.chat_json.return_value = {
            "tech_stack": {"backend": "Python/FastAPI", "frontend": "React",
                           "database": "PostgreSQL", "hosting": "Railway",
                           "auth": "Clerk", "payments": "Stripe"},
            "core_features": [
                {"name": "User Auth", "description": "Login/signup",
                 "priority": "critical", "complexity": "low"},
                {"name": "Dashboard", "description": "Main dashboard",
                 "priority": "high", "complexity": "medium"},
            ],
            "api_endpoints": [{"method": "GET", "path": "/api/users", "description": "List users"}],
            "data_model": [{"table": "users", "fields": ["id", "email"]}],
            "mvp_scope": "Auth + Dashboard", "estimated_build_hours": 20
        }
        result = agent._design_architecture()

        products = db.execute("SELECT * FROM saas_products WHERE name = 'ArchTest'")
        assert products[0]["status"] == "building"
        assert "FastAPI" in products[0]["tech_stack"]

        features = db.execute("SELECT * FROM saas_features")
        assert len(features) == 2
