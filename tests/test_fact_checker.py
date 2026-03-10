"""Tests for the fact-checker agent."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monai.agents.fact_checker import CLAIM_CATEGORIES, FactChecker
from monai.db.database import Database


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(Path(path))
    yield database
    os.unlink(path)


@pytest.fixture
def checker(db):
    config = MagicMock()
    llm = MagicMock()
    with patch("monai.agents.fact_checker.FactChecker.__init__", lambda self, *a, **kw: None):
        fc = FactChecker.__new__(FactChecker)
    fc.config = config
    fc.db = db
    fc.llm = llm
    with db.connect() as conn:
        from monai.agents.fact_checker import FACT_CHECK_SCHEMA
        conn.executescript(FACT_CHECK_SCHEMA)
    return fc


class TestClaimExtraction:
    def test_extract_claims(self, checker):
        checker.think_json = MagicMock(return_value={
            "claims": [
                {"claim": "Python was created in 1991", "category": "historical"},
                {"claim": "70% of developers use VS Code", "category": "statistic"},
            ]
        })

        claims = checker._extract_claims("Python was created in 1991...", "article")
        assert len(claims) == 2
        assert claims[0]["claim"] == "Python was created in 1991"
        assert claims[1]["category"] == "statistic"

    def test_extract_no_claims(self, checker):
        checker.think_json = MagicMock(return_value={"claims": []})
        claims = checker._extract_claims("Just my opinion...", "social_post")
        assert len(claims) == 0

    def test_extract_handles_malformed(self, checker):
        checker.think_json = MagicMock(return_value={"claims": [
            {"claim": "Valid claim", "category": "statistic"},
            {"no_claim_key": "Invalid"},  # Missing 'claim' key
            "not a dict",
        ]})
        claims = checker._extract_claims("Some content", "article")
        assert len(claims) == 1  # Only the valid one


class TestClaimVerification:
    def test_verify_all_true(self, checker):
        claims = [
            {"claim": "Python was created in 1991", "category": "historical"},
        ]
        checker.think_json = MagicMock(return_value={
            "results": [{
                "claim": "Python was created in 1991",
                "category": "historical",
                "status": "verified",
                "confidence": 0.95,
                "note": "Correct — first released Feb 1991",
                "correction": None,
            }]
        })

        results = checker._verify_claims(claims, "Python was created in 1991")
        assert len(results) == 1
        assert results[0]["status"] == "verified"

    def test_verify_false_claim(self, checker):
        claims = [{"claim": "JavaScript was created by Microsoft", "category": "historical"}]
        checker.think_json = MagicMock(return_value={
            "results": [{
                "claim": "JavaScript was created by Microsoft",
                "category": "historical",
                "status": "false",
                "confidence": 0.99,
                "note": "Created by Brendan Eich at Netscape",
                "correction": "JavaScript was created by Brendan Eich at Netscape in 1995",
            }]
        })

        results = checker._verify_claims(claims, "JS was created by Microsoft")
        assert results[0]["status"] == "false"
        assert "Netscape" in results[0]["correction"]

    def test_verify_normalizes_invalid_status(self, checker):
        claims = [{"claim": "Some claim", "category": "technical"}]
        checker.think_json = MagicMock(return_value={
            "results": [{
                "claim": "Some claim",
                "category": "technical",
                "status": "maybe",  # Invalid status
                "confidence": 0.5,
                "note": "",
                "correction": None,
            }]
        })

        results = checker._verify_claims(claims, "content")
        assert results[0]["status"] == "unverifiable"  # Normalized

    def test_verify_fallback_on_empty_response(self, checker):
        claims = [
            {"claim": "Claim 1", "category": "technical"},
            {"claim": "Claim 2", "category": "statistic"},
        ]
        checker.think_json = MagicMock(return_value={"results": []})

        results = checker._verify_claims(claims, "content")
        assert len(results) == 2
        assert all(r["status"] == "unverifiable" for r in results)


class TestVerdicts:
    def test_publish_all_verified(self, checker):
        checker.think_json = MagicMock(side_effect=[
            {"claims": [
                {"claim": "Python is dynamically typed", "category": "technical"},
                {"claim": "Python 3 was released in 2008", "category": "historical"},
            ]},
            {"results": [
                {"claim": "Python is dynamically typed", "category": "technical",
                 "status": "verified", "confidence": 0.95, "note": "Correct", "correction": None},
                {"claim": "Python 3 was released in 2008", "category": "historical",
                 "status": "verified", "confidence": 0.9, "note": "Dec 2008", "correction": None},
            ]},
        ])

        result = checker.check("Python is dynamically typed and version 3 was released in 2008.",
                              "tech_blog", "article")
        assert result["verdict"] == "publish"
        assert result["accuracy_score"] == 1.0
        assert result["claims_found"] == 2

    def test_block_on_false_claim(self, checker):
        checker.think_json = MagicMock(side_effect=[
            {"claims": [{"claim": "Earth is flat", "category": "scientific"}]},
            {"results": [{
                "claim": "Earth is flat", "category": "scientific",
                "status": "false", "confidence": 1.0,
                "note": "Earth is an oblate spheroid",
                "correction": "Earth is roughly spherical",
            }]},
        ])

        result = checker.check("The Earth is flat.", "bad_blog", "article")
        assert result["verdict"] == "block"
        assert result["claims_false"] == 1
        assert len(result["blocking_reasons"]) == 1

    def test_revise_on_many_unverifiable(self, checker):
        checker.think_json = MagicMock(side_effect=[
            {"claims": [
                {"claim": "A", "category": "statistic"},
                {"claim": "B", "category": "statistic"},
                {"claim": "C", "category": "statistic"},
            ]},
            {"results": [
                {"claim": "A", "category": "statistic", "status": "verified",
                 "confidence": 0.9, "note": "", "correction": None},
                {"claim": "B", "category": "statistic", "status": "unverifiable",
                 "confidence": 0.3, "note": "No source", "correction": None},
                {"claim": "C", "category": "statistic", "status": "unverifiable",
                 "confidence": 0.2, "note": "No source", "correction": None},
            ]},
        ])

        result = checker.check("A, B, C claims", "blog", "article")
        assert result["verdict"] == "revise"
        assert "suggested_corrections" in result

    def test_publish_no_claims(self, checker):
        checker.think_json = MagicMock(return_value={"claims": []})

        result = checker.check("Just vibes, no facts.", "casual_brand", "social_post")
        assert result["verdict"] == "publish"
        assert result["claims_found"] == 0


class TestBrandAccuracy:
    def test_accuracy_tracking(self, checker):
        # First check
        checker.think_json = MagicMock(side_effect=[
            {"claims": [{"claim": "True fact", "category": "technical"}]},
            {"results": [{"claim": "True fact", "category": "technical",
                         "status": "verified", "confidence": 0.9,
                         "note": "", "correction": None}]},
        ])
        checker.check("True fact here", "my_brand", "article")

        accuracy = checker.get_brand_accuracy("my_brand")
        assert accuracy is not None
        assert accuracy["total_checks"] == 1
        assert accuracy["total_claims"] == 1
        assert accuracy["verified_claims"] == 1
        assert accuracy["avg_accuracy"] == 1.0

    def test_accuracy_updates_over_time(self, checker):
        # Check 1: all verified
        checker.think_json = MagicMock(side_effect=[
            {"claims": [{"claim": "A", "category": "technical"}]},
            {"results": [{"claim": "A", "category": "technical",
                         "status": "verified", "confidence": 0.9,
                         "note": "", "correction": None}]},
        ])
        checker.check("Content A", "brand_x", "article")

        # Check 2: has a false claim
        checker.think_json = MagicMock(side_effect=[
            {"claims": [{"claim": "B", "category": "statistic"}]},
            {"results": [{"claim": "B", "category": "statistic",
                         "status": "false", "confidence": 0.95,
                         "note": "Wrong", "correction": "Correct B"}]},
        ])
        checker.check("Content B", "brand_x", "article")

        accuracy = checker.get_brand_accuracy("brand_x")
        assert accuracy["total_checks"] == 2
        assert accuracy["total_claims"] == 2
        assert accuracy["verified_claims"] == 1
        assert accuracy["false_claims"] == 1
        assert accuracy["avg_accuracy"] == 0.5

    def test_no_accuracy_for_unknown_brand(self, checker):
        assert checker.get_brand_accuracy("nonexistent") is None


class TestReporting:
    def test_recent_checks(self, checker):
        checker.think_json = MagicMock(side_effect=[
            {"claims": [{"claim": "X", "category": "technical"}]},
            {"results": [{"claim": "X", "category": "technical",
                         "status": "verified", "confidence": 0.9,
                         "note": "", "correction": None}]},
        ])
        checker.check("Content X", "brand_a", "article")

        recent = checker.get_recent_checks("brand_a")
        assert len(recent) == 1
        assert recent[0]["brand"] == "brand_a"
        assert recent[0]["verdict"] == "publish"

    def test_blocked_content(self, checker):
        checker.think_json = MagicMock(side_effect=[
            {"claims": [{"claim": "False", "category": "scientific"}]},
            {"results": [{"claim": "False", "category": "scientific",
                         "status": "false", "confidence": 1.0,
                         "note": "Wrong", "correction": "Right"}]},
        ])
        checker.check("False claim content", "bad_brand", "article")

        blocked = checker.get_blocked_content()
        assert len(blocked) == 1
        assert blocked[0]["brand"] == "bad_brand"

    def test_accuracy_report(self, checker):
        # Add checks for two brands
        checker.think_json = MagicMock(side_effect=[
            {"claims": [{"claim": "A", "category": "technical"}]},
            {"results": [{"claim": "A", "category": "technical",
                         "status": "verified", "confidence": 0.9,
                         "note": "", "correction": None}]},
        ])
        checker.check("A", "good_brand", "article")

        checker.think_json = MagicMock(side_effect=[
            {"claims": [{"claim": "B", "category": "technical"}]},
            {"results": [{"claim": "B", "category": "technical",
                         "status": "false", "confidence": 0.9,
                         "note": "Wrong", "correction": "Right"}]},
        ])
        checker.check("B", "bad_brand", "article")

        report = checker.get_accuracy_report()
        assert report["total_checks"] == 2
        assert report["total_false_claims_caught"] == 1
        assert report["total_content_blocked"] == 1
        assert report["worst_brand"] == "bad_brand"
        assert report["best_brand"] == "good_brand"


class TestClaimCategories:
    def test_all_categories_defined(self):
        expected = {"statistic", "attribution", "historical", "scientific",
                    "comparative", "financial", "legal", "technical"}
        assert CLAIM_CATEGORIES == expected


class TestCorrections:
    def test_suggest_corrections_for_false(self, checker):
        claims = [
            {"claim": "X is true", "status": "false", "confidence": 0.9,
             "note": "X is wrong", "correction": "Y is true"},
        ]
        corrections = checker._suggest_corrections("content", claims)
        assert len(corrections) == 1
        assert corrections[0]["action"] == "replace"
        assert corrections[0]["correction"] == "Y is true"

    def test_suggest_corrections_for_unverifiable(self, checker):
        claims = [
            {"claim": "Maybe Z", "status": "unverifiable", "confidence": 0.3,
             "note": "No source", "correction": None},
        ]
        corrections = checker._suggest_corrections("content", claims)
        assert len(corrections) == 1
        assert corrections[0]["action"] == "soften_or_remove"

    def test_no_corrections_for_verified(self, checker):
        claims = [
            {"claim": "True fact", "status": "verified", "confidence": 0.95,
             "note": "Correct", "correction": None},
        ]
        corrections = checker._suggest_corrections("content", claims)
        assert len(corrections) == 0
