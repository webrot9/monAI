"""Dynamic landing page generator.

Takes a Config object and generates index.html with real payment addresses,
live funding progress from the database, and configured payment links.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from monai.config import Config
from monai.db.database import Database

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).parent / "index.html"
OUTPUT_DIR = Path(__file__).parent


def _get_funding_progress(db: Database) -> dict[str, Any]:
    """Pull live funding data from the crowdfunding tables.

    Returns dict with keys: raised, goal, backers.
    """
    default = {"raised": 0, "goal": 500, "backers": 0}

    try:
        # Get the active campaign
        campaigns = db.fetch_all(
            "SELECT goal_amount, raised_amount, backer_count "
            "FROM crowdfunding_campaigns "
            "WHERE status IN ('active', 'funded') "
            "ORDER BY created_at DESC LIMIT 1"
        )
        if campaigns:
            row = campaigns[0]
            return {
                "raised": row[1] or 0,
                "goal": row[0] or 500,
                "backers": row[2] or 0,
            }

        # Fallback: sum contributions directly
        totals = db.fetch_all(
            "SELECT COALESCE(SUM(amount), 0), COUNT(*) "
            "FROM crowdfunding_contributions "
            "WHERE status = 'completed'"
        )
        if totals and totals[0][0] > 0:
            return {
                "raised": totals[0][0],
                "goal": 500,
                "backers": totals[0][1],
            }
    except Exception as e:
        logger.warning("Could not fetch funding progress: %s", e)

    return default


def _build_stripe_link(base_url: str, amount_eur: int) -> str:
    """Build a Stripe payment link for a given tier amount.

    If base_url is a Stripe Payment Links URL, return as-is.
    If it's a checkout session template, append the amount.
    """
    if not base_url:
        return f"#tier-{amount_eur}"
    # Stripe Payment Links are already complete URLs
    if "buy.stripe.com" in base_url or "checkout.stripe.com" in base_url:
        return base_url
    # Template URL with amount placeholder
    return base_url.replace("{amount}", str(amount_eur * 100))


def _build_kofi_link(config: Config) -> str:
    """Build Ko-fi donation page link from config."""
    # Check if there's a Ko-fi URL in the campaign data or config
    # Default to a placeholder that the generator caller should override
    return "https://ko-fi.com/monai"


def _get_monero_address(config: Config) -> str:
    """Get the Monero address for receiving crowdfunding contributions."""
    # Prefer creator wallet if set, otherwise use a placeholder
    if config.creator_wallet.xmr_address:
        return config.creator_wallet.xmr_address
    return "4... (Monero address will appear here once configured)"


def generate(
    config: Config,
    db: Optional[Database] = None,
    output_path: Optional[Path] = None,
    stripe_links: Optional[dict[int, str]] = None,
    kofi_url: Optional[str] = None,
    monero_address: Optional[str] = None,
) -> Path:
    """Generate the landing page with live data and real payment links.

    Args:
        config: monAI Config object.
        db: Database connection for funding progress. If None, uses defaults.
        output_path: Where to write the generated file. Defaults to same dir as template.
        stripe_links: Dict mapping tier amounts to Stripe payment URLs.
            e.g. {10: "https://buy.stripe.com/abc", 50: "...", 200: "..."}
        kofi_url: Ko-fi page URL. Defaults to https://ko-fi.com/monai.
        monero_address: XMR address for crypto payments. Falls back to config.

    Returns:
        Path to the generated index.html file.
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "index.html"

    # Read template
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    # Get funding progress
    progress = {"raised": 0, "goal": 500, "backers": 0}
    if db is not None:
        progress = _get_funding_progress(db)

    # Resolve payment links
    stripe = stripe_links or {}
    stripe_10 = stripe.get(10, _build_stripe_link("", 10))
    stripe_50 = stripe.get(50, _build_stripe_link("", 50))
    stripe_200 = stripe.get(200, _build_stripe_link("", 200))

    kofi = kofi_url or _build_kofi_link(config)
    xmr = monero_address or _get_monero_address(config)

    # Apply replacements
    replacements = {
        "{{RAISED_AMOUNT}}": str(progress["raised"]),
        "{{GOAL_AMOUNT}}": str(progress["goal"]),
        "{{BACKER_COUNT}}": str(progress["backers"]),
        "{{STRIPE_LINK_10}}": stripe_10,
        "{{STRIPE_LINK_50}}": stripe_50,
        "{{STRIPE_LINK_200}}": stripe_200,
        "{{KOFI_LINK}}": kofi,
        "{{MONERO_ADDRESS}}": xmr,
    }

    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    # Warn about any remaining placeholders
    remaining = re.findall(r"\{\{[A-Z_]+\}\}", html)
    if remaining:
        logger.warning(
            "Unreplaced placeholders in landing page: %s",
            ", ".join(set(remaining)),
        )

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("Generated landing page at %s", output_path)

    return output_path


def generate_preview(output_path: Optional[Path] = None) -> Path:
    """Generate a preview version with placeholder data (no config/db needed).

    Useful for local development and testing.
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "preview.html"

    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    # Fill with demo data
    replacements = {
        "{{RAISED_AMOUNT}}": "127",
        "{{GOAL_AMOUNT}}": "500",
        "{{BACKER_COUNT}}": "14",
        "{{STRIPE_LINK_10}}": "#demo-tier-10",
        "{{STRIPE_LINK_50}}": "#demo-tier-50",
        "{{STRIPE_LINK_200}}": "#demo-tier-200",
        "{{KOFI_LINK}}": "https://ko-fi.com/monai",
        "{{MONERO_ADDRESS}}": "4AdUndXHHZ6cfufTMvppY6JwXNouMBzSkbLYfpAV5Usx3skxNgYeYTRJ5UzqtReoS44qo9mtmXCqY45DJ852K5Jv2684Rge",
    }

    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("Generated preview landing page at %s", output_path)

    return output_path
