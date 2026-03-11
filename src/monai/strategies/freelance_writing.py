"""Freelance writing strategy agent.

Finds REAL writing gigs on platforms, bids on them, delivers content,
and handles the full client cycle. Uses browser automation to interact
with real freelance platforms.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.business.crm import CRM
from monai.business.comms import CommsEngine
from monai.business.invoicing import Invoicing
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

# Real freelance platforms to prospect on
FREELANCE_PLATFORMS = [
    {
        "name": "upwork",
        "search_url": "https://www.upwork.com/nx/search/jobs/?q=writing&sort=recency",
        "categories": ["blog posts", "articles", "copywriting", "technical writing", "SEO content"],
    },
    {
        "name": "fiverr",
        "search_url": "https://www.fiverr.com/search/gigs?query=writing&source=top-bar",
        "categories": ["blog posts", "articles", "copywriting", "product descriptions"],
    },
    {
        "name": "freelancer",
        "search_url": "https://www.freelancer.com/jobs/writing/",
        "categories": ["content writing", "article writing", "copywriting", "ghostwriting"],
    },
    {
        "name": "problogger",
        "search_url": "https://problogger.com/jobs/",
        "categories": ["blog writing", "content creation"],
    },
]


class FreelanceWritingAgent(BaseAgent):
    name = "freelance_writing"
    description = (
        "Finds freelance writing opportunities on REAL platforms, creates proposals, "
        "delivers high-quality content, and manages client relationships."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.crm = CRM(db)
        self.comms = CommsEngine(config, db)
        self.invoicing = Invoicing(config, db)

    def plan(self) -> list[str]:
        """Plan the next actions for this strategy."""
        pipeline = self.crm.get_pipeline_summary()
        context = json.dumps(pipeline)

        plan = self.think_json(
            "Given the current client pipeline, plan my next actions. "
            "Return: {\"steps\": [str]}. "
            "Possible actions: prospect_platforms, send_proposals, "
            "write_content, deliver_work, send_invoice, follow_up.",
            context=context,
        )
        steps = plan.get("steps", ["prospect_platforms"])
        self.log_action("plan", f"Planned {len(steps)} steps", json.dumps(steps))
        return steps

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Execute one cycle of the freelance writing strategy."""
        self.log_action("run_start", "Starting freelance writing cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "prospect_platforms":
                results["prospect"] = self._prospect()
            elif step == "send_proposals":
                results["proposals"] = self._send_proposals()
            elif step == "write_content":
                results["content"] = self._write_content()
            elif step == "deliver_work":
                results["delivery"] = self._deliver_work()
            elif step == "send_invoice":
                results["invoice"] = self._send_invoices()
            elif step == "follow_up":
                results["followup"] = self._follow_up()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _prospect(self) -> dict[str, Any]:
        """Find REAL writing opportunities by browsing freelance platforms."""
        all_jobs = []

        for platform in FREELANCE_PLATFORMS:
            # Ensure we have an account on this platform
            self.ensure_platform_account(platform["name"])

            # Browse the real platform and extract real job listings
            result = self.browse_and_extract(
                platform["search_url"],
                "Extract job listings from this page. For each job, get:\n"
                "- title: the job title\n"
                "- description: brief description of what's needed\n"
                "- budget: the posted budget or price range (if visible)\n"
                "- client: client name or username (if visible)\n"
                "- url: the direct link to the job posting\n"
                "- posted: when it was posted (if visible)\n\n"
                "Return JSON: {\"jobs\": [{\"title\": str, \"description\": str, "
                "\"budget\": str, \"client\": str, \"url\": str, \"posted\": str}]}\n"
                "Only include REAL jobs visible on the page. Do NOT invent any."
            )

            if result.get("status") == "completed":
                job_data = result.get("result", {})
                if isinstance(job_data, str):
                    try:
                        job_data = json.loads(job_data)
                    except (json.JSONDecodeError, TypeError):
                        job_data = {}

                jobs = job_data.get("jobs", []) if isinstance(job_data, dict) else []
                for job in jobs:
                    job["platform"] = platform["name"]
                    all_jobs.append(job)

        # Store real jobs in CRM
        for job in all_jobs:
            contact_id = self.crm.add_contact(
                name=job.get("title", "Unknown Job"),
                platform=job.get("platform", "unknown"),
                source_strategy=self.name,
                notes=json.dumps(job),
            )
            self.crm.update_stage(contact_id, "prospect")

        self.log_action("prospect", f"Found {len(all_jobs)} real opportunities")
        return {"jobs_found": len(all_jobs), "platforms_searched": len(FREELANCE_PLATFORMS)}

    def _send_proposals(self) -> dict[str, Any]:
        """Generate and submit real proposals on platforms."""
        prospects = self.crm.get_contacts_by_stage("prospect")
        sent = 0

        for prospect in prospects[:5]:  # Process up to 5 at a time
            notes = json.loads(prospect.get("notes", "{}") or "{}")
            platform = prospect.get("platform", "upwork")
            job_url = notes.get("url", "")

            # Generate a compelling proposal tailored to the real job
            proposal = self.think(
                f"Write a compelling, personalized proposal for this REAL job posting. "
                f"Be professional, highlight relevant skills, and propose a fair price.\n\n"
                f"Job: {json.dumps(notes)}"
            )

            if job_url:
                # Submit the proposal on the actual platform
                result = self.platform_action(
                    platform,
                    f"Navigate to {job_url} and submit this proposal:\n\n{proposal}\n\n"
                    "Find the 'Submit Proposal' or 'Apply' button and submit it. "
                    "If asked for a bid amount, use a competitive rate based on the job description.",
                    context=proposal,
                )

                if result.get("status") == "completed":
                    sent += 1
                    self.crm.update_stage(prospect["id"], "contacted")
                else:
                    self.log_action("proposal_failed",
                                    f"Failed to submit on {platform}: {result.get('reason', 'unknown')}")
            else:
                # No direct URL — log the proposal for manual follow-up via comms
                self.comms.log_platform_message(
                    contact_id=prospect["id"],
                    channel=platform,
                    body=proposal,
                    direction="outbound",
                    subject=f"Proposal: {prospect['name']}",
                )
                self.crm.update_stage(prospect["id"], "contacted")
                sent += 1

        self.log_action("send_proposals", f"Sent {sent} proposals")
        return {"proposals_sent": sent}

    def _write_content(self) -> dict[str, Any]:
        """Write content for accepted projects."""
        projects = self.db.execute(
            "SELECT p.*, c.name as client_name FROM projects p "
            "JOIN contacts c ON p.contact_id = c.id "
            "WHERE p.strategy_id = (SELECT id FROM strategies WHERE name = ?) "
            "AND p.status = 'in_progress'",
            (self.name,),
        )
        written = 0
        for project in projects:
            p = dict(project)
            content = self.llm.chat(
                [
                    {"role": "system", "content": "You are a professional freelance writer. "
                     "Write high-quality, engaging content that exceeds client expectations. "
                     "The output must be indistinguishable from expert human work."},
                    {"role": "user", "content": f"Write the following:\n{p['description']}"},
                ],
                model=self.config.llm.model,  # Use full model for quality
            )
            # Track API cost as expense
            self.record_expense(
                0.05, "api_cost", f"Content generation for project {p['id']}",
                project_id=p["id"],
            )
            self.log_action("write_content", f"Wrote content for project {p['id']}")
            written += 1

        return {"pieces_written": written}

    def _deliver_work(self) -> dict[str, Any]:
        """Deliver completed work to clients on their platforms."""
        deliverable_projects = self.db.execute(
            "SELECT p.*, c.name as client_name, c.platform as client_platform, "
            "c.notes as client_notes FROM projects p "
            "JOIN contacts c ON p.contact_id = c.id "
            "WHERE p.strategy_id = (SELECT id FROM strategies WHERE name = ?) "
            "AND p.status = 'written'",
            (self.name,),
        )
        delivered = 0

        for project in deliverable_projects:
            p = dict(project)
            platform = p.get("client_platform", "upwork")
            client_notes = json.loads(p.get("client_notes", "{}") or "{}")
            job_url = client_notes.get("url", "")

            # Read the written content from storage
            content_path = self.config.data_dir / "deliverables" / f"project_{p['id']}.txt"
            content = ""
            if content_path.exists():
                content = content_path.read_text()

            if job_url or platform:
                # Deliver via platform
                result = self.platform_action(
                    platform,
                    f"Deliver completed work for this project.\n"
                    f"Job: {p.get('title', 'Unknown')}\n"
                    f"Client: {p.get('client_name', 'Unknown')}\n"
                    f"Navigate to the project/job page and submit the deliverable. "
                    f"Upload the completed work or paste it in the submission form. "
                    f"Mark the milestone as complete if applicable.",
                    context=f"Deliverable content (first 2000 chars):\n{content[:2000]}",
                )

                if result.get("status") == "completed":
                    delivered += 1
                    self.db.execute(
                        "UPDATE projects SET status = 'delivered' WHERE id = ?",
                        (p["id"],),
                    )
                    self.log_action("deliver", f"Delivered project {p['id']} on {platform}")
                else:
                    self.log_action("deliver_failed",
                                    f"Failed delivery on {platform}: {result.get('reason', '')}")
            else:
                self.log_action("deliver_skip",
                                f"No platform URL for project {p['id']}, needs manual delivery")

        self.log_action("deliver", f"Delivered {delivered} projects")
        return {"delivered": delivered}

    def _send_invoices(self) -> dict[str, Any]:
        """Generate and send invoices for delivered work."""
        delivered = self.db.execute(
            "SELECT p.*, c.name as client_name, c.email as client_email FROM projects p "
            "JOIN contacts c ON p.contact_id = c.id "
            "WHERE p.status = 'delivered' AND p.paid_amount = 0"
        )
        invoiced = 0
        for project in delivered:
            p = dict(project)
            if p.get("client_email"):
                invoice = self.invoicing.create_invoice(
                    client_name=p["client_name"],
                    client_email=p["client_email"],
                    items=[{"description": p["title"], "amount": p["quoted_amount"] or 0}],
                    project_id=p["id"],
                )
                self.log_action("invoice", f"Invoiced {p['client_name']}", invoice["invoice_number"])
                invoiced += 1
        return {"invoices_sent": invoiced}

    def _follow_up(self) -> dict[str, Any]:
        """Follow up with contacted prospects who haven't responded."""
        contacted = self.crm.get_contacts_by_stage("contacted")
        followed = 0
        for contact in contacted[:3]:
            notes = json.loads(contact.get("notes", "{}") or "{}")
            platform = contact.get("platform", "upwork")
            job_url = notes.get("url", "")

            followup = self.think(
                f"Write a friendly follow-up message for a prospect I sent a proposal to. "
                f"Be brief and add value. Contact: {contact['name']}"
            )

            if job_url:
                # Send follow-up on the actual platform
                self.platform_action(
                    platform,
                    f"Send a follow-up message to the client for job: {contact['name']}\n"
                    f"Navigate to the job/conversation and send this message:\n\n{followup}",
                    context=followup,
                )
            else:
                self.comms.log_platform_message(
                    contact_id=contact["id"],
                    channel=platform,
                    body=followup,
                    direction="outbound",
                    subject=f"Following up: {contact['name']}",
                )
            followed += 1

        self.log_action("follow_up", f"Followed up with {followed} contacts")
        return {"followed_up": followed}
