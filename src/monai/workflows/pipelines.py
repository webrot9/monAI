"""Pre-built workflow pipelines for common monAI operations.

These are reusable pipeline definitions that wire agents together
for end-to-end workflows.
"""

from __future__ import annotations

from monai.workflows.engine import Pipeline, Step, StepType


def content_pipeline() -> Pipeline:
    """Content creation → humanization → distribution pipeline.

    Flow: Research keywords → Write article → Humanize → Distribute to
    newsletter + social media + content site.
    """
    return (
        Pipeline("content_pipeline", description="End-to-end content creation and distribution")
        .step("research_keywords", agent="content_sites", action="_research_keywords")
        .step("write_article", agent="content_sites", action="_create_article",
              depends_on=["research_keywords"])
        .step("humanize", agent="humanizer", action="humanize",
              depends_on=["write_article"])
        .parallel("distribute", steps=[
            Step("to_newsletter", agent="newsletter", action="_write_issue"),
            Step("to_social", agent="social_media", action="_create_content_batch"),
        ], depends_on=["humanize"])
    )


def product_launch_pipeline() -> Pipeline:
    """Full product launch: research → validate → build → launch.

    Flow: Market research → Validation → Competitor analysis →
    Architecture → Build MVP → Create landing page → Launch.
    """
    return (
        Pipeline("product_launch", description="SaaS product discovery to launch")
        .step("discover", agent="saas", action="_discover_opportunities")
        .step("validate", agent="saas", action="_validate_idea",
              depends_on=["discover"])
        .conditional("is_validated",
                     condition=lambda ctx: (
                         ctx.get("validate", {}).get("verdict") == "build"
                     ),
                     if_true="competitor_analysis",
                     if_false="pivot",
                     depends_on=["validate"])
        .step("competitor_analysis", agent="saas", action="_competitor_analysis",
              depends_on=["is_validated"])
        .step("design", agent="saas", action="_design_architecture",
              depends_on=["competitor_analysis"])
        .step("build_mvp", agent="saas", action="_build_mvp",
              depends_on=["design"], max_retries=5)
        .step("landing_page", agent="saas", action="_create_landing_page",
              depends_on=["build_mvp"])
        .parallel("launch_marketing", steps=[
            Step("newsletter_announce", agent="newsletter", action="_write_issue"),
            Step("social_announce", agent="social_media", action="_create_content_batch"),
            Step("launch_plan", agent="saas", action="_plan_launch"),
        ], depends_on=["landing_page"])
    )


def client_acquisition_pipeline() -> Pipeline:
    """Find leads → Qualify → Outreach → Proposal → Onboard.

    Flow: Research leads → Enrich data → Qualify → Send proposals →
    Follow up → Onboard client.
    """
    return (
        Pipeline("client_acquisition", description="End-to-end client acquisition")
        .step("find_leads", agent="lead_gen", action="_research_niches")
        .step("build_list", agent="lead_gen", action="_build_list",
              depends_on=["find_leads"])
        .step("qualify", agent="lead_gen", action="_qualify_leads",
              depends_on=["build_list"])
        .parallel("outreach", steps=[
            Step("email_outreach", agent="freelance_writing", action="_send_proposals"),
            Step("social_outreach", agent="social_media", action="_find_clients"),
        ], depends_on=["qualify"])
        .step("follow_up", agent="freelance_writing", action="_follow_up",
              depends_on=["outreach"])
    )


def affiliate_content_pipeline() -> Pipeline:
    """Research products → Write reviews → Humanize → Distribute.

    Flow: Find affiliate programs → Research products → Write reviews/comparisons →
    Humanize → Publish on content site + newsletter.
    """
    return (
        Pipeline("affiliate_content", description="Affiliate content creation pipeline")
        .step("find_programs", agent="affiliate", action="_research_programs")
        .step("research_products", agent="affiliate", action="_research_products",
              depends_on=["find_programs"])
        .parallel("create_content", steps=[
            Step("write_review", agent="affiliate", action="_write_review"),
            Step("write_comparison", agent="affiliate", action="_write_comparison"),
        ], depends_on=["research_products"])
        .step("humanize_content", agent="humanizer", action="humanize",
              depends_on=["create_content"])
        .parallel("publish", steps=[
            Step("to_site", agent="content_sites", action="_create_article"),
            Step("to_newsletter", agent="newsletter", action="_write_issue"),
        ], depends_on=["humanize_content"])
    )


