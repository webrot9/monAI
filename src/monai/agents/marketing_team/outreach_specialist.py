"""OutreachSpecialist — cold outreach, partnerships, influencer collaboration.

Real autonomous capabilities:
- Sends real emails via CommsEngine (SMTP through proxy)
- Executes real LinkedIn/Twitter outreach via platform_action
- Researches real prospects via web scraping before outreach
- Tracks outreach responses and follow-ups in the DB
- Personalizes messages using real prospect data from the web
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

OUTREACH_SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER,
    target_name TEXT,
    target_email TEXT,
    target_platform TEXT,
    target_handle TEXT,
    channel TEXT NOT NULL,               -- email, linkedin, twitter, partnership
    message_body TEXT NOT NULL,
    status TEXT DEFAULT 'pending',       -- pending, sent, delivered, opened, replied, bounced
    follow_up_count INTEGER DEFAULT 0,
    next_follow_up_at TIMESTAMP,
    sent_at TIMESTAMP,
    replied_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class OutreachSpecialist(BaseAgent):
    """Executes personalized outreach campaigns with real email and social actions.

    Sends real emails via CommsEngine, does real LinkedIn/Twitter outreach
    via platform_action, and tracks everything in the DB.
    """

    name = "outreach_specialist"
    description = "Runs personalized cold outreach, partnership proposals, and influencer collaborations."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self._comms = None  # Lazy-loaded
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(OUTREACH_SCHEMA)

    @property
    def comms(self):
        """Lazy-load CommsEngine for real email sending."""
        if self._comms is None:
            from monai.business.comms import CommsEngine
            self._comms = CommsEngine(self.config, self.db)
        return self._comms

    def plan(self) -> list[str]:
        return [
            "Check for pending follow-ups on prior outreach",
            "Research real prospects via web scraping",
            "Craft personalized messages using real prospect data",
            "Send emails via CommsEngine and social via platform_action",
            "Track delivery and responses in DB",
        ]

    def run(self, campaign: dict | None = None, strategy: str = "",
            **kwargs: Any) -> dict[str, Any]:
        """Execute outreach for a campaign with real sending.

        Layer 1: Follow up on prior outreach (check DB)
        Layer 2: Research real prospects via web scraping
        Layer 3: Craft personalized messages with real prospect data
        Layer 4: Send via real channels (email, LinkedIn, Twitter)
        """
        # ── Layer 1: Handle follow-ups on existing outreach ────────
        follow_ups_sent = self._process_follow_ups()

        if not campaign:
            return {"messages_sent": 0, "follow_ups_sent": follow_ups_sent}

        # ── Layer 2: Research real prospects ────────────────────────
        prospects = self._research_prospects(
            campaign.get("target_audience", ""),
            strategy,
        )

        # ── Layer 3: Plan outreach with real prospect data ─────────
        outreach_plan = self.think_json(
            f"Plan outreach for:\n"
            f"Strategy: {strategy}\n"
            f"Campaign: {campaign.get('name', '')}\n"
            f"Target audience: {campaign.get('target_audience', '')}\n\n"
            f"REAL PROSPECT DATA (from web research):\n"
            f"{json.dumps(prospects, default=str)[:1000]}\n\n"
            "Design personalized outreach. NEVER spray and pray. "
            "Each message must reference specific details about the prospect.\n\n"
            "Return JSON: {{\"outreach_sequences\": [{{\"target_name\": str, "
            "\"target_type\": str, "
            "\"channel\": \"email\"|\"linkedin\"|\"twitter\"|\"partnership\", "
            "\"target_email\": str (if known), \"target_handle\": str (if known), "
            "\"subject\": str, \"message_body\": str, "
            "\"personalization_notes\": str, "
            "\"follow_up_days\": int, \"expected_response_rate\": float}}]}}"
        )

        sequences = outreach_plan.get("outreach_sequences", [])

        # ── Layer 4: Send via real channels ────────────────────────
        emails_sent = 0
        social_sent = 0

        for seq in sequences:
            channel = seq.get("channel", "")
            target_name = seq.get("target_name", "Unknown")
            message = seq.get("message_body", "")
            subject = seq.get("subject", f"Regarding {strategy}")

            # Store in outreach_sequences table
            seq_id = self.db.execute_insert(
                "INSERT INTO outreach_sequences "
                "(campaign_id, target_name, target_email, target_platform, "
                "target_handle, channel, message_body, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (campaign.get("id"), target_name,
                 seq.get("target_email", ""), channel,
                 seq.get("target_handle", ""), channel, message),
            )

            sent = False

            if channel == "email" and seq.get("target_email"):
                # Real email via CommsEngine
                sent = self._send_email(
                    seq["target_email"], subject, message, seq_id
                )
                if sent:
                    emails_sent += 1

            elif channel in ("linkedin", "twitter"):
                # Real social outreach via platform_action
                sent = self._send_social_outreach(
                    channel, target_name, seq.get("target_handle", ""),
                    message, seq_id
                )
                if sent:
                    social_sent += 1

            elif channel == "partnership":
                # Partnership proposals via email or platform
                if seq.get("target_email"):
                    sent = self._send_email(
                        seq["target_email"], subject, message, seq_id
                    )
                    if sent:
                        emails_sent += 1

            # Update status and schedule follow-up
            if sent:
                follow_up_days = seq.get("follow_up_days", 3)
                self.db.execute(
                    "UPDATE outreach_sequences SET status = 'sent', sent_at = ?, "
                    "next_follow_up_at = datetime(?, '+' || ? || ' days') WHERE id = ?",
                    (datetime.now().isoformat(), datetime.now().isoformat(),
                     str(follow_up_days), seq_id),
                )

            self.share_knowledge(
                "outreach_template", seq.get("target_type", target_name),
                f"Channel: {channel}. Sent: {sent}. "
                f"Expected response: {seq.get('expected_response_rate', 0):.0%}",
                confidence=0.5,
                tags=["outreach", strategy],
            )

        total_sent = emails_sent + social_sent
        self.log_action("outreach_executed",
                        f"Sent {total_sent} ({emails_sent} email, {social_sent} social) "
                        f"+ {follow_ups_sent} follow-ups for {strategy}")

        return {
            "messages_sent": total_sent,
            "emails_sent": emails_sent,
            "social_sent": social_sent,
            "follow_ups_sent": follow_ups_sent,
            "sequences": sequences,
        }

    # ── Real prospect research ─────────────────────────────────────

    def _research_prospects(self, target_audience: str, strategy: str) -> dict[str, Any]:
        """Research real prospects via web scraping."""
        if not target_audience:
            return {"prospects": []}

        try:
            return self.search_web(
                query=f"{target_audience} {strategy} contacts leaders companies",
                extraction_prompt=(
                    f"Find potential outreach targets matching: '{target_audience}'. "
                    "For each prospect found: name, company/role, any public email or "
                    "social handle, what they're known for, recent activity. "
                    "Return: {\"prospects\": [{\"name\": str, \"role\": str, "
                    "\"company\": str, \"email\": str (if public), "
                    "\"linkedin\": str (if found), \"twitter\": str (if found), "
                    "\"notable_for\": str}]}"
                ),
                num_results=3,
            )
        except Exception as e:
            logger.warning(f"Prospect research failed: {e}")
            return {"prospects": [], "error": str(e)}

    # ── Real email sending ─────────────────────────────────────────

    def _send_email(self, to_email: str, subject: str, body: str,
                    sequence_id: int | None = None) -> bool:
        """Send a real email via CommsEngine."""
        try:
            success = self.comms.send_email(
                to_email=to_email,
                subject=subject,
                body=body,
            )
            if success:
                self.log_action("email_sent", f"To: {to_email}", subject[:100])
            else:
                self.log_action("email_draft", f"To: {to_email} (SMTP not configured)", subject[:100])
            return success
        except Exception as e:
            logger.error(f"Email send failed to {to_email}: {e}")
            if sequence_id:
                self.db.execute(
                    "UPDATE outreach_sequences SET status = 'bounced' WHERE id = ?",
                    (sequence_id,),
                )
            return False

    # ── Real social outreach ───────────────────────────────────────

    def _send_social_outreach(self, platform: str, target_name: str,
                               target_handle: str, message: str,
                               sequence_id: int | None = None) -> bool:
        """Send real outreach via LinkedIn or Twitter using platform_action."""
        try:
            action = (
                f"Send a personalized message to {target_name}"
                f"{' (@' + target_handle + ')' if target_handle else ''}.\n"
                f"Message:\n{message[:2000]}"
            )
            result = self.platform_action(
                platform=platform,
                action_description=action,
                context=f"Outreach to {target_name}",
            )
            sent = result.get("status") != "error"
            self.log_action(f"{platform}_outreach",
                            f"To: {target_name}",
                            f"sent={sent}")
            return sent
        except Exception as e:
            logger.warning(f"{platform} outreach failed to {target_name}: {e}")
            return False

    # ── Follow-up processing ───────────────────────────────────────

    def _process_follow_ups(self) -> int:
        """Process pending follow-ups from prior outreach."""
        due = self.db.execute(
            "SELECT * FROM outreach_sequences "
            "WHERE status = 'sent' AND next_follow_up_at <= datetime('now') "
            "AND follow_up_count < 3 "
            "ORDER BY next_follow_up_at ASC LIMIT 10"
        )

        sent_count = 0
        for seq in due:
            seq = dict(seq)

            # Generate follow-up message
            follow_up_body = self.think(
                f"Write a polite follow-up message to {seq.get('target_name', 'the recipient')}. "
                f"Original message was:\n{seq.get('message_body', '')[:500]}\n\n"
                f"This is follow-up #{seq.get('follow_up_count', 0) + 1}. "
                "Keep it brief, add value, don't be pushy."
            )

            sent = False
            if seq.get("channel") == "email" and seq.get("target_email"):
                sent = self._send_email(
                    seq["target_email"],
                    f"Re: Following up",
                    follow_up_body,
                )
            elif seq.get("channel") in ("linkedin", "twitter"):
                sent = self._send_social_outreach(
                    seq["channel"], seq.get("target_name", ""),
                    seq.get("target_handle", ""), follow_up_body,
                )

            if sent:
                self.db.execute(
                    "UPDATE outreach_sequences SET follow_up_count = follow_up_count + 1, "
                    "next_follow_up_at = datetime('now', '+3 days') WHERE id = ?",
                    (seq["id"],),
                )
                sent_count += 1

        return sent_count
