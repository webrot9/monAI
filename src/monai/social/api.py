"""Social API integration — posts to Twitter, LinkedIn, Reddit via their APIs.

Uses the anonymizer's HTTP client for all requests. Each platform client
handles auth and posting. Credentials are stored via IdentityManager.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from monai.config import Config
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)


class SocialAPIError(Exception):
    """Raised when a social platform API call fails."""


class BasePlatformClient:
    """Base class for platform API clients."""

    platform: str = ""

    def __init__(self, config: Config, credentials: dict[str, str]):
        self._config = config
        self._credentials = credentials
        self._anonymizer = get_anonymizer(config)

    def _get_client(self) -> httpx.Client:
        self._anonymizer.maybe_rotate()
        return self._anonymizer.create_http_client(timeout=30)

    def post(self, content: str, **kwargs) -> dict[str, Any]:
        raise NotImplementedError

    def get_post_metrics(self, post_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_profile_metrics(self) -> dict[str, Any]:
        raise NotImplementedError


class TwitterClient(BasePlatformClient):
    """Twitter/X API v2 client."""

    platform = "twitter"
    API_BASE = "https://api.twitter.com/2"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credentials.get('bearer_token', '')}",
            "Content-Type": "application/json",
        }

    def post(self, content: str, **kwargs) -> dict[str, Any]:
        """Create a tweet."""
        client = self._get_client()
        payload: dict[str, Any] = {"text": content}

        # Support reply threading
        reply_to = kwargs.get("reply_to_id")
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}

        resp = client.post(
            f"{self.API_BASE}/tweets",
            headers=self._headers(),
            json=payload,
        )

        if resp.status_code != 201:
            raise SocialAPIError(
                f"Twitter post failed ({resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json().get("data", {})
        logger.info(f"Tweet posted: {data.get('id')}")
        return {
            "platform": "twitter",
            "post_id": data.get("id", ""),
            "url": f"https://x.com/i/status/{data.get('id', '')}",
        }

    def post_thread(self, tweets: list[str]) -> list[dict[str, Any]]:
        """Post a thread of tweets."""
        results = []
        reply_to = None
        for tweet in tweets:
            result = self.post(tweet, reply_to_id=reply_to)
            results.append(result)
            reply_to = result["post_id"]
        return results

    def get_post_metrics(self, post_id: str) -> dict[str, Any]:
        """Get engagement metrics for a tweet."""
        client = self._get_client()
        resp = client.get(
            f"{self.API_BASE}/tweets/{post_id}",
            headers=self._headers(),
            params={
                "tweet.fields": "public_metrics",
            },
        )
        if resp.status_code != 200:
            raise SocialAPIError(
                f"Twitter metrics failed ({resp.status_code}): {resp.text[:300]}"
            )

        metrics = resp.json().get("data", {}).get("public_metrics", {})
        return {
            "likes": metrics.get("like_count", 0),
            "comments": metrics.get("reply_count", 0),
            "shares": metrics.get("retweet_count", 0) + metrics.get("quote_count", 0),
            "clicks": metrics.get("impression_count", 0),  # impressions as proxy
        }

    def get_profile_metrics(self) -> dict[str, Any]:
        """Get follower count."""
        client = self._get_client()
        user_id = self._credentials.get("user_id", "")
        resp = client.get(
            f"{self.API_BASE}/users/{user_id}",
            headers=self._headers(),
            params={"user.fields": "public_metrics"},
        )
        if resp.status_code != 200:
            raise SocialAPIError(
                f"Twitter profile failed ({resp.status_code}): {resp.text[:300]}"
            )
        metrics = resp.json().get("data", {}).get("public_metrics", {})
        return {"followers": metrics.get("followers_count", 0)}


class LinkedInClient(BasePlatformClient):
    """LinkedIn API client (Community Management API)."""

    platform = "linkedin"
    API_BASE = "https://api.linkedin.com/v2"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credentials.get('access_token', '')}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def post(self, content: str, **kwargs) -> dict[str, Any]:
        """Create a LinkedIn post."""
        client = self._get_client()
        person_urn = self._credentials.get("person_urn", "")

        payload = {
            "author": person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": content},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }

        resp = client.post(
            f"{self.API_BASE}/ugcPosts",
            headers=self._headers(),
            json=payload,
        )

        if resp.status_code not in (200, 201):
            raise SocialAPIError(
                f"LinkedIn post failed ({resp.status_code}): {resp.text[:300]}"
            )

        post_id = resp.headers.get("x-restli-id", resp.json().get("id", ""))
        logger.info(f"LinkedIn post created: {post_id}")
        return {
            "platform": "linkedin",
            "post_id": post_id,
            "url": "",  # LinkedIn doesn't return a direct URL easily
        }

    def get_post_metrics(self, post_id: str) -> dict[str, Any]:
        """Get engagement metrics for a LinkedIn post."""
        client = self._get_client()
        resp = client.get(
            f"{self.API_BASE}/socialActions/{post_id}",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            return {"likes": 0, "comments": 0, "shares": 0, "clicks": 0}

        data = resp.json()
        return {
            "likes": data.get("likesSummary", {}).get("totalLikes", 0),
            "comments": data.get("commentsSummary", {}).get("totalFirstLevelComments", 0),
            "shares": data.get("sharesSummary", {}).get("totalShares", 0) if "sharesSummary" in data else 0,
            "clicks": 0,
        }

    def get_profile_metrics(self) -> dict[str, Any]:
        """Get follower count."""
        client = self._get_client()
        person_urn = self._credentials.get("person_urn", "")
        resp = client.get(
            f"{self.API_BASE}/networkSizes/{person_urn}",
            headers=self._headers(),
            params={"edgeType": "CompanyFollowedByMember"},
        )
        if resp.status_code != 200:
            return {"followers": 0}
        return {"followers": resp.json().get("firstDegreeSize", 0)}


class RedditClient(BasePlatformClient):
    """Reddit API client (OAuth2)."""

    platform = "reddit"
    API_BASE = "https://oauth.reddit.com"
    AUTH_URL = "https://www.reddit.com/api/v1/access_token"

    def __init__(self, config: Config, credentials: dict[str, str]):
        super().__init__(config, credentials)
        self._access_token: str = ""

    def _ensure_auth(self):
        """Get OAuth2 access token if needed."""
        if self._access_token:
            return
        client = self._get_client()
        resp = client.post(
            self.AUTH_URL,
            auth=(
                self._credentials.get("client_id", ""),
                self._credentials.get("client_secret", ""),
            ),
            data={
                "grant_type": "password",
                "username": self._credentials.get("username", ""),
                "password": self._credentials.get("password", ""),
            },
            headers={"User-Agent": f"monAI/1.0 by {self._credentials.get('username', '')}"},
        )
        if resp.status_code == 200:
            self._access_token = resp.json().get("access_token", "")

    def _headers(self) -> dict[str, str]:
        self._ensure_auth()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": f"monAI/1.0 by {self._credentials.get('username', '')}",
        }

    def post(self, content: str, **kwargs) -> dict[str, Any]:
        """Submit a text post to a subreddit."""
        client = self._get_client()
        subreddit = kwargs.get("subreddit", "test")
        title = kwargs.get("title", content[:100])

        resp = client.post(
            f"{self.API_BASE}/api/submit",
            headers=self._headers(),
            data={
                "sr": subreddit,
                "kind": "self",
                "title": title,
                "text": content,
            },
        )

        if resp.status_code != 200:
            raise SocialAPIError(
                f"Reddit post failed ({resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json()
        post_url = ""
        # Reddit returns nested JSON
        if "json" in data and "data" in data["json"]:
            post_url = data["json"]["data"].get("url", "")
            post_id = data["json"]["data"].get("name", "")
        else:
            post_id = ""

        logger.info(f"Reddit post submitted: {post_id}")
        return {
            "platform": "reddit",
            "post_id": post_id,
            "url": post_url,
        }

    def comment(self, parent_id: str, content: str) -> dict[str, Any]:
        """Reply to a post or comment."""
        client = self._get_client()
        resp = client.post(
            f"{self.API_BASE}/api/comment",
            headers=self._headers(),
            data={"thing_id": parent_id, "text": content},
        )
        if resp.status_code != 200:
            raise SocialAPIError(
                f"Reddit comment failed ({resp.status_code}): {resp.text[:300]}"
            )
        return {"platform": "reddit", "post_id": parent_id, "action": "comment"}

    def get_post_metrics(self, post_id: str) -> dict[str, Any]:
        """Get metrics for a Reddit post."""
        client = self._get_client()
        resp = client.get(
            f"{self.API_BASE}/api/info",
            headers=self._headers(),
            params={"id": post_id},
        )
        if resp.status_code != 200:
            return {"likes": 0, "comments": 0, "shares": 0, "clicks": 0}

        children = resp.json().get("data", {}).get("children", [])
        if not children:
            return {"likes": 0, "comments": 0, "shares": 0, "clicks": 0}

        data = children[0].get("data", {})
        return {
            "likes": data.get("ups", 0),
            "comments": data.get("num_comments", 0),
            "shares": data.get("num_crossposts", 0),
            "clicks": 0,
        }

    def get_profile_metrics(self) -> dict[str, Any]:
        """Get karma/followers."""
        client = self._get_client()
        resp = client.get(
            f"{self.API_BASE}/api/v1/me",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            return {"followers": 0}
        data = resp.json()
        return {
            "followers": data.get("subreddit", {}).get("subscribers", 0),
            "karma": data.get("total_karma", 0),
        }


class IndieHackersClient(BasePlatformClient):
    """Indie Hackers client — uses browser automation for posting
    since IH doesn't have a public API."""

    platform = "indie_hackers"
    IH_BASE = "https://www.indiehackers.com"

    def post(self, content: str, **kwargs) -> dict[str, Any]:
        """Post on Indie Hackers using browser automation."""
        from monai.utils.browser import Browser

        group = kwargs.get("group", "")
        title = kwargs.get("title", content[:80])

        async def _post():
            browser = Browser(self._config, headless=True)
            try:
                await browser.start()

                if group:
                    await browser.navigate(f"{self.IH_BASE}/group/{group}/posts/new")
                else:
                    await browser.navigate(f"{self.IH_BASE}/new-post")

                page_info = await browser.get_page_info()
                page_text = (page_info.get("text", "") or "").lower()

                # Check if we need to log in
                if "sign in" in page_text or "log in" in page_text:
                    return {
                        "platform": "indie_hackers",
                        "post_id": "",
                        "url": "",
                        "status": "auth_required",
                        "content": content,
                    }

                # Fill in post form
                await browser.fill_form({
                    "[name='title'], #title, .title-input": title,
                    "[name='body'], #body, .body-input, .ProseMirror": content,
                })
                await browser.submit_form("form")

                import asyncio
                await asyncio.sleep(3)
                final_info = await browser.get_page_info()
                post_url = final_info.get("url", "")

                return {
                    "platform": "indie_hackers",
                    "post_id": post_url.split("/")[-1] if post_url else "",
                    "url": post_url,
                    "status": "posted",
                }
            except Exception as e:
                logger.error(f"IH post failed: {e}")
                return {
                    "platform": "indie_hackers",
                    "post_id": "",
                    "url": "",
                    "status": "error",
                    "error": str(e),
                    "content": content,
                }
            finally:
                await browser.stop()

        import asyncio
        from monai.agents.base import BaseAgent
        return BaseAgent._run_async(_post())

    def get_post_metrics(self, post_id: str) -> dict[str, Any]:
        """Scrape metrics from an IH post page."""
        if not post_id:
            return {"likes": 0, "comments": 0, "shares": 0, "clicks": 0}

        from monai.utils.browser import Browser
        import asyncio

        async def _get_metrics():
            browser = Browser(self._config, headless=True)
            try:
                await browser.start()
                await browser.navigate(f"{self.IH_BASE}/post/{post_id}")
                text = await browser.get_text()

                # Extract metrics from page text (best-effort)
                import re
                likes = 0
                comments = 0
                like_match = re.search(r"(\d+)\s*(?:upvote|like|point)", text.lower())
                if like_match:
                    likes = int(like_match.group(1))
                comment_match = re.search(r"(\d+)\s*(?:comment|repl)", text.lower())
                if comment_match:
                    comments = int(comment_match.group(1))

                return {"likes": likes, "comments": comments, "shares": 0, "clicks": 0}
            except Exception:
                return {"likes": 0, "comments": 0, "shares": 0, "clicks": 0}
            finally:
                await browser.stop()

        from monai.agents.base import BaseAgent
        return BaseAgent._run_async(_get_metrics())

    def get_profile_metrics(self) -> dict[str, Any]:
        return {"followers": 0}


# ── Factory ──────────────────────────────────────────────────

PLATFORM_CLIENTS = {
    "twitter": TwitterClient,
    "linkedin": LinkedInClient,
    "reddit": RedditClient,
    "indie_hackers": IndieHackersClient,
}


def create_platform_client(
    platform: str, config: Config, credentials: dict[str, str]
) -> BasePlatformClient:
    """Create a platform client for the given platform."""
    client_cls = PLATFORM_CLIENTS.get(platform)
    if not client_cls:
        raise ValueError(f"Unknown platform: {platform}")
    return client_cls(config, credentials)
