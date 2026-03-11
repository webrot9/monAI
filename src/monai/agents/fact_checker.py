"""Fact-checker agent — verifies claims in all content before publication.

Every brand monAI operates MUST have verified content. No AI slop.

Pipeline: Content → Humanizer → FactChecker → Publish

The fact-checker:
1. Extracts verifiable claims from content (numbers, dates, names, statements)
2. Cross-references each claim against known sources
3. Flags unverifiable, dubious, or false claims
4. Returns a verdict: publish, revise, or block
5. Tracks accuracy score per brand over time

This is NOT optional — every piece of content passes through here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

FACT_CHECK_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_check_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL,
    brand TEXT NOT NULL,
    content_type TEXT DEFAULT 'article',    -- article, social_post, email, landing_page, review
    claims_found INTEGER DEFAULT 0,
    claims_verified INTEGER DEFAULT 0,
    claims_flagged INTEGER DEFAULT 0,
    claims_false INTEGER DEFAULT 0,
    verdict TEXT NOT NULL,                  -- publish, revise, block
    accuracy_score REAL,                    -- 0.0 to 1.0
    claims_detail TEXT,                     -- JSON array of individual claim checks
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS brand_accuracy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    total_checks INTEGER DEFAULT 0,
    total_claims INTEGER DEFAULT 0,
    verified_claims INTEGER DEFAULT 0,
    false_claims INTEGER DEFAULT 0,
    avg_accuracy REAL DEFAULT 1.0,
    last_check TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(brand)
);
""";

# Claim categories that require verification
CLAIM_CATEGORIES = {
    "statistic",      # Numbers, percentages, data points
    "attribution",    # Quotes, "according to X"
    "historical",     # Dates, events, timelines
    "scientific",     # Research findings, studies
    "comparative",    # "X is better than Y", rankings
    "financial",      # Prices, market data, ROI claims
    "legal",          # Regulations, requirements
    "technical",      # Technical specifications, capabilities
}


