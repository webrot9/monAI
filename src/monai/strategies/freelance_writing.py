"""Freelance writing strategy agent.

Finds writing gigs, bids on them, delivers content, and handles the full cycle.
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


class FreelanceWritingAgent(BaseAgent):
    name = "freelance_writing"
    description = (
        "Finds freelance writing opportunities, creates proposals, "
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
        """Find new writing opportunities to bid on."""
        opportunities = self.think_json(
            "Generate 3 realistic freelance writing job postings I should target. "
            "Focus on: blog posts, articles, copywriting, technical writing, SEO content. "
            "Return: {\"jobs\": [{\"title\": str, \"platform\": str, \"budget_range\": str, "
            "\"description\": str, \"ideal_bid\": float}]}"
        )
        jobs = opportunities.get("jobs", [])
        for job in jobs:
            contact_id = self.crm.add_contact(
                name=job.get("title", "Unknown"),
                platform=job.get("platform", "upwork"),
                source_strategy=self.name,
                notes=json.dumps(job),
            )
            self.crm.update_stage(contact_id, "prospect")
        self.log_action("prospect", f"Found {len(jobs)} opportunities")
        return {"jobs_found": len(jobs)}

    def _send_proposals(self) -> dict[str, Any]:
        """Generate and send proposals to prospects."""
        prospects = self.crm.get_contacts_by_stage("prospect")
        sent = 0

        for prospect in prospects[:5]:  # Process up to 5 at a time
            notes = json.loads(prospect.get("notes", "{}") or "{}")
            proposal = self.think(
                f"Write a compelling, personalized proposal for this job. "
                f"Be professional, highlight relevant skills, and propose a fair price.\n\n"
                f"Job: {json.dumps(notes)}"
            )
            self.comms.log_platform_message(
                contact_id=prospect["id"],
                channel=prospect.get("platform", "upwork"),
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
                     "Write high-quality, engaging content that exceeds client expectations."},
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
        """Deliver completed work to clients."""
        # Placeholder — would integrate with platform APIs
        self.log_action("deliver", "Checking for deliverables")
        return {"delivered": 0}

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
            followup = self.think(
                f"Write a friendly follow-up message for a prospect I sent a proposal to. "
                f"Be brief and add value. Contact: {contact['name']}"
            )
            self.comms.log_platform_message(
                contact_id=contact["id"],
                channel=contact.get("platform", "email"),
                body=followup,
                direction="outbound",
                subject=f"Following up: {contact['name']}",
            )
            followed += 1
        self.log_action("follow_up", f"Followed up with {followed} contacts")
        return {"followed_up": followed}
