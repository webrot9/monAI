"""Telegram Channel integration — post content to public channels.

Uses the Telegram Bot API to post formatted messages to channels.
The bot must be added as an admin to the target channel.

Supports: text, photos with captions, message pinning, channel stats.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from monai.config import Config
from monai.db.database import Database
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

CHANNEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_username TEXT UNIQUE NOT NULL,
    bot_token TEXT NOT NULL,
    brand TEXT NOT NULL,
    description TEXT,
    subscriber_count INTEGER DEFAULT 0,
    posts_total INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_username TEXT NOT NULL,
    message_id INTEGER,
    content_type TEXT NOT NULL,      -- deal, promo, growth, announcement
    title TEXT,
    text TEXT NOT NULL,
    affiliate_url TEXT,
    product_id TEXT,
    clicks INTEGER DEFAULT 0,
    status TEXT DEFAULT 'posted',     -- posted, pinned, deleted
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cp_channel ON channel_posts(channel_username, created_at);
"""


class TelegramChannelClient:
    """Post content to Telegram channels via Bot API."""

    # Rate limit: Telegram allows ~20 messages/minute to channels
    MIN_POST_INTERVAL = 3.0  # seconds between posts

    def __init__(self, config: Config, db: Database,
                 bot_token: str, channel_username: str):
        """
        Args:
            bot_token: Bot API token (bot must be channel admin)
            channel_username: Channel @username (e.g. "@mydeals")
        """
        self.config = config
        self.db = db
        self.bot_token = bot_token
        self.channel_username = channel_username
        self._anonymizer = get_anonymizer(config)
        self._last_post_time = 0.0

        with db.connect() as conn:
            conn.executescript(CHANNEL_SCHEMA)

    def _api_call(self, method: str, **params) -> dict[str, Any]:
        """Make a Telegram Bot API call."""
        url = TELEGRAM_API.format(token=self.bot_token, method=method)
        client = self._anonymizer.create_http_client(timeout=30)
        resp = client.post(url, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramChannelError(
                f"API error: {data.get('description', 'unknown')}"
            )
        return data.get("result", {})

    def _rate_limit(self) -> None:
        """Enforce minimum interval between posts."""
        elapsed = time.time() - self._last_post_time
        if elapsed < self.MIN_POST_INTERVAL:
            time.sleep(self.MIN_POST_INTERVAL - elapsed)
        self._last_post_time = time.time()

    def post_message(self, text: str, parse_mode: str = "HTML",
                     disable_preview: bool = False) -> dict[str, Any]:
        """Post a text message to the channel.

        Returns: {"message_id": int, "chat": {...}, ...}
        """
        self._rate_limit()
        result = self._api_call(
            "sendMessage",
            chat_id=self.channel_username,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_preview,
        )
        self._record_post(result.get("message_id"), "deal", text)
        return result

    def post_photo(self, photo_url: str, caption: str = "",
                   parse_mode: str = "HTML") -> dict[str, Any]:
        """Post a photo with caption to the channel."""
        self._rate_limit()
        result = self._api_call(
            "sendPhoto",
            chat_id=self.channel_username,
            photo=photo_url,
            caption=caption,
            parse_mode=parse_mode,
        )
        self._record_post(result.get("message_id"), "deal", caption)
        return result

    def pin_message(self, message_id: int,
                    disable_notification: bool = True) -> bool:
        """Pin a message in the channel."""
        try:
            self._api_call(
                "pinChatMessage",
                chat_id=self.channel_username,
                message_id=message_id,
                disable_notification=disable_notification,
            )
            return True
        except TelegramChannelError:
            return False

    def get_member_count(self) -> int:
        """Get channel subscriber count."""
        try:
            result = self._api_call(
                "getChatMemberCount",
                chat_id=self.channel_username,
            )
            count = int(result) if isinstance(result, (int, str)) else 0
            # Update DB
            self.db.execute(
                "UPDATE telegram_channels SET subscriber_count = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE channel_username = ?",
                (count, self.channel_username),
            )
            return count
        except TelegramChannelError:
            return 0

    def get_channel_info(self) -> dict[str, Any]:
        """Get channel metadata."""
        try:
            return self._api_call("getChat", chat_id=self.channel_username)
        except TelegramChannelError as e:
            return {"error": str(e)}

    def _record_post(self, message_id: int | None, content_type: str,
                     text: str, affiliate_url: str = "") -> None:
        """Record post to DB for tracking."""
        try:
            self.db.execute_insert(
                "INSERT INTO channel_posts "
                "(channel_username, message_id, content_type, text, affiliate_url) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.channel_username, message_id, content_type,
                 text[:500], affiliate_url),
            )
            self.db.execute(
                "UPDATE telegram_channels SET posts_total = posts_total + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE channel_username = ?",
                (self.channel_username,),
            )
        except Exception as e:
            logger.debug(f"Failed to record channel post: {e}")

    def get_post_stats(self, days: int = 7) -> dict[str, Any]:
        """Get posting statistics for the channel."""
        rows = self.db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN content_type = 'deal' THEN 1 ELSE 0 END) as deals, "
            "SUM(CASE WHEN content_type = 'growth' THEN 1 ELSE 0 END) as growth "
            "FROM channel_posts WHERE channel_username = ? "
            "AND created_at > datetime('now', ?)",
            (self.channel_username, f"-{days} days"),
        )
        if rows:
            r = dict(rows[0])
            return {
                "total_posts": r.get("total", 0) or 0,
                "deal_posts": r.get("deals", 0) or 0,
                "growth_posts": r.get("growth", 0) or 0,
                "subscriber_count": self.get_member_count(),
            }
        return {"total_posts": 0, "subscriber_count": 0}


class TelegramChannelError(Exception):
    pass
