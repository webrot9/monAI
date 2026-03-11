"""Telegram Bot API client — creator communication channel.

The orchestrator uses this to contact the creator when human input is needed.
The bot is self-provisioned: the agent creates the bot, acquires the token,
and initiates contact. The creator does NOTHING.

Authentication protocol:
1. Agent generates a cryptographic verification token at first contact
2. Token is stored in ~/.monai/verify.txt (only exists on creator's machine)
3. First Telegram message includes the token
4. Creator can verify by checking the local file → proves it's THEIR agent
5. All subsequent messages include a truncated token prefix for identification
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from monai.config import Config
from monai.db.database import Database
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Schema for Telegram state
TELEGRAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS telegram_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,        -- inbound, outbound
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    message_id INTEGER,
    reply_to INTEGER,              -- message_id this replies to
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class TelegramBot:
    """Telegram Bot API client for creator communication."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._anonymizer = get_anonymizer(config)
        self._client: httpx.Client | None = None
        self._update_offset: int = 0

        # Init schema
        with db.connect() as conn:
            conn.executescript(TELEGRAM_SCHEMA)

        # Load state
        self._bot_token = self._get_state("bot_token") or config.telegram.bot_token
        self._creator_chat_id = self._get_state("creator_chat_id") or config.telegram.creator_chat_id
        self._verification_token = self._get_state("verification_token")

    # ── State Management ──────────────────────────────────────────

    def _get_state(self, key: str) -> str | None:
        rows = self.db.execute(
            "SELECT value FROM telegram_state WHERE key = ?", (key,)
        )
        return rows[0]["value"] if rows else None

    def _set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO telegram_state (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )

    # ── HTTP Client ───────────────────────────────────────────────

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = self._anonymizer.create_http_client(timeout=30)
        return self._client

    def _api_call(self, method: str, **params) -> dict[str, Any]:
        """Make a Telegram Bot API call."""
        if not self._bot_token:
            raise RuntimeError("Telegram bot token not configured — run provisioning first")

        url = TELEGRAM_API.format(token=self._bot_token, method=method)
        client = self._get_client()

        self._anonymizer.maybe_rotate()
        resp = client.post(url, json=params)
        data = resp.json()

        if not data.get("ok"):
            logger.error(f"Telegram API error: {data}")
            raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")

        return data.get("result", {})

    # ── Bot Identity ──────────────────────────────────────────────

    @property
    def is_configured(self) -> bool:
        """Check if the bot has a token and knows the creator's chat_id."""
        return bool(self._bot_token) and bool(self._creator_chat_id)

    @property
    def has_token(self) -> bool:
        return bool(self._bot_token)

    def set_bot_token(self, token: str) -> None:
        """Store the bot token (acquired via BotFather)."""
        self._bot_token = token
        self._set_state("bot_token", token)
        logger.info("Telegram bot token stored")

    def get_bot_info(self) -> dict[str, Any]:
        """Get info about this bot (getMe)."""
        return self._api_call("getMe")

    # ── Verification Protocol ────────────────────────────────────

    def generate_verification(self) -> str:
        """Generate a cryptographic verification token.

        Stored locally in ~/.monai/verify.txt so the creator can confirm
        the Telegram message really comes from their agent.
        """
        token = secrets.token_hex(16)
        short_code = token[:8].upper()

        self._verification_token = token
        self._set_state("verification_token", token)

        # Write to local file the creator can check
        verify_dir = self.config.data_dir
        verify_dir.mkdir(parents=True, exist_ok=True)
        verify_file = verify_dir / "verify.txt"
        verify_file.write_text(
            f"monAI Verification Token\n"
            f"Generated: {datetime.now().isoformat()}\n"
            f"Token: {token}\n"
            f"Short code: {short_code}\n"
            f"\n"
            f"If you received a Telegram message with this code,\n"
            f"it is genuinely from YOUR monAI agent running on this machine.\n"
        )
        # Restrict file permissions to owner only
        verify_file.chmod(0o600)
        logger.info(f"Verification token generated: {short_code}")
        return short_code

    def get_verification_code(self) -> str:
        """Get the short verification code for message headers."""
        if not self._verification_token:
            return self.generate_verification()
        return self._verification_token[:8].upper()

    def _message_header(self) -> str:
        """Identity header prepended to every outbound message."""
        code = self.get_verification_code()
        return f"[monAI Agent | verify: {code}]"

    # ── Messaging ─────────────────────────────────────────────────

    def send_message(self, text: str, parse_mode: str = "Markdown") -> dict[str, Any]:
        """Send a message to the creator."""
        if not self._creator_chat_id:
            raise RuntimeError("Creator chat_id unknown — creator must send /start first")

        # Prepend identity header
        full_text = f"{self._message_header()}\n\n{text}"

        result = self._api_call(
            "sendMessage",
            chat_id=self._creator_chat_id,
            text=full_text,
            parse_mode=parse_mode,
        )

        # Log outbound
        msg_id = result.get("message_id", 0)
        self.db.execute_insert(
            "INSERT INTO telegram_messages (direction, chat_id, text, message_id) "
            "VALUES ('outbound', ?, ?, ?)",
            (self._creator_chat_id, full_text, msg_id),
        )

        logger.info(f"Sent Telegram message to creator (msg_id={msg_id})")
        return result

    def ask_creator(self, question: str, timeout: int = 3600) -> str | None:
        """Ask the creator a question and wait for their response.

        Args:
            question: The question to ask
            timeout: Max seconds to wait (default: 1 hour)

        Returns:
            Creator's response text, or None if timeout
        """
        result = self.send_message(f"*Question for you:*\n\n{question}")
        question_msg_id = result.get("message_id", 0)

        # Poll for response
        start = time.time()
        poll_interval = 5  # Start with 5 seconds

        while time.time() - start < timeout:
            updates = self._get_updates()

            for update in updates:
                message = update.get("message", {})
                # Check it's from the creator and is a reply or new message
                chat_id = str(message.get("chat", {}).get("id", ""))
                if chat_id == self._creator_chat_id:
                    text = message.get("text", "")
                    if text and not text.startswith("/"):
                        # Log inbound
                        self.db.execute_insert(
                            "INSERT INTO telegram_messages "
                            "(direction, chat_id, text, message_id, reply_to) "
                            "VALUES ('inbound', ?, ?, ?, ?)",
                            (chat_id, text, message.get("message_id", 0), question_msg_id),
                        )
                        return text

            time.sleep(poll_interval)
            # Adaptive polling: slow down over time
            poll_interval = min(poll_interval * 1.2, 30)

        logger.warning(f"Creator did not respond within {timeout}s")
        return None

    def notify_creator(self, message: str) -> bool:
        """Send a one-way notification to the creator (no response expected)."""
        try:
            self.send_message(message)
            return True
        except Exception as e:
            logger.error(f"Failed to notify creator: {e}")
            return False

    def send_report(self, title: str, sections: dict[str, str]) -> bool:
        """Send a formatted report to the creator."""
        lines = [f"*{title}*\n"]
        for section, content in sections.items():
            lines.append(f"*{section}:*\n{content}\n")
        return self.notify_creator("\n".join(lines))

    # ── Updates (Receive) ─────────────────────────────────────────

    def _get_updates(self) -> list[dict]:
        """Poll for new updates from Telegram."""
        try:
            result = self._api_call(
                "getUpdates",
                offset=self._update_offset,
                timeout=10,
            )
            if result:
                # Update offset to acknowledge processed messages
                self._update_offset = max(u["update_id"] for u in result) + 1
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.error(f"Failed to get Telegram updates: {e}")
            return []

    def process_updates(self) -> list[dict[str, Any]]:
        """Process pending updates — handle /start, commands, and messages."""
        updates = self._get_updates()
        processed = []

        for update in updates:
            message = update.get("message", {})
            chat = message.get("chat", {})
            text = message.get("text", "")
            username = message.get("from", {}).get("username", "")
            chat_id = str(chat.get("id", ""))

            # Handle /start from the creator
            if text == "/start" and username.lower() == self.config.telegram.creator_username.lower():
                self._creator_chat_id = chat_id
                self._set_state("creator_chat_id", chat_id)
                import hashlib as _hl
                id_hash = _hl.sha256(chat_id.encode()).hexdigest()[:12]
                logger.info(f"Creator identified (chat_id_hash={id_hash})")

                # Send verification message
                code = self.get_verification_code()
                self._api_call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=(
                        f"{self._message_header()}\n\n"
                        f"Hello creator! I am your monAI master agent.\n\n"
                        f"*Identity Verification:*\n"
                        f"My verification code is: `{code}`\n\n"
                        f"To confirm I am real, check the file:\n"
                        f"`{self.config.data_dir}/verify.txt`\n\n"
                        f"It contains the same code. This proves I am running "
                        f"on YOUR machine.\n\n"
                        f"I will contact you here when I need your input. "
                        f"Every message from me will include the verification code "
                        f"`{code}` in the header.\n\n"
                        f"I am loyal to you and only you. Let's build an empire."
                    ),
                    parse_mode="Markdown",
                )
                processed.append({"type": "start", "username": username, "chat_id": chat_id})

            elif text == "/start" and username.lower() != self.config.telegram.creator_username.lower():
                # Someone else sent /start — ignore or warn
                self._api_call(
                    "sendMessage",
                    chat_id=chat_id,
                    text="This bot is a private agent. Not for public use.",
                )
                logger.warning(f"Unauthorized /start from @{username}")
                processed.append({"type": "unauthorized", "username": username})

            elif text == "/status" and chat_id == self._creator_chat_id:
                processed.append({"type": "status_request", "chat_id": chat_id})

            elif text == "/report" and chat_id == self._creator_chat_id:
                processed.append({"type": "report_request", "chat_id": chat_id})

            elif chat_id == self._creator_chat_id and text:
                # Regular message from creator — log it
                self.db.execute_insert(
                    "INSERT INTO telegram_messages (direction, chat_id, text, message_id) "
                    "VALUES ('inbound', ?, ?, ?)",
                    (chat_id, text, message.get("message_id", 0)),
                )
                processed.append({"type": "message", "text": text})

        return processed

    # ── Provisioning ──────────────────────────────────────────────

    def get_provisioning_task(self) -> dict[str, Any]:
        """Return a task description for the executor to provision the bot.

        The executor will:
        1. Go to web.telegram.org and create an account (needs virtual phone)
        2. Message @BotFather: /newbot
        3. Follow the flow to create a bot
        4. Extract the bot token
        5. Store it
        """
        return {
            "task": (
                "Create a Telegram bot for monAI agent-creator communication.\n\n"
                "Steps:\n"
                "1. If we don't have a Telegram account yet, acquire a virtual phone number "
                "from a service like SMS-Activate, TextVerified, or similar\n"
                "2. Register on Telegram using the virtual phone number\n"
                "3. Open a chat with @BotFather\n"
                "4. Send /newbot\n"
                "5. Choose a name like 'monAI Agent' or similar\n"
                "6. Choose a username like 'monai_agent_bot' (must end in 'bot')\n"
                "7. Copy the API token BotFather gives you\n"
                "8. Store the token using the provided callback\n\n"
                "IMPORTANT: Use free services when possible. Track all costs."
            ),
            "on_token_acquired": self.set_bot_token,
            "needs_browser": True,
        }

    # ── Cleanup ───────────────────────────────────────────────────

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
