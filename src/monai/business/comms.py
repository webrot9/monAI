"""Communications engine — handles all outbound/inbound messaging."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from monai.config import Config
from monai.db.database import Database

logger = logging.getLogger(__name__)


class CommsEngine:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: str = "",
        contact_id: int | None = None,
        project_id: int | None = None,
    ) -> bool:
        """Send an email and log it."""
        cfg = self.config.comms

        if not cfg.smtp_host:
            logger.warning("SMTP not configured — email not sent")
            self._log_message(contact_id, project_id, "outbound", "email",
                              subject, body, "draft")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{cfg.from_name} <{cfg.from_email}>"
        msg["To"] = to_email
        msg.attach(MIMEText(body, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))

        try:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
                server.starttls()
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(msg)
            self._log_message(contact_id, project_id, "outbound", "email",
                              subject, body, "sent")
            logger.info(f"Email sent to {to_email}: {subject}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            self._log_message(contact_id, project_id, "outbound", "email",
                              subject, body, "failed")
            return False

    def log_platform_message(
        self,
        contact_id: int,
        channel: str,
        body: str,
        direction: str = "outbound",
        subject: str = "",
        project_id: int | None = None,
    ):
        """Log a message sent/received on a platform (Upwork, Fiverr, etc.)."""
        self._log_message(contact_id, project_id, direction, channel, subject, body, "sent")

    def get_conversation(self, contact_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE contact_id = ? ORDER BY created_at ASC",
            (contact_id,),
        )
        return [dict(r) for r in rows]

    def get_unread_inbound(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE direction = 'inbound' AND status != 'read' "
            "ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]

    def _log_message(self, contact_id: int | None, project_id: int | None,
                     direction: str, channel: str, subject: str, body: str, status: str):
        self.db.execute_insert(
            "INSERT INTO messages (contact_id, project_id, direction, channel, subject, body, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contact_id, project_id, direction, channel, subject, body, status),
        )
