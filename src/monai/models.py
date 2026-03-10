"""Common pydantic response models used across monAI agents.

These models enforce structured LLM outputs — no more hoping the JSON
has the right shape. Validate or retry.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ── Opportunity Evaluation ─────────────────────────────────────

class OpportunityEvaluation(BaseModel):
    """Structured evaluation of a business opportunity."""
    worth_pursuing: bool
    expected_revenue: float = Field(description="Expected revenue in EUR")
    estimated_cost: float = Field(description="Estimated cost in EUR")
    risk_level: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score 0-1")
    reasoning: str


# ── Action Planning ────────────────────────────────────────────

class Action(BaseModel):
    """A single action within a plan."""
    action: str
    priority: int = Field(ge=1, description="Priority (1 = highest)")
    reason: str
    delegate_to_subagent: bool


class ActionPlan(BaseModel):
    """Ordered list of actions to execute."""
    actions: list[Action]


# ── Research ───────────────────────────────────────────────────

class ResearchBrief(BaseModel):
    """Summary of research findings on a niche or topic."""
    title: str
    niche: str
    findings: str
    recommended_action: Literal["pursue", "monitor", "skip"]
    confidence: float = Field(ge=0.0, le=1.0)
    revenue_estimate: float = Field(description="Estimated revenue potential in EUR")


# ── Content Verification ──────────────────────────────────────

class ClaimCheck(BaseModel):
    """Verification result for a single claim."""
    claim: str
    category: str
    status: Literal["verified", "false", "unverifiable"]
    confidence: float = Field(ge=0.0, le=1.0)
    note: str
    correction: str | None = None


class ContentCheckResult(BaseModel):
    """Full content verification result."""
    verdict: Literal["publish", "revise", "block"]
    claims: list[ClaimCheck]


# ── Keyword Research ──────────────────────────────────────────

class Keyword(BaseModel):
    """A single keyword with SEO analysis."""
    keyword: str
    search_volume_estimate: str = Field(description="e.g. '1K-10K', '10K-100K'")
    competition: Literal["low", "medium", "high"]
    monetization: str = Field(description="How to monetize this keyword")
    article_angle: str = Field(description="Suggested article angle")


class KeywordResearch(BaseModel):
    """Keyword research results for a niche."""
    keywords: list[Keyword]
