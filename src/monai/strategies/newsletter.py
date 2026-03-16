"""Newsletter monetization strategy agent.

Builds email subscriber lists around specific niches, then monetizes via:
- Paid sponsorships
- Premium/paid tiers
- Affiliate links in content
- Driving traffic to other monAI properties

Uses REAL web research to find niches, sponsors, and growth opportunities.
Compounds over time — subscribers are an owned asset.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

NEWSLETTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS newsletters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    niche TEXT NOT NULL,
    description TEXT,
    frequency TEXT DEFAULT 'weekly',        -- daily, weekly, biweekly, monthly
    subscriber_count INTEGER DEFAULT 0,
    platform TEXT DEFAULT 'substack',        -- substack, beehiiv, convertkit, ghost
    monetization TEXT,                       -- JSON: sponsors, premium, affiliate
    status TEXT DEFAULT 'planning',          -- planning, launched, growing, monetizing, paused
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS newsletter_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newsletter_id INTEGER REFERENCES newsletters(id),
    subject TEXT NOT NULL,
    content_path TEXT,                       -- path to the full content file
    status TEXT DEFAULT 'draft',             -- draft, reviewed, scheduled, sent
    open_rate REAL,
    click_rate REAL,
    sponsor_revenue REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Real newsletter platforms to research trending niches
NEWSLETTER_PLATFORMS = [
    {
        "name": "substack",
        "trending_url": "https://substack.com/discover",
        "category_urls": [
            "https://substack.com/discover/category/technology",
            "https://substack.com/discover/category/business",
            "https://substack.com/discover/category/finance",
        ],
    },
    {
        "name": "beehiiv",
        "trending_url": "https://www.beehiiv.com/discover",
    },
]

# Real sponsor discovery platforms
SPONSOR_PLATFORMS = [
    "https://www.sponsorgap.com/",
    "https://swapstack.co/",
    "https://www.paved.com/",
]


class NewsletterAgent(BaseAgent):
    name = "newsletter"
    description = (
        "Builds and monetizes email newsletters. Picks profitable niches using "
        "REAL market research, creates consistent high-quality content, grows "
        "subscriber lists, and monetizes via sponsorships, premium tiers, and "
        "affiliate links."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(NEWSLETTER_SCHEMA)
        self.content_dir = config.data_dir / "newsletter_content"
        self.content_dir.mkdir(parents=True, exist_ok=True)

    def plan(self) -> list[str]:
        newsletters = self.db.execute("SELECT * FROM newsletters WHERE status != 'paused'")
        statuses: dict[str, int] = {}
        for nl in newsletters:
            s = nl["status"] if nl["status"] else "unknown"
            statuses[s] = statuses.get(s, 0) + 1

        # Check for reviewed issues that need publishing (highest priority)
        reviewed_issues = self.db.execute(
            "SELECT COUNT(*) as c FROM newsletter_issues WHERE status = 'reviewed'"
        )
        if reviewed_issues and reviewed_issues[0]["c"] > 0:
            return ["publish_issue"]

        # Check for draft issues that need review before anything else
        draft_issues = self.db.execute(
            "SELECT COUNT(*) as c FROM newsletter_issues WHERE status = 'draft'"
        )
        if draft_issues and draft_issues[0]["c"] > 0:
            return ["review_issue"]

        # Deterministic progression
        if not statuses:
            return ["research_niches"]
        if statuses.get("planning", 0) > 0:
            return ["launch_newsletter"]
        if statuses.get("launched", 0) > 0:
            return ["write_issue"]
        if statuses.get("growing", 0) > 0:
            return ["grow_subscribers"]
        if statuses.get("monetizing", 0) > 0:
            return ["find_sponsors"]

        # All newsletters at final stage — start new one
        return ["research_niches"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting newsletter cycle")
        steps = self.plan()
        results = {}

        step_methods = {
            "research_niches": self._research_niches,
            "plan_newsletter": self._plan_newsletter,
            "launch_newsletter": self._launch_newsletter,
            "write_issue": self._write_issue,
            "review_issue": self._review_issue,
            "publish_issue": self._publish_issue,
            "find_sponsors": self._find_sponsors,
            "grow_subscribers": self._grow_subscribers,
        }

        for step in steps:
            fn = step_methods.get(step)
            if fn:
                results[step] = self.run_step(step, fn)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Find newsletter niches with monetization potential using REAL web data."""
        self.log_action("niche_research", "Browsing real newsletter platforms for trending niches")

        # Browse Substack trending/discover to see what's actually popular
        substack_data = self.browse_and_extract(
            "https://substack.com/discover",
            "Extract trending newsletters and categories from this page. For each "
            "newsletter or category, get:\n"
            "- name: newsletter or category name\n"
            "- niche/topic: what it covers\n"
            "- subscriber_count: if shown\n"
            "- description: brief summary\n"
            "- is_paid: whether it has a paid tier\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"trending\": [{\"name\": str, \"niche\": str, "
            "\"subscriber_count\": str, \"description\": str, \"is_paid\": bool}]}"
        )

        # Browse Substack business/technology categories
        substack_biz = self.browse_and_extract(
            "https://substack.com/discover/category/business",
            "Extract newsletter listings from this business category page. For each "
            "newsletter, get the name, description, subscriber count if shown, and "
            "whether it has paid subscribers.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"newsletters\": [{\"name\": str, \"description\": str, "
            "\"subscribers\": str, \"is_paid\": bool}]}"
        )

        # Browse Beehiiv discover for additional market data
        beehiiv_data = self.browse_and_extract(
            "https://www.beehiiv.com/discover",
            "Extract featured or trending newsletters from this page. For each, get:\n"
            "- name, niche/topic, subscriber_count (if shown), description\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"newsletters\": [{\"name\": str, \"niche\": str, "
            "\"subscribers\": str, \"description\": str}]}"
        )

        # Search for newsletter market gaps and underserved niches
        market_gaps = self.search_web(
            "most profitable newsletter niches 2026 underserved",
            "Extract niche ideas, revenue data, audience sizes, and monetization "
            "strategies mentioned for newsletter businesses. Only include REAL data "
            "visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"niches\": [{\"niche\": str, \"revenue_data\": str, "
            "\"audience_size\": str, \"monetization\": str}]}"
        )

        # Use LLM to analyze real data and pick the best niches
        raw_data = {
            "substack_trending": substack_data,
            "substack_business": substack_biz,
            "beehiiv": beehiiv_data,
            "market_gaps": market_gaps,
        }
        niches = self.think_json(
            "Based on the following REAL newsletter market data, identify 5 "
            "profitable newsletter niches to pursue.\n\n"
            f"Raw research data:\n{json.dumps(raw_data, default=str)[:4000]}\n\n"
            "Requirements:\n"
            "- Audience willing to pay for info (professionals, hobbyists)\n"
            "- Sponsors exist in the space (B2B SaaS, tools, courses)\n"
            "- Can publish weekly with consistent value\n"
            "- Not oversaturated based on the real data above\n"
            "- Can hit 1,000 subscribers in 3 months with good content\n\n"
            "IMPORTANT: Base your analysis on the real data above. Do not invent "
            "market data or subscriber counts.\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"audience\": str, "
            "\"content_angle\": str, \"monetization_path\": str, "
            "\"potential_sponsors\": [str], \"estimated_cpm\": float, "
            "\"platform\": str, \"competition_level\": str, \"source\": str}]}"
        )

        self.share_knowledge(
            "opportunity", "newsletter_niches",
            json.dumps(niches.get("niches", []))[:1000],
            tags=["newsletter", "niches"],
        )
        return niches

    def _plan_newsletter(self) -> dict[str, Any]:
        """Plan a new newsletter from scratch using LLM for creative planning."""
        plan = self.think_json(
            "Design a newsletter to launch. Include:\n"
            "- Name, tagline, niche\n"
            "- Content format (curated links, original analysis, tutorials, etc.)\n"
            "- Publishing schedule\n"
            "- First 4 issue topics\n"
            "- Growth strategy for first 500 subscribers\n"
            "- Monetization timeline\n\n"
            "Return: {\"name\": str, \"tagline\": str, \"niche\": str, "
            "\"format\": str, \"frequency\": str, \"platform\": str, "
            "\"first_issues\": [str], \"growth_strategy\": str, "
            "\"monetization_timeline\": str}"
        )

        name = plan.get("name", "untitled")
        self.db.execute_insert(
            "INSERT OR IGNORE INTO newsletters (name, niche, description, frequency, platform, status) "
            "VALUES (?, ?, ?, ?, ?, 'planning')",
            (name, plan.get("niche", ""), plan.get("tagline", ""),
             plan.get("frequency", "weekly"), plan.get("platform", "substack")),
        )
        self.log_action("newsletter_planned", name)
        return plan

    def _launch_newsletter(self) -> dict[str, Any]:
        """Create the newsletter on a real platform (Substack/Beehiiv) via browser.

        Registers an account, creates the publication, and sets it to 'launched'.
        Without this step, newsletters exist only in DB and never get real subscribers.
        """
        newsletters = self.db.execute(
            "SELECT * FROM newsletters WHERE status = 'planning' LIMIT 1"
        )
        if not newsletters:
            return {"status": "no_newsletters_to_launch"}

        nl = newsletters[0]
        name = nl["name"]
        platform = nl["platform"] or "substack"

        # Ensure we have a platform account
        account_result = self.ensure_platform_account(platform)
        if account_result.get("status") in ("blocked", "error"):
            self.log_action(
                "launch_blocked", name,
                f"{platform} account setup failed: {account_result}"
            )
            # Try Beehiiv as fallback
            if platform != "beehiiv":
                platform = "beehiiv"
                account_result = self.ensure_platform_account("beehiiv")
                if account_result.get("status") in ("blocked", "error"):
                    return {"status": "blocked", "reason": "Both Substack and Beehiiv unavailable"}

        # Create the publication on the platform
        try:
            create_result = self.execute_task(
                f"Create a new newsletter publication on {platform}.\n"
                f"Name: {name}\n"
                f"Description: {nl['description']}\n"
                f"Niche: {nl['niche']}\n"
                f"Frequency: {nl['frequency']}\n\n"
                f"Steps:\n"
                f"1. Go to {platform}.com and navigate to create a new publication\n"
                f"2. Enter the newsletter name and description\n"
                f"3. Choose the appropriate category\n"
                f"4. Set up the welcome email for new subscribers\n"
                f"5. Return the publication URL\n\n"
                f"IMPORTANT: Use ONLY the credentials from your stored {platform} account.\n"
                f"Return: {{\"url\": str, \"status\": str}}",
                f"Launching newsletter '{name}' on {platform}"
            )

            pub_url = create_result.get("url", "")

            if create_result.get("status") == "completed" or pub_url:
                self.db.execute(
                    "UPDATE newsletters SET status = 'launched', platform = ? WHERE name = ?",
                    (platform, name),
                )
                self.log_action("newsletter_launched", f"{name} on {platform}: {pub_url}")
                return {"status": "launched", "platform": platform, "url": pub_url}
            else:
                self.log_action(
                    "newsletter_launch_failed", name,
                    f"Platform creation returned: {create_result}"
                )
                return {"status": "failed", "reason": str(create_result)}

        except Exception as e:
            self.log_action("newsletter_launch_failed", name, str(e)[:300])
            self.learn_from_error(e, f"Launching newsletter '{name}' on {platform}")
            return {"status": "error", "error": str(e)}

    def _write_issue(self) -> dict[str, Any]:
        """Write a newsletter issue for an active newsletter using LLM content creation."""
        newsletters = self.db.execute(
            "SELECT * FROM newsletters WHERE status IN ('launched', 'growing', 'monetizing') LIMIT 1"
        )
        if not newsletters:
            return {"status": "no_active_newsletters"}

        nl = dict(newsletters[0])

        # Research current events/trends in the niche for the issue
        niche_news = self.search_web(
            f"{nl['niche']} latest news trends this week 2026",
            "Extract the latest news, trends, and noteworthy developments mentioned. "
            "Include article titles, key points, and sources.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"news\": [{\"title\": str, \"summary\": str, "
            "\"source\": str}]}"
        )

        # Plan the issue structure
        issue_plan = self.think_json(
            f"Plan a newsletter issue for: {nl['name']}\n"
            f"Niche: {nl['niche']}\n"
            f"Description: {nl['description']}\n\n"
            f"Recent news/trends in this niche:\n"
            f"{json.dumps(niche_news, default=str)[:2000]}\n\n"
            "Plan the issue with:\n"
            "- Attention-grabbing subject line\n"
            "- Opening hook (1-2 sentences)\n"
            "- 3-5 content section topics based on real news above\n"
            "- Call to action\n\n"
            "Return: {\"subject\": str, \"hook\": str, "
            "\"sections\": [{\"heading\": str, \"topic\": str}], "
            "\"cta\": str}"
        )

        # Write the full issue content using LLM
        sections = issue_plan.get("sections", [])
        written_sections = []
        for section in sections:
            content = self.llm.chat(
                [
                    {"role": "system", "content": (
                        f"You are the author of '{nl['name']}', a {nl['niche']} newsletter. "
                        "Write engaging, insightful content that provides genuine value to "
                        "readers. Be opinionated and specific — not generic. Use a conversational "
                        "but knowledgeable tone. The output must be indistinguishable from "
                        "expert human work."
                    )},
                    {"role": "user", "content": (
                        f"Write this newsletter section.\n"
                        f"Heading: {section.get('heading', '')}\n"
                        f"Topic: {section.get('topic', '')}\n"
                        f"Context from real news:\n"
                        f"{json.dumps(niche_news, default=str)[:1000]}\n\n"
                        "Write 2-4 paragraphs of substantive content. Reference real "
                        "developments where relevant."
                    )},
                ],
                model=self.config.llm.model,
            )
            written_sections.append({
                "heading": section.get("heading", ""),
                "content": content,
            })

        # Track API cost
        self.record_expense(
            0.05, "api_cost", f"Newsletter issue for {nl['name']}",
        )

        # Save the full issue content
        issue_data = {
            "subject": issue_plan.get("subject", "Untitled"),
            "hook": issue_plan.get("hook", ""),
            "sections": written_sections,
            "cta": issue_plan.get("cta", ""),
            "newsletter_id": nl["id"],
        }

        safe_subject = "".join(
            c if c.isalnum() or c in " -_" else "" for c in issue_data["subject"]
        ).strip()[:50]
        path = self.content_dir / f"issue_{nl['id']}_{safe_subject}.json"
        path.write_text(json.dumps(issue_data, indent=2))

        self.db.execute_insert(
            "INSERT INTO newsletter_issues (newsletter_id, subject, content_path, status) "
            "VALUES (?, ?, ?, 'draft')",
            (nl["id"], issue_data["subject"], str(path)),
        )
        self.log_action("issue_written", issue_data["subject"])
        return {
            "subject": issue_data["subject"],
            "sections": len(written_sections),
            "content_path": str(path),
        }

    def _review_issue(self) -> dict[str, Any]:
        """Quality gate: review draft newsletter issues before sending."""
        draft_issues = self.db.execute(
            "SELECT ni.*, n.name as newsletter_name FROM newsletter_issues ni "
            "JOIN newsletters n ON ni.newsletter_id = n.id "
            "WHERE ni.status = 'draft' LIMIT 1"
        )
        if not draft_issues:
            return {"status": "no_drafts_to_review"}

        issue = dict(draft_issues[0])
        content_path = issue.get("content_path", "")

        # Load issue content
        import os
        product_data = {"spec": {"title": issue["subject"]}, "content": []}
        if content_path and os.path.exists(content_path):
            try:
                issue_data = json.loads(open(content_path).read())
                product_data["content"] = issue_data.get("sections", [])
            except (json.JSONDecodeError, OSError):
                pass

        result = self.reviewer.review_product(
            strategy=self.name,
            product_name=issue["subject"],
            product_data=product_data,
            product_type="content",
        )

        if result.verdict == "approved":
            self.db.execute(
                "UPDATE newsletter_issues SET status = 'reviewed' WHERE id = ?",
                (issue["id"],),
            )
            self.log_action("issue_reviewed", f"{issue['subject']}: APPROVED")
        elif result.verdict == "rejected":
            self.db.execute(
                "DELETE FROM newsletter_issues WHERE id = ?",
                (issue["id"],),
            )
            self.log_action("issue_rejected", f"{issue['subject']}: REJECTED — will rewrite")
        else:
            revised = self.reviewer.revise_product(product_data, result, "content")
            if content_path and os.path.exists(content_path):
                try:
                    issue_data = json.loads(open(content_path).read())
                    issue_data["revised_content"] = revised
                    open(content_path, "w").write(json.dumps(issue_data, indent=2))
                except (json.JSONDecodeError, OSError):
                    pass
            self.db.execute(
                "UPDATE newsletter_issues SET status = 'reviewed' WHERE id = ?",
                (issue["id"],),
            )
            self.log_action("issue_revised", f"{issue['subject']}: REVISED")

        return result.to_dict()

    def _publish_issue(self) -> dict[str, Any]:
        """Publish reviewed newsletter issues to the actual platform.

        Without this step, issues sit at 'reviewed' status forever and no
        subscribers ever receive them. This publishes to Substack/Beehiiv
        via browser automation.
        """
        reviewed = self.db.execute(
            "SELECT ni.*, n.platform, n.name as newsletter_name "
            "FROM newsletter_issues ni "
            "JOIN newsletters n ON ni.newsletter_id = n.id "
            "WHERE ni.status = 'reviewed' LIMIT 1"
        )
        if not reviewed:
            return {"status": "no_reviewed_issues"}

        issue = dict(reviewed[0])
        subject = issue.get("subject", "Untitled")
        platform = issue.get("platform", "substack")
        newsletter_name = issue.get("newsletter_name", "")

        # Load content from file
        content = ""
        content_path = self.content_dir / f"issue_{issue['newsletter_id']}_{subject.replace(' ', '_')[:30]}.json"
        if content_path.exists():
            try:
                file_data = json.loads(content_path.read_text())
                content = file_data.get("content", "")
            except (json.JSONDecodeError, OSError):
                pass

        if not content:
            # Try from DB if file not found
            content = issue.get("content", "")

        if not content:
            self.log_action("publish_skip", f"No content for issue: {subject}")
            return {"status": "no_content"}

        try:
            result = self.execute_task(
                f"Publish a newsletter issue on {platform}.\n"
                f"Newsletter: {newsletter_name}\n"
                f"Subject line: {subject}\n"
                f"Content (first 2000 chars):\n{content[:2000]}\n\n"
                f"Steps:\n"
                f"1. Go to {platform}.com dashboard for '{newsletter_name}'\n"
                f"2. Create a new post/issue\n"
                f"3. Set the subject line: {subject}\n"
                f"4. Paste the full content\n"
                f"5. Send/publish to all subscribers\n"
                f"6. Return the published URL\n\n"
                f"Return: {{\"url\": str, \"status\": str}}",
                f"Publishing newsletter issue: {subject}",
            )

            if result.get("status") == "completed" or result.get("url"):
                self.db.execute(
                    "UPDATE newsletter_issues SET status = 'sent' WHERE id = ?",
                    (issue["id"],),
                )
                self.log_action(
                    "issue_published",
                    f"{subject} → {result.get('url', platform)}",
                )
                return {"status": "published", "url": result.get("url", "")}
            else:
                self.log_action("issue_publish_failed", f"{subject}: {result}")
                return {"status": "failed", "reason": str(result)}

        except Exception as e:
            self.log_action("issue_publish_failed", f"{subject}: {e}")
            self.learn_from_error(e, f"Publishing newsletter issue '{subject}' on {platform}")
            return {"status": "error", "error": str(e)}

    def _find_sponsors(self) -> dict[str, Any]:
        """Find REAL potential sponsors by browsing sponsor platforms and newsletters."""
        newsletters = self.db.execute(
            "SELECT * FROM newsletters WHERE status IN ('growing', 'monetizing')"
        )
        if not newsletters:
            return {"status": "no_newsletters_ready_for_sponsors"}

        nl = dict(newsletters[0])
        self.log_action("sponsor_research", f"Finding sponsors for {nl['name']}")

        # Browse SponsorGap for real sponsor listings
        sponsorgap_data = self.browse_and_extract(
            "https://www.sponsorgap.com/",
            "Extract any sponsor listings, advertiser names, industries, budget "
            "ranges, and newsletter sponsorship opportunities shown on this page.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"sponsors\": [{\"name\": str, \"industry\": str, "
            "\"budget\": str, \"details\": str}]}"
        )

        # Browse Swapstack for real newsletter sponsorship marketplace data
        swapstack_data = self.browse_and_extract(
            "https://swapstack.co/",
            "Extract any sponsor listings, brands, industries, CPM rates, and "
            "newsletter sponsorship opportunities shown on this page.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"sponsors\": [{\"name\": str, \"industry\": str, "
            "\"cpm\": str, \"details\": str}]}"
        )

        # Browse Paved for real sponsor data
        paved_data = self.browse_and_extract(
            "https://www.paved.com/",
            "Extract any sponsor listings, advertiser names, industries, and "
            "newsletter sponsorship information shown on this page.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"sponsors\": [{\"name\": str, \"industry\": str, "
            "\"details\": str}]}"
        )

        # Search for companies that sponsor newsletters in this niche
        niche_sponsors = self.search_web(
            f"companies sponsoring {nl['niche']} newsletters 2026",
            "Extract company names, products, and any sponsorship details mentioned "
            "for newsletter sponsorships in this niche.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"sponsors\": [{\"company\": str, \"product\": str, "
            "\"sponsorship_details\": str}]}"
        )

        # Browse existing newsletters in the niche to see who sponsors them
        competitor_sponsors = self.search_web(
            f"popular {nl['niche']} newsletter sponsors ads",
            "Extract the names of companies and brands that appear as sponsors "
            "or advertisers in newsletters in this niche. Include the newsletter "
            "name and sponsor/advertiser name.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"sponsor_sightings\": [{\"newsletter\": str, "
            "\"sponsor\": str, \"product\": str}]}"
        )

        # Use LLM to select the most relevant sponsors from real data
        raw_data = {
            "sponsorgap": sponsorgap_data,
            "swapstack": swapstack_data,
            "paved": paved_data,
            "niche_search": niche_sponsors,
            "competitor_sponsors": competitor_sponsors,
        }
        sponsors = self.think_json(
            f"Based on the following REAL sponsor data, find the 5 best potential "
            f"sponsors for my newsletter '{nl['name']}' in the {nl['niche']} niche.\n"
            f"Current subscribers: {nl['subscriber_count']}\n\n"
            f"Raw research data:\n{json.dumps(raw_data, default=str)[:4000]}\n\n"
            "IMPORTANT: Only include sponsors/companies that appeared in the real "
            "data above. Do not invent companies.\n\n"
            "Return: {\"sponsors\": [{\"company\": str, \"product\": str, "
            "\"relevance\": str, \"estimated_cpm\": float, "
            "\"contact_method\": str, \"source\": str}]}"
        )

        # Create a checkout link for sponsorship slots and pitch sponsors
        subscriber_count = nl.get("subscriber_count", 0) or 100
        cpm_rate = 25.0  # Conservative newsletter CPM
        slot_price = round(max(subscriber_count * cpm_rate / 1000, 25.0), 2)

        checkout = self.create_checkout_link(
            amount=slot_price,
            product=f"Newsletter Sponsorship: {nl['name']} ({subscriber_count} subscribers)",
            provider="kofi",
            metadata={"newsletter_id": nl["id"], "type": "sponsorship"},
        )
        checkout_url = checkout.get("checkout_url", "")

        # Reach out to the best sponsors with our pitch + checkout link
        for sponsor in sponsors.get("sponsors", [])[:3]:
            contact = sponsor.get("contact_method", "")
            if not contact or "@" not in contact:
                continue
            try:
                pitch_body = (
                    f"Sponsorship slot in '{nl['name']}' newsletter.\n"
                    f"- {subscriber_count} engaged subscribers in {nl['niche']}\n"
                    f"- Price: €{slot_price:.2f} per issue\n"
                    f"- Includes: dedicated section + link + CTA\n"
                )
                if checkout_url:
                    pitch_body += f"- Book instantly: {checkout_url}\n"

                self.platform_action(
                    "email",
                    f"Send a sponsorship pitch email.\n"
                    f"To: {contact}\n"
                    f"Subject: Sponsorship opportunity — {nl['name']} newsletter\n"
                    f"Body: {pitch_body}\n"
                    f"Keep it professional and concise.",
                    f"Pitching {sponsor.get('company', 'sponsor')}",
                )
                self.log_action("sponsor_pitched", f"{sponsor.get('company', '')} for {nl['name']}")
            except Exception:
                pass

        self.log_action("sponsors_found", f"{len(sponsors.get('sponsors', []))} potential sponsors")
        return sponsors

    def _grow_subscribers(self) -> dict[str, Any]:
        """Execute REAL growth activities using platform actions."""
        newsletters = self.db.execute(
            "SELECT * FROM newsletters WHERE status IN ('launched', 'growing', 'monetizing') LIMIT 1"
        )
        if not newsletters:
            return {"status": "no_active_newsletters"}

        nl = dict(newsletters[0])
        self.log_action("growth_start", f"Running growth tactics for {nl['name']}")

        actions_taken = []

        # 1. Cross-promote on Twitter/X by posting valuable content
        promo_tweet = self.llm.chat(
            [
                {"role": "system", "content": (
                    "You write engaging social media posts that drive newsletter signups. "
                    "Be concise, provide a compelling hook, and include a clear CTA."
                )},
                {"role": "user", "content": (
                    f"Write a Twitter/X post promoting the newsletter '{nl['name']}' "
                    f"about {nl['niche']}. Share a valuable insight from the niche to "
                    f"hook readers, then mention the newsletter. Keep under 280 characters."
                )},
            ],
            model=self.config.llm.model,
        )

        twitter_result = self.platform_action(
            "twitter",
            f"Post this tweet to promote the newsletter:\n\n{promo_tweet}",
            context=f"Newsletter: {nl['name']}, Niche: {nl['niche']}",
        )
        actions_taken.append({
            "action": "twitter_post",
            "status": twitter_result.get("status", "unknown"),
            "content": promo_tweet[:100],
        })

        # 2. Post in relevant Reddit communities
        reddit_post = self.llm.chat(
            [
                {"role": "system", "content": (
                    "You write helpful Reddit posts that provide genuine value. "
                    "Do NOT be promotional — focus on sharing knowledge. "
                    "Mention the newsletter naturally only if relevant."
                )},
                {"role": "user", "content": (
                    f"Write a Reddit post sharing a valuable insight about {nl['niche']}. "
                    f"This should be genuinely useful, not promotional. At the end, "
                    f"mention that you write a newsletter about this topic if people "
                    f"want more. Keep it authentic."
                )},
            ],
            model=self.config.llm.model,
        )

        reddit_result = self.platform_action(
            "reddit",
            f"Find a relevant subreddit for {nl['niche']} and post this content. "
            f"Choose a subreddit where this would be on-topic and valuable.\n\n"
            f"{reddit_post}",
            context=f"Newsletter: {nl['name']}, Niche: {nl['niche']}",
        )
        actions_taken.append({
            "action": "reddit_post",
            "status": reddit_result.get("status", "unknown"),
        })

        # 3. Find and engage with cross-promotion opportunities
        cross_promo = self.search_web(
            f"{nl['niche']} newsletter cross promotion swap recommendations",
            "Extract newsletter names, their topics, subscriber counts, and any "
            "cross-promotion or recommendation swap opportunities mentioned.\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"newsletters\": [{\"name\": str, \"topic\": str, "
            "\"subscribers\": str, \"contact\": str}]}"
        )
        actions_taken.append({
            "action": "cross_promo_research",
            "status": cross_promo.get("status", "unknown"),
            "results": cross_promo,
        })

        # 4. Post on LinkedIn with professional angle
        linkedin_post = self.llm.chat(
            [
                {"role": "system", "content": (
                    "You write professional LinkedIn posts that share industry insights. "
                    "Be thoughtful and data-driven. Include a newsletter CTA at the end."
                )},
                {"role": "user", "content": (
                    f"Write a LinkedIn post sharing a professional insight about "
                    f"{nl['niche']}. This should position you as a thought leader. "
                    f"End with a mention of your newsletter '{nl['name']}' for those "
                    f"who want more insights."
                )},
            ],
            model=self.config.llm.model,
        )

        linkedin_result = self.platform_action(
            "linkedin",
            f"Post this content on LinkedIn:\n\n{linkedin_post}",
            context=f"Newsletter: {nl['name']}, Niche: {nl['niche']}",
        )
        actions_taken.append({
            "action": "linkedin_post",
            "status": linkedin_result.get("status", "unknown"),
        })

        self.log_action("growth_complete", f"Executed {len(actions_taken)} growth actions")
        return {"actions_taken": actions_taken, "newsletter": nl["name"]}