class FactChecker(BaseAgent):
    """Verifies factual claims in content before publication."""

    name = "fact_checker"
    description = (
        "Extracts and verifies factual claims in all content. "
        "Every brand must pass fact-checking before publishing."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(FACT_CHECK_SCHEMA)

    def plan(self) -> list[str]:
        """Plan autonomous fact-checking cycle."""
        steps = []

        # Check for brands with declining accuracy
        brands = self.get_all_brand_accuracy()
        declining = [b for b in brands if b["avg_accuracy"] < 0.8]
        if declining:
            steps.append("audit_low_accuracy_brands")

        # Check for recent blocks that need review
        blocked = self.db.execute(
            "SELECT COUNT(*) as cnt FROM fact_check_results "
            "WHERE verdict = 'block' AND created_at > datetime('now', '-24 hours')"
        )
        if blocked and blocked[0]["cnt"] > 0:
            steps.append("review_recent_blocks")

        steps.append("generate_accuracy_report")
        return steps

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Autonomous cycle: audit accuracy trends and flag problem areas."""
        self.log_action("run", "Starting fact-checker autonomous cycle")
        results: dict[str, Any] = {}

        # 1. Audit brands with low accuracy — identify recurring false-claim categories
        brands = self.get_all_brand_accuracy()
        problem_brands = [b for b in brands if b["avg_accuracy"] < 0.8]
        if problem_brands:
            brand_issues = {}
            for brand in problem_brands:
                recent = self.db.execute(
                    "SELECT claims_detail FROM fact_check_results "
                    "WHERE brand = ? AND verdict IN ('block', 'revise') "
                    "ORDER BY created_at DESC LIMIT 10",
                    (brand["brand"],),
                )
                false_categories: list[str] = []
                for row in recent:
                    try:
                        claims = json.loads(row["claims_detail"] or "[]")
                        for c in claims:
                            if c.get("status") in ("false", "unverifiable"):
                                false_categories.append(c.get("category", "unknown"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                from collections import Counter
                common = Counter(false_categories).most_common(3)
                brand_issues[brand["brand"]] = {
                    "accuracy": brand["avg_accuracy"],
                    "weak_categories": [cat for cat, _ in common],
                }
            results["problem_brands"] = brand_issues

        # 2. Review recent blocks — share learnings with other agents
        recent_blocks = self.get_blocked_content()
        if recent_blocks:
            blocking_patterns = []
            for block in recent_blocks[:5]:
                try:
                    claims = json.loads(block.get("claims_detail", "[]"))
                    false_claims = [c for c in claims if c.get("status") == "false"]
                    for fc in false_claims:
                        blocking_patterns.append({
                            "brand": block["brand"],
                            "claim": fc.get("claim", ""),
                            "category": fc.get("category", ""),
                        })
                except (json.JSONDecodeError, TypeError):
                    pass
            if blocking_patterns:
                self.share_knowledge(
                    category="quality",
                    topic="common_false_claims",
                    content=json.dumps(blocking_patterns[:10], default=str),
                    tags=["fact_check", "quality_alert"],
                )
            results["blocks_reviewed"] = len(recent_blocks)

        # 3. Generate accuracy report
        results["accuracy_report"] = self.get_accuracy_report()
        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    # ── Main Entry Point ───────────────────────────────────────

    def check(self, content: str, brand: str,
              content_type: str = "article",
              context: str = "") -> dict[str, Any]:
        """Fact-check content and return verdict.

        Args:
            content: The content to verify
            brand: Which brand this is for
            content_type: article, social_post, email, landing_page, review
            context: Additional context (topic, audience, etc.)

        Returns:
            Dict with verdict, accuracy_score, claims detail, and
            optionally corrected_content if revisions were needed.
        """
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        # Step 1: Extract verifiable claims
        claims = self._extract_claims(content, content_type)

        if not claims:
            result = {
                "verdict": "publish",
                "accuracy_score": 1.0,
                "claims_found": 0,
                "claims_verified": 0,
                "claims_flagged": 0,
                "claims_false": 0,
                "message": "No verifiable claims found — opinion/creative content.",
            }
            self._record_result(content_hash, brand, content_type, result, [])
            return result

        # Step 2: Verify each claim
        checked_claims = self._verify_claims(claims, content, context)

        # Step 3: Score and verdict
        verified = sum(1 for c in checked_claims if c["status"] == "verified")
        flagged = sum(1 for c in checked_claims if c["status"] == "unverifiable")
        false_count = sum(1 for c in checked_claims if c["status"] == "false")
        total = len(checked_claims)

        accuracy = verified / total if total > 0 else 1.0

        if false_count > 0:
            verdict = "block"
        elif flagged > total * 0.3:
            verdict = "revise"
        elif accuracy >= 0.8:
            verdict = "publish"
        else:
            verdict = "revise"

        result = {
            "verdict": verdict,
            "accuracy_score": round(accuracy, 3),
            "claims_found": total,
            "claims_verified": verified,
            "claims_flagged": flagged,
            "claims_false": false_count,
            "claims": checked_claims,
        }

        # Step 4: If revisions needed, suggest corrections
        if verdict == "revise":
            corrections = self._suggest_corrections(content, checked_claims)
            result["suggested_corrections"] = corrections

        if verdict == "block":
            false_claims = [c for c in checked_claims if c["status"] == "false"]
            result["blocking_reasons"] = [
                f"False claim: {c['claim']} — {c.get('note', 'no detail')}"
                for c in false_claims
            ]

        # Record result
        self._record_result(content_hash, brand, content_type, result, checked_claims)
        self._update_brand_accuracy(brand, total, verified, false_count)

        return result

    # ── Claim Extraction ───────────────────────────────────────

    def _extract_claims(self, content: str, content_type: str) -> list[dict[str, Any]]:
        """Use LLM to extract verifiable factual claims from content."""
        response = self.think_json(
            f"Extract ALL verifiable factual claims from this {content_type}. "
            "A claim is a statement that can be proven true or false — "
            "NOT opinions, NOT subjective statements, NOT creative expression. "
            "Focus on: statistics, numbers, dates, names, quoted sources, "
            "scientific statements, legal requirements, technical specs, "
            "comparative claims ('X is better than Y'), and financial data.\n\n"
            f"Content:\n{content[:3000]}\n\n"
            "Return: {\"claims\": [{\"claim\": str, \"category\": str, "
            "\"source_text\": str}]}\n"
            f"Categories: {', '.join(CLAIM_CATEGORIES)}"
        )

        claims = response.get("claims", [])
        return [c for c in claims if isinstance(c, dict) and "claim" in c]

    # ── Claim Verification ─────────────────────────────────────

    def _verify_claims(self, claims: list[dict], content: str,
                       context: str = "") -> list[dict[str, Any]]:
        """Verify each extracted claim using LLM reasoning."""
        if not claims:
            return []

        claims_text = "\n".join(
            f"{i+1}. [{c.get('category', 'unknown')}] {c['claim']}"
            for i, c in enumerate(claims)
        )

        response = self.think_json(
            "Verify each factual claim below. For each claim, determine:\n"
            "- status: 'verified' (known true), 'false' (known false), "
            "'unverifiable' (can't confirm)\n"
            "- confidence: 0.0-1.0 how confident you are\n"
            "- note: brief explanation of your reasoning\n"
            "- correction: if false, what the correct fact is\n\n"
            "IMPORTANT: Be conservative. If you're not sure, mark 'unverifiable'. "
            "Only mark 'false' if you're confident the claim is wrong. "
            "Only mark 'verified' if you're confident the claim is correct.\n\n"
            f"Context: {context or 'General content'}\n\n"
            f"Claims to verify:\n{claims_text}\n\n"
            "Return: {\"results\": [{\"claim\": str, \"category\": str, "
            "\"status\": str, \"confidence\": float, \"note\": str, "
            "\"correction\": str|null}]}"
        )

        results = response.get("results", [])
        if not results:
            # Fallback: mark all as unverifiable
            return [
                {
                    "claim": c["claim"],
                    "category": c.get("category", "unknown"),
                    "status": "unverifiable",
                    "confidence": 0.5,
                    "note": "Verification failed — treating as unverifiable",
                    "correction": None,
                }
                for c in claims
            ]

        # Ensure valid status values
        valid_statuses = {"verified", "false", "unverifiable"}
        for r in results:
            if r.get("status") not in valid_statuses:
                r["status"] = "unverifiable"

        return results

    # ── Corrections ────────────────────────────────────────────

    def _suggest_corrections(self, content: str,
                             claims: list[dict]) -> list[dict[str, Any]]:
        """Generate specific corrections for flagged or false claims."""
        problem_claims = [
            c for c in claims if c["status"] in ("false", "unverifiable")
        ]
        if not problem_claims:
            return []

        corrections = []
        for claim in problem_claims:
            if claim["status"] == "false" and claim.get("correction"):
                corrections.append({
                    "original": claim["claim"],
                    "correction": claim["correction"],
                    "action": "replace",
                    "reason": claim.get("note", ""),
                })
            elif claim["status"] == "unverifiable":
                corrections.append({
                    "original": claim["claim"],
                    "correction": None,
                    "action": "soften_or_remove",
                    "reason": f"Cannot verify: {claim.get('note', 'no source available')}",
                })

        return corrections

    # ── Recording & Tracking ───────────────────────────────────

    def _record_result(self, content_hash: str, brand: str,
                       content_type: str, result: dict,
                       claims: list[dict]) -> int:
        return self.db.execute_insert(
            "INSERT INTO fact_check_results "
            "(content_hash, brand, content_type, claims_found, claims_verified, "
            "claims_flagged, claims_false, verdict, accuracy_score, claims_detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                content_hash, brand, content_type,
                result.get("claims_found", 0),
                result.get("claims_verified", 0),
                result.get("claims_flagged", 0),
                result.get("claims_false", 0),
                result["verdict"],
                result.get("accuracy_score", 0),
                json.dumps(claims),
            ),
        )

    def _update_brand_accuracy(self, brand: str, total_claims: int,
                               verified: int, false_count: int) -> None:
        """Update running accuracy stats for a brand."""
        existing = self.db.execute(
            "SELECT * FROM brand_accuracy WHERE brand = ?", (brand,)
        )

        if existing:
            row = dict(existing[0])
            new_total_checks = row["total_checks"] + 1
            new_total_claims = row["total_claims"] + total_claims
            new_verified = row["verified_claims"] + verified
            new_false = row["false_claims"] + false_count
            new_avg = new_verified / new_total_claims if new_total_claims > 0 else 1.0

            self.db.execute(
                "UPDATE brand_accuracy SET "
                "total_checks = ?, total_claims = ?, verified_claims = ?, "
                "false_claims = ?, avg_accuracy = ?, last_check = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE brand = ?",
                (new_total_checks, new_total_claims, new_verified,
                 new_false, round(new_avg, 3),
                 datetime.now().isoformat(), brand),
            )
        else:
            avg = verified / total_claims if total_claims > 0 else 1.0
            self.db.execute_insert(
                "INSERT INTO brand_accuracy "
                "(brand, total_checks, total_claims, verified_claims, "
                "false_claims, avg_accuracy, last_check) "
                "VALUES (?, 1, ?, ?, ?, ?, ?)",
                (brand, total_claims, verified, false_count,
                 round(avg, 3), datetime.now().isoformat()),
            )

    # ── Reporting ──────────────────────────────────────────────

    def get_brand_accuracy(self, brand: str) -> dict[str, Any] | None:
        """Get accuracy stats for a brand."""
        rows = self.db.execute(
            "SELECT * FROM brand_accuracy WHERE brand = ?", (brand,)
        )
        return dict(rows[0]) if rows else None

    def get_all_brand_accuracy(self) -> list[dict[str, Any]]:
        """Get accuracy stats for all brands."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM brand_accuracy ORDER BY avg_accuracy ASC"
        )]

    def get_recent_checks(self, brand: str = "",
                          limit: int = 20) -> list[dict[str, Any]]:
        """Get recent fact-check results."""
        query = "SELECT * FROM fact_check_results"
        params: list = []
        if brand:
            query += " WHERE brand = ?"
            params.append(brand)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    def get_blocked_content(self, brand: str = "") -> list[dict[str, Any]]:
        """Get content that was blocked by fact-checking."""
        query = "SELECT * FROM fact_check_results WHERE verdict = 'block'"
        params: list = []
        if brand:
            query += " AND brand = ?"
            params.append(brand)
        query += " ORDER BY created_at DESC"
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    def get_accuracy_report(self) -> dict[str, Any]:
        """Overall fact-checking report across all brands."""
        brands = self.get_all_brand_accuracy()

        total_checks = sum(b["total_checks"] for b in brands)
        total_claims = sum(b["total_claims"] for b in brands)
        total_verified = sum(b["verified_claims"] for b in brands)
        total_false = sum(b["false_claims"] for b in brands)

        overall_accuracy = total_verified / total_claims if total_claims > 0 else 1.0

        blocked = self.db.execute(
            "SELECT COUNT(*) as cnt FROM fact_check_results WHERE verdict = 'block'"
        )
        blocked_count = blocked[0]["cnt"] if blocked else 0

        return {
            "total_checks": total_checks,
            "total_claims_verified": total_claims,
            "overall_accuracy": round(overall_accuracy, 3),
            "total_false_claims_caught": total_false,
            "total_content_blocked": blocked_count,
            "brands": brands,
            "worst_brand": brands[0]["brand"] if brands else None,
            "best_brand": brands[-1]["brand"] if brands else None,
        }
