"""Pre-built workflow pipelines for common monAI operations.

These are reusable pipeline definitions that wire agents together
for end-to-end workflows.

IMPORTANT: Every content pipeline MUST include a fact_check step.
Flow: content generation → fact_check → humanize → distribute.
No content leaves monAI without verification.
"""

from __future__ import annotations

from monai.workflows.engine import Pipeline, Step, StepType


def digital_products_pipeline() -> Pipeline:
    """Digital product creation → review → fact-check → humanize → list on Gumroad.

    Flow: Research niche → Create product → Review quality →
    Fact-check → Humanize → List on Gumroad.
    """
    return (
        Pipeline("digital_products", description="Digital product creation and Gumroad listing")
        .step("research", agent="digital_products", action="_research_niches")
        .step("create", agent="digital_products", action="_create_product",
              depends_on=["research"])
        .step("review", agent="product_reviewer", action="review",
              depends_on=["create"])
        .step("fact_check", agent="fact_checker", action="check",
              depends_on=["review"])
        .step("humanize", agent="humanizer", action="humanize",
              depends_on=["fact_check"])
        .step("list_product", agent="digital_products", action="_list_product",
              depends_on=["humanize"])
    )


# Registry of all available pipelines
PIPELINE_REGISTRY: dict[str, callable] = {
    "digital_products": digital_products_pipeline,
}


def get_pipeline(name: str) -> Pipeline | None:
    """Get a pipeline by name from the registry."""
    factory = PIPELINE_REGISTRY.get(name)
    return factory() if factory else None


def list_pipelines() -> list[dict[str, str]]:
    """List all available pipelines."""
    return [
        {"name": name, "description": factory().description}
        for name, factory in PIPELINE_REGISTRY.items()
    ]
