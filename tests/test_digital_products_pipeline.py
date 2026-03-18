"""Integration test: full digital products pipeline.

Tests the complete DigitalProductsAgent lifecycle:
  research → create → review → list → check_sales
Each run() call advances the pipeline by one step.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monai.strategies.digital_products import DigitalProductsAgent


@pytest.fixture
def products_dir(tmp_path):
    d = tmp_path / "products"
    d.mkdir()
    return d


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock()
    config.data_dir = tmp_path
    config.llm.model = "test-model"
    config.llm.cheap_model = "test-cheap"
    return config


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat.return_value = "Generated section content with actionable insights."
    llm.chat_json.return_value = {}
    llm.quick.return_value = "Quick response"
    return llm


@pytest.fixture
def agent(mock_config, db, mock_llm, products_dir):
    """Create DigitalProductsAgent with all external deps mocked."""
    agent = DigitalProductsAgent(mock_config, db, mock_llm)
    agent.products_dir = products_dir

    # Mock identity/provisioner (not testing account setup)
    agent._identity = MagicMock()
    agent._identity.get_api_key.return_value = "fake_gumroad_token"
    agent._provisioner = MagicMock()

    # Mock browser methods
    agent.browse_and_extract = MagicMock(return_value={
        "status": "completed",
        "result": {"trending": [
            {"category": "templates", "product_name": "AI Toolkit", "price": "$19.99"},
        ]},
    })
    agent.execute_task = MagicMock(return_value={"status": "completed"})

    # Mock Gumroad integration
    gumroad = MagicMock()
    gumroad.health_check.return_value = {"status": "ok"}
    gumroad.create_product.return_value = {
        "id": "gum_prod_123",
        "short_url": "https://test.gumroad.com/l/test-product",
    }
    gumroad.get_revenue_summary.return_value = {
        "total_revenue_usd": 0,
        "total_sales": 0,
        "products": [],
    }
    agent._gumroad = gumroad

    # Mock product reviewer
    _make_reviewer_mock(agent)

    # Mock product iterator
    agent._product_iterator = MagicMock()
    agent._product_iterator.get_pending_improvements.return_value = []

    return agent


def _make_reviewer_mock(agent):
    """Set up a reviewer that approves products."""
    from monai.agents.product_reviewer import ReviewResult
    reviewer = MagicMock()
    reviewer.review_product.return_value = ReviewResult(
        verdict="approved",
        quality_score=0.85,
        humanizer_score=0.9,
        factcheck_verdict="publish",
        factcheck_accuracy=0.95,
        legal_status="approved",
        usability_score=0.8,
        issues=[],
        suggestions=["Consider adding more examples"],
        improved_content={"humanized": "Polished version of the listing"},
    )
    agent._reviewer = reviewer
    return reviewer


# ── Step 1: Research ────────────────────────────────────────────

class TestResearchStep:
    def test_research_creates_product_files(self, agent, products_dir, mock_llm):
        """Research step browses marketplaces and saves niche files."""
        mock_llm.chat_json.return_value = {
            "niches": [
                {
                    "niche": "AI Prompt Packs",
                    "product_type": "prompt_pack",
                    "estimated_price": 14.99,
                    "reasoning": "High demand in AI tooling space",
                },
                {
                    "niche": "Notion Templates",
                    "product_type": "template",
                    "estimated_price": 9.99,
                    "reasoning": "Consistent seller on Gumroad",
                },
            ],
        }

        result = agent.run()

        # Should have called browse_and_extract for Gumroad + ProductHunt
        assert agent.browse_and_extract.call_count == 2
        urls = [c[0][0] for c in agent.browse_and_extract.call_args_list]
        assert any("gumroad.com" in u for u in urls)
        assert any("producthunt.com" in u for u in urls)

        # Product files created with status="researched"
        files = list(products_dir.glob("*.json"))
        assert len(files) == 2
        for f in files:
            data = json.loads(f.read_text())
            assert data["status"] == "researched"
            assert "research" in data

    def test_research_limits_to_two_niches(self, agent, products_dir, mock_llm):
        """Only top 2 niches saved even if LLM returns more."""
        mock_llm.chat_json.return_value = {
            "niches": [{"niche": f"Niche {i}", "product_type": "ebook",
                        "estimated_price": 9.99, "reasoning": "test"}
                       for i in range(5)],
        }
        agent.run()
        assert len(list(products_dir.glob("*.json"))) == 2


# ── Step 2: Create ──────────────────────────────────────────────

class TestCreateStep:
    def test_create_generates_content_sections(self, agent, products_dir, mock_llm):
        """Create step generates content for each section."""
        # Pre-seed a researched product
        (products_dir / "AI Prompts.json").write_text(json.dumps({
            "research": {
                "niche": "AI Prompts",
                "product_type": "prompt_pack",
                "estimated_price": 14.99,
                "reasoning": "Growing market",
            },
            "status": "researched",
        }))

        mock_llm.chat_json.return_value = {
            "title": "Ultimate AI Prompt Collection",
            "type": "prompt_pack",
            "description": "100+ expert-crafted prompts",
            "target_audience": "AI enthusiasts",
            "price": 14.99,
            "sections": ["Introduction", "Creative Prompts", "Business Prompts"],
        }

        result = agent.run()

        # Product file updated to "created"
        data = json.loads((products_dir / "AI Prompts.json").read_text())
        assert data["status"] == "created"
        assert data["spec"]["title"] == "Ultimate AI Prompt Collection"
        assert len(data["content"]) == 3

        # LLM called once per section
        assert mock_llm.chat.call_count >= 3


# ── Step 3: Review ──────────────────────────────────────────────

class TestReviewStep:
    def test_approved_product_advances_to_reviewed(self, agent, products_dir):
        """Approved review → status becomes 'reviewed'."""
        (products_dir / "Test Product.json").write_text(json.dumps({
            "spec": {"title": "Test Product", "price": 9.99},
            "content": [{"section": "Intro", "content": "Hello world"}],
            "status": "created",
        }))

        result = agent.run()

        data = json.loads((products_dir / "Test Product.json").read_text())
        assert data["status"] == "reviewed"
        assert data["review"]["verdict"] == "approved"
        assert data["review"]["quality_score"] == 0.85
        # Humanized listing saved
        assert data.get("humanized_listing") == "Polished version of the listing"

    def test_rejected_product_loops_back(self, agent, products_dir):
        """Rejected review → status goes back to 'researched' for re-creation."""
        from monai.agents.product_reviewer import ReviewResult
        agent._reviewer.review_product.return_value = ReviewResult(
            verdict="rejected",
            quality_score=0.3,
            factcheck_verdict="block",
            factcheck_accuracy=0.4,
            legal_status="blocked",
            usability_score=0.2,
            issues=["Contains factual errors", "Legal risk detected"],
            suggestions=[],
        )

        (products_dir / "Bad Product.json").write_text(json.dumps({
            "spec": {"title": "Bad Product", "price": 5.99},
            "content": [{"section": "Intro", "content": "Dubious claims"}],
            "status": "created",
        }))

        agent.run()

        data = json.loads((products_dir / "Bad Product.json").read_text())
        assert data["status"] == "researched"  # Sent back for re-creation
        assert data["review"]["verdict"] == "rejected"

    def test_needs_revision_gets_revised(self, agent, products_dir):
        """Needs-revision → content is revised, then proceeds to 'reviewed'."""
        from monai.agents.product_reviewer import ReviewResult
        agent._reviewer.review_product.return_value = ReviewResult(
            verdict="needs_revision",
            quality_score=0.6,
            factcheck_verdict="revise",
            factcheck_accuracy=0.7,
            legal_status="approved",
            usability_score=0.5,
            issues=["Needs more examples"],
            suggestions=["Add case studies"],
        )
        agent._reviewer.revise_product.return_value = {
            "revised_intro": "Improved introduction with case studies",
        }

        (products_dir / "Needs Work.json").write_text(json.dumps({
            "spec": {"title": "Needs Work", "price": 12.99},
            "content": [{"section": "Intro", "content": "Thin content"}],
            "status": "created",
        }))

        agent.run()

        data = json.loads((products_dir / "Needs Work.json").read_text())
        assert data["status"] == "reviewed"  # Proceeds after revision
        assert "revised_content" in data
        agent._reviewer.revise_product.assert_called_once()


# ── Step 4: List on Gumroad ─────────────────────────────────────

class TestListStep:
    def test_list_calls_gumroad_api(self, agent, products_dir, mock_llm):
        """Listing step creates product via Gumroad API."""
        mock_llm.chat_json.return_value = "A compelling marketplace description"
        mock_llm.quick.return_value = "A compelling marketplace description"

        (products_dir / "Listed Product.json").write_text(json.dumps({
            "spec": {
                "title": "Listed Product",
                "price": 19.99,
                "type": "ebook",
                "description": "An awesome ebook",
                "target_audience": "developers",
            },
            "content": [{"section": "Ch1", "content": "Content"}],
            "review": {"verdict": "approved", "quality_score": 0.85},
            "status": "reviewed",
        }))

        agent.run()

        # Gumroad API called with correct args
        agent._gumroad.create_product.assert_called_once()
        call_kwargs = agent._gumroad.create_product.call_args
        assert call_kwargs[1]["name"] == "Listed Product"
        assert call_kwargs[1]["price"] in (1998, 1999)  # int(19.99*100) float rounding

        # Product file updated with gumroad_id and status
        data = json.loads((products_dir / "Listed Product.json").read_text())
        assert data["status"] == "listed"
        assert data["gumroad_id"] == "gum_prod_123"
        assert data["gumroad_url"] == "https://test.gumroad.com/l/test-product"

    def test_gumroad_api_failure_does_not_crash(self, agent, products_dir, mock_llm):
        """If Gumroad API fails, product stays in reviewed state."""
        mock_llm.chat_json.return_value = "Description"
        mock_llm.quick.return_value = "Description"
        agent._gumroad.create_product.side_effect = Exception("API Error: rate limited")

        (products_dir / "Failing Product.json").write_text(json.dumps({
            "spec": {"title": "Failing Product", "price": 9.99, "type": "guide",
                     "description": "A guide", "target_audience": "everyone"},
            "content": [{"section": "Ch1", "content": "Content"}],
            "review": {"verdict": "approved"},
            "status": "reviewed",
        }))

        # Should not raise
        agent.run()

        # Status unchanged
        data = json.loads((products_dir / "Failing Product.json").read_text())
        assert data["status"] == "reviewed"


# ── Step 5: Check Sales ─────────────────────────────────────────

class TestCheckSalesStep:
    def test_check_sales_with_revenue(self, agent, products_dir):
        """Sales check records revenue from Gumroad."""
        agent._gumroad.get_revenue_summary.return_value = {
            "total_revenue_usd": 149.97,
            "total_sales": 10,
            "products": [
                {"name": "AI Prompts", "id": "p1", "revenue_usd": 99.99, "sales": 7},
                {"name": "Templates", "id": "p2", "revenue_usd": 49.98, "sales": 3},
            ],
        }

        (products_dir / "Active Product.json").write_text(json.dumps({
            "spec": {"title": "Active Product"},
            "status": "listed",
        }))

        result = agent.run()

        # Revenue summary returned
        assert "check_sales" in result
        agent._gumroad.get_revenue_summary.assert_called_once_with("digital_products")

    def test_check_sales_no_gumroad_returns_not_configured(self, agent, products_dir):
        """If Gumroad not set up, returns graceful status."""
        agent._identity.get_api_key.return_value = None
        agent._gumroad.health_check.return_value = {"status": "not_configured"}

        (products_dir / "Listed.json").write_text(json.dumps({
            "spec": {"title": "Listed"},
            "status": "listed",
        }))

        result = agent.run()
        assert "check_sales" in result


# ── Full Pipeline ───────────────────────────────────────────────

class TestFullPipeline:
    def test_four_runs_advance_through_entire_pipeline(
        self, agent, products_dir, mock_llm
    ):
        """4 sequential run() calls advance: research → create → review → list."""
        # Run 1: Research
        mock_llm.chat_json.return_value = {
            "niches": [{
                "niche": "Productivity Templates",
                "product_type": "template",
                "estimated_price": 12.99,
                "reasoning": "Consistently popular",
            }],
        }
        result1 = agent.run()
        files = list(products_dir.glob("*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text())["status"] == "researched"

        # Run 2: Create
        mock_llm.chat_json.return_value = {
            "title": "Ultimate Productivity Pack",
            "type": "template",
            "description": "50 Notion templates for peak productivity",
            "target_audience": "knowledge workers",
            "price": 12.99,
            "sections": ["Getting Started", "Daily Planning", "Weekly Review"],
        }
        result2 = agent.run()
        data = json.loads(files[0].read_text())
        assert data["status"] == "created"
        assert len(data["content"]) == 3

        # Run 3: Review (approved)
        result3 = agent.run()
        data = json.loads(files[0].read_text())
        assert data["status"] == "reviewed"
        assert data["review"]["verdict"] == "approved"

        # Run 4: List on Gumroad
        mock_llm.chat_json.return_value = "Marketplace description"
        mock_llm.quick.return_value = "Marketplace description"
        result4 = agent.run()
        data = json.loads(files[0].read_text())
        assert data["status"] == "listed"
        assert data["gumroad_id"] == "gum_prod_123"
        assert data["gumroad_url"] == "https://test.gumroad.com/l/test-product"

        # Verify Gumroad API was called correctly
        agent._gumroad.create_product.assert_called_once()
        call_kwargs = agent._gumroad.create_product.call_args[1]
        assert call_kwargs["name"] == "Ultimate Productivity Pack"
        assert call_kwargs["price"] == 1299

    def test_plan_returns_correct_next_step(self, agent, products_dir):
        """plan() deterministically picks the right step."""
        # No products → research
        assert agent.plan() == ["research_niches"]

        # Researched → create
        (products_dir / "p1.json").write_text(
            json.dumps({"status": "researched", "research": {}})
        )
        assert agent.plan() == ["create_product"]

        # Created → review
        _set_status(products_dir / "p1.json", "created")
        assert agent.plan() == ["review_product"]

        # Reviewed → list
        _set_status(products_dir / "p1.json", "reviewed")
        assert agent.plan() == ["list_product"]

        # Listed → check_sales
        _set_status(products_dir / "p1.json", "listed")
        assert agent.plan() == ["check_sales"]


def _set_status(path: Path, status: str):
    data = json.loads(path.read_text())
    data["status"] = status
    path.write_text(json.dumps(data))