def course_creation_pipeline() -> Pipeline:
    """Research topic → Design curriculum → Write lessons → Humanize → Publish.

    Full course creation from topic research to publication.
    """
    return (
        Pipeline("course_creation", description="Online course creation pipeline")
        .step("research_topics", agent="course_creation", action="_research_topics")
        .step("design_curriculum", agent="course_creation", action="_design_curriculum",
              depends_on=["research_topics"])
        .step("write_lessons", agent="course_creation", action="_write_lessons",
              depends_on=["design_curriculum"], max_retries=5)
        .step("humanize_lessons", agent="humanizer", action="humanize",
              depends_on=["write_lessons"])
    )


def domain_flipping_pipeline() -> Pipeline:
    """Research → Evaluate → Acquire → List for sale."""
    return (
        Pipeline("domain_flipping", description="Domain acquisition and resale")
        .step("research", agent="domain_flipping", action="_research_expired")
        .step("evaluate", agent="domain_flipping", action="_evaluate_domains",
              depends_on=["research"])
        .step("market_analysis", agent="domain_flipping", action="_analyze_market",
              depends_on=["research"])
        .step("list_for_sale", agent="domain_flipping", action="_list_for_sale",
              depends_on=["evaluate"])
    )


def pod_pipeline() -> Pipeline:
    """Research niches → Find trending → Generate designs → List products."""
    return (
        Pipeline("print_on_demand", description="POD design to listing")
        .step("research_niches", agent="print_on_demand", action="_research_niches")
        .step("find_trending", agent="print_on_demand", action="_find_trending")
        .step("generate_concepts", agent="print_on_demand", action="_generate_concepts",
              depends_on=["research_niches", "find_trending"])
        .step("create_listings", agent="print_on_demand", action="_create_listings",
              depends_on=["generate_concepts"])
    )


def revenue_diversification_pipeline() -> Pipeline:
    """Run all revenue channels in parallel for maximum diversification."""
    return (
        Pipeline("revenue_diversification",
                 description="Parallel execution of all revenue channels")
        .parallel("services", steps=[
            Step("freelance", agent="freelance_writing", action="run"),
            Step("lead_gen", agent="lead_gen", action="run"),
            Step("social_media", agent="social_media", action="run"),
        ])
        .parallel("products", steps=[
            Step("digital_products", agent="digital_products", action="run"),
            Step("micro_saas", agent="micro_saas", action="run"),
            Step("telegram_bots", agent="telegram_bots", action="run"),
            Step("courses", agent="course_creation", action="run"),
        ])
        .parallel("content", steps=[
            Step("content_sites", agent="content_sites", action="run"),
            Step("affiliate", agent="affiliate", action="run"),
            Step("newsletter", agent="newsletter", action="run"),
        ])
        .parallel("trading", steps=[
            Step("domains", agent="domain_flipping", action="run"),
            Step("pod", agent="print_on_demand", action="run"),
        ])
        .step("saas_cycle", agent="saas", action="run",
              depends_on=["services", "products", "content", "trading"])
    )


# Registry of all available pipelines
PIPELINE_REGISTRY: dict[str, callable] = {
    "content": content_pipeline,
    "product_launch": product_launch_pipeline,
    "client_acquisition": client_acquisition_pipeline,
    "affiliate_content": affiliate_content_pipeline,
    "course_creation": course_creation_pipeline,
    "domain_flipping": domain_flipping_pipeline,
    "print_on_demand": pod_pipeline,
    "revenue_diversification": revenue_diversification_pipeline,
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
