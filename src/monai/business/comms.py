"""Communications engine — handles all outbound/inbound messaging.

The agents communicate professionally under their own business identity.
They are NOT anonymous to clients — they present themselves as a real business.

HOWEVER: the underlying network connection (SMTP, HTTP) is routed through
the proxy so the creator's real IP never appears in connection logs or
email headers. The creator remains invisible; the agent is the public face.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)


class CommsEngine:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._anonymizer = get_anonymizer(config)

    def _create_smtp_connection(self, host: str, port: int) -> smtplib.SMTP:
        """Create SMTP connection routed through proxy to hide creator's IP.

        The email itself is professional (agent's real identity) — only the
        network-level connection goes through proxy so the SMTP server sees
        the proxy IP, not the creator's home IP.
        """
        proxy_url = self._anonymizer.get_proxy_url()
        if proxy_url and "socks5" in proxy_url:
            parts = proxy_url.replace("socks5://", "").split(":")
            proxy_host = parts[0]
            proxy_port = int(parts[1]) if len(parts) > 1 else 9050

            try:
                import socks
                socks.setdefaultproxy(socks.SOCKS5, proxy_host, proxy_port)
                socks.wrapmodule(smtplib)
                logger.info(f"SMTP routed through proxy {proxy_host}:{proxy_port}")
            except ImportError:
                logger.warning("PySocks not installed — SMTP connection is direct")

        return smtplib.SMTP(host, port)

    def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: str = "",
        contact_id: int | None = None,
        project_id: int | None = None,
    ) -> bool:
        """Send a professional email under the agent's identity.

        The email looks completely normal to the recipient — professional,
        from a real business. Network connection goes through proxy to
        protect the creator's IP.
        """
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
            with self._create_smtp_connection(cfg.smtp_host, cfg.smtp_port) as server:
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
