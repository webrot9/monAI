"""Product quality gate — reviews products before they go live.

Coordinates Humanizer, FactChecker, and Legal Advisor to ensure every
product monAI sells is high quality, factually accurate, legally safe,
and indistinguishable from expert human work.

Sits in the pipeline between creation/build and listing/deploy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from monai.agents.base import BaseAgent
from monai.agents.fact_checker import FactChecker
from monai.agents.humanizer import Humanizer
from monai.agents.legal import LegalAdvisorFactory
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    product_name TEXT NOT NULL,
    verdict TEXT NOT NULL,             -- approved, needs_revision, rejected
    quality_score REAL DEFAULT 0.0,    -- 0-1 aggregate score
    humanizer_score REAL DEFAULT 0.0,
    factcheck_verdict TEXT,            -- publish, revise, block
    factcheck_accuracy REAL DEFAULT 0.0,
    legal_status TEXT,                 -- approved, blocked, needs_review
    usability_score REAL DEFAULT 0.0,  -- 0-1 does this actually deliver value?
    issues TEXT,                       -- JSON list of issues found
    suggestions TEXT,                  -- JSON list of improvement suggestions
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass
class ReviewResult:
    """Result of a product review."""

    verdict: str = "needs_revision"  # approved, needs_revision, rejected
    quality_score: float = 0.0
    humanizer_score: float = 0.0
    factcheck_verdict: str = ""
    factcheck_accuracy: float = 0.0
    legal_status: str = ""
    usability_score: float = 0.0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    improved_content: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "quality_score": self.quality_score,
            "humanizer_score": self.humanizer_score,
            "factcheck_verdict": self.factcheck_verdict,
            "factcheck_accuracy": self.factcheck_accuracy,
            "legal_status": self.legal_status,
            "usability_score": self.usability_score,
            "issues": self.issues,
            "suggestions": self.suggestions,
        }


class ProductReviewer(BaseAgent):
    """Reviews products before they go live.

    Checks:
    1. Content quality (Humanizer) — no AI slop, reads like expert human work
    2. Factual accuracy (FactChecker) — no false claims, citations correct
    3. Legal compliance (Legal Advisor) — no legal issues in any jurisdiction
    4. Usability (LLM assessment) — product actually delivers what it promises
    """

    name = "product_reviewer"
    description = (
        "Quality gate for all products. Ensures everything monAI sells is "
        "high quality, factually accurate, legally compliant, and genuinely useful."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(REVIEW_SCHEMA)
        self._humanizer = None
        self._fact_checker = None
        self._legal_factory = None

    @property
    def humanizer(self) -> Humanizer:
        if self._humanizer is None:
            self._humanizer = Humanizer(self.config, self.db, self.llm)
        return self._humanizer

    @property
    def fact_checker(self) -> FactChecker:
        if self._fact_checker is None:
            self._fact_checker = FactChecker(self.config, self.db, self.llm)
        return self._fact_checker

    @property
    def legal_factory(self) -> LegalAdvisorFactory:
        if self._legal_factory is None:
            self._legal_factory = LegalAdvisorFactory(self.config, self.db, self.llm)
        return self._legal_factory

    def plan(self) -> list[str]:
        return ["review"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        return self.review_product(**kwargs)

    def review_product(
        self,
        strategy: str,
        product_name: str,
        product_data: dict[str, Any],
        product_type: str = "digital_product",
    ) -> ReviewResult:
        """Full product review pipeline.

        Args:
            strategy: Strategy name (e.g. "digital_products", "telegram_bots")
            product_name: Human-readable product name
            product_data: Full product data dict (spec, content, design, etc.)
            product_type: One of: digital_product, saas, bot, course, content

        Returns:
            ReviewResult with verdict, scores, issues, and improved content
        """
        self.log_action("review_start", f"{strategy}/{product_name}")
        result = ReviewResult()

        # Collect all textual content from the product
        content_text = self._extract_content(product_data, product_type)
        if not content_text:
            result.issues.append("No content found in product data")
            result.verdict = "rejected"
            self._save_review(strategy, product_name, result)
            return result

        # 1. Usability assessment — does this actually deliver value?
        result.usability_score = self._assess_usability(
            product_name, product_data, product_type, content_text,
        )
        if result.usability_score < 0.3:
            result.issues.append(
                f"Usability score too low ({result.usability_score:.2f}): "
                "product does not deliver sufficient value"
            )

        # 2. Humanizer — make content indistinguishable from expert human work
        humanized_content, humanizer_score = self._humanize_content(
            content_text, product_type,
        )
        result.humanizer_score = humanizer_score
        if humanizer_score < 0.7:
            result.issues.append(
                f"Content quality score ({humanizer_score:.2f}) below threshold: "
                "still reads like AI-generated content"
            )
        if humanized_content != content_text:
            result.improved_content["humanized"] = humanized_content

        # 3. Fact checker — verify all claims
        fc_result = self._fact_check(
            humanized_content or content_text, product_name, product_type,
        )
        result.factcheck_verdict = fc_result.get("verdict", "")
        result.factcheck_accuracy = fc_result.get("accuracy_score", 0.0)
        if result.factcheck_verdict == "block":
            result.issues.append(
                f"Fact check BLOCKED: {'; '.join(fc_result.get('blocking_reasons', []))}"
            )
        elif result.factcheck_verdict == "revise":
            corrections = fc_result.get("suggested_corrections", [])
            for c in corrections[:5]:
                result.suggestions.append(
                    f"Fix claim: '{c.get('original', '')[:80]}' → "
                    f"'{c.get('correction', '')[:80]}'"
                )

        # 4. Legal review — check for legal issues
        legal_result = self._legal_review(
            strategy, product_name, product_data, product_type,
        )
        result.legal_status = legal_result.get("status", "")
        if result.legal_status == "blocked":
            blockers = legal_result.get("blockers", [])
            for b in blockers:
                result.issues.append(f"Legal blocker: {b}")
        elif result.legal_status == "needs_review":
            for req in legal_result.get("requirements", []):
                result.suggestions.append(f"Legal requirement: {req}")

        # Compute aggregate score
        scores = [
            result.usability_score,
            result.humanizer_score,
            result.factcheck_accuracy,
            1.0 if result.legal_status == "approved" else 0.5 if result.legal_status == "needs_review" else 0.0,
        ]
        result.quality_score = sum(scores) / len(scores)

        # Determine verdict
        if result.factcheck_verdict == "block" or result.legal_status == "blocked":
            result.verdict = "rejected"
        elif result.quality_score >= 0.7 and result.usability_score >= 0.5:
            result.verdict = "approved"
        else:
            result.verdict = "needs_revision"

        self._save_review(strategy, product_name, result)
        self.log_action(
            "review_complete",
            f"{strategy}/{product_name}: {result.verdict} "
            f"(score={result.quality_score:.2f}, issues={len(result.issues)})",
        )
        return result

    def _extract_content(self, product_data: dict, product_type: str) -> str:
        """Extract all textual content from a product for review."""
        parts = []

        # Product spec/description
        spec = product_data.get("spec", product_data.get("design", {}))
        if isinstance(spec, dict):
            for key in ("title", "name", "tagline", "description", "problem", "solution"):
                if key in spec:
                    parts.append(f"{key}: {spec[key]}")
            # Features
            features = spec.get("features", [])
            if features:
                if isinstance(features[0], dict):
                    parts.append("Features: " + ", ".join(
                        f.get("name", "") for f in features
                    ))
                else:
                    parts.append("Features: " + ", ".join(str(f) for f in features))

        # Content sections (digital products, courses)
        content = product_data.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    section = item.get("section", "")
                    text = item.get("content", "")
                    if text:
                        parts.append(f"## {section}\n{text[:2000]}")
                elif isinstance(item, str):
                    parts.append(item[:2000])

        # Listing/description text
        listing = product_data.get("listing", "")
        if listing:
            parts.append(f"Listing:\n{listing[:2000]}")

        return "\n\n".join(parts)

    def _assess_usability(
        self,
        product_name: str,
        product_data: dict,
        product_type: str,
        content_text: str,
    ) -> float:
        """Assess whether the product actually delivers value to buyers."""
        assessment = self.think_json(
            f"You are a critical product reviewer. Assess this {product_type} "
            f"called '{product_name}' for real-world usability.\n\n"
            f"Product content:\n{content_text[:3000]}\n\n"
            "Score each dimension 0-1:\n"
            "- completeness: Does it cover the topic thoroughly?\n"
            "- actionability: Can a buyer immediately use this?\n"
            "- uniqueness: Does it offer something beyond a quick Google search?\n"
            "- value_for_money: Would someone feel good about paying for this?\n"
            "- professionalism: Does it look/feel professional?\n\n"
            "Be HARSH. If this is generic filler content, score it low.\n"
            "Return: {\"completeness\": float, \"actionability\": float, "
            "\"uniqueness\": float, \"value_for_money\": float, "
            "\"professionalism\": float, \"overall\": float, "
            "\"issues\": [str], \"suggestions\": [str]}"
        )

        return assessment.get("overall", 0.0)

    def _humanize_content(
        self, content: str, product_type: str,
    ) -> tuple[str, float]:
        """Run content through Humanizer and return improved text + score."""
        try:
            # Pick style profile based on product type
            profile_map = {
                "digital_product": "professional_author",
                "course": "educator",
                "content": "journalist",
                "saas": "tech_writer",
                "bot": "tech_writer",
            }
            profile = profile_map.get(product_type, "default")

            humanized = self.humanizer.humanize(
                content,
                style_profile=profile,
                context=f"Product content for sale — must be premium quality",
            )

            # Get quality stats
            stats = self.humanizer.get_quality_stats()
            avg_score = stats.get("avg_quality_score", 0.8)

            return humanized, avg_score
        except Exception as e:
            logger.warning(f"Humanizer failed: {e}")
            return content, 0.5

    def _fact_check(
        self, content: str, product_name: str, product_type: str,
    ) -> dict[str, Any]:
        """Run content through FactChecker."""
        try:
            content_type_map = {
                "digital_product": "review",
                "course": "article",
                "content": "article",
                "saas": "landing_page",
                "bot": "landing_page",
            }
            ct = content_type_map.get(product_type, "article")

            return self.fact_checker.check(
                content=content,
                brand=product_name,
                content_type=ct,
                context=f"Product being sold: {product_name}",
            )
        except Exception as e:
            logger.warning(f"FactChecker failed: {e}")
            return {"verdict": "revise", "accuracy_score": 0.5}

    def _legal_review(
        self,
        strategy: str,
        product_name: str,
        product_data: dict,
        product_type: str,
    ) -> dict[str, Any]:
        """Run legal review on the product."""
        try:
            spec = product_data.get("spec", product_data.get("design", {}))
            pricing = spec.get("pricing", spec.get("monetization", ""))
            description = (
                f"Selling a {product_type}: {product_name}. "
                f"Strategy: {strategy}. "
                f"Pricing: {pricing}. "
            )

            return self.legal_factory.assess_activity(
                activity_name=f"sell_{product_name}",
                activity_type="strategy",
                description=description,
                requesting_agent=strategy,
            )
        except Exception as e:
            logger.warning(f"Legal review failed: {e}")
            return {"status": "needs_review", "requirements": [str(e)]}

    def _save_review(self, strategy: str, product_name: str, result: ReviewResult):
        """Persist review result to database."""
        self.db.execute_insert(
            "INSERT INTO product_reviews "
            "(strategy, product_name, verdict, quality_score, humanizer_score, "
            "factcheck_verdict, factcheck_accuracy, legal_status, usability_score, "
            "issues, suggestions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                strategy,
                product_name,
                result.verdict,
                result.quality_score,
                result.humanizer_score,
                result.factcheck_verdict,
                result.factcheck_accuracy,
                result.legal_status,
                result.usability_score,
                json.dumps(result.issues),
                json.dumps(result.suggestions),
            ),
        )
