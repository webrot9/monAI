"""Tests for social API integration layer."""

from unittest.mock import MagicMock, patch

import pytest

from monai.social.api import (
    BasePlatformClient,
    IndieHackersClient,
    LinkedInClient,
    RedditClient,
    SocialAPIError,
    TwitterClient,
    create_platform_client,
)


@pytest.fixture
def mock_config():
    return MagicMock()


@pytest.fixture
def twitter_creds():
    return {
        "bearer_token": "test_bearer",
        "user_id": "12345",
    }


@pytest.fixture
def linkedin_creds():
    return {
        "access_token": "test_access",
        "person_urn": "urn:li:person:abc123",
    }


@pytest.fixture
def reddit_creds():
    return {
        "client_id": "test_client",
        "client_secret": "test_secret",
        "username": "test_user",
        "password": "test_pass",
    }


# ── Factory ──────────────────────────────────────────────────


class TestFactory:
    def test_create_twitter_client(self, mock_config, twitter_creds):
        client = create_platform_client("twitter", mock_config, twitter_creds)
        assert isinstance(client, TwitterClient)
        assert client.platform == "twitter"

    def test_create_linkedin_client(self, mock_config, linkedin_creds):
        client = create_platform_client("linkedin", mock_config, linkedin_creds)
        assert isinstance(client, LinkedInClient)

    def test_create_reddit_client(self, mock_config, reddit_creds):
        client = create_platform_client("reddit", mock_config, reddit_creds)
        assert isinstance(client, RedditClient)

    def test_create_indie_hackers_client(self, mock_config):
        client = create_platform_client("indie_hackers", mock_config, {})
        assert isinstance(client, IndieHackersClient)

    def test_create_unknown_raises(self, mock_config):
        with pytest.raises(ValueError, match="Unknown platform"):
            create_platform_client("myspace", mock_config, {})


# ── Twitter ──────────────────────────────────────────────────


class TestTwitterClient:
    @patch("monai.social.api.get_anonymizer")
    def test_post_success(self, mock_anon, mock_config, twitter_creds):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"data": {"id": "tweet_123"}}
        mock_http.post.return_value = mock_resp
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = TwitterClient(mock_config, twitter_creds)
        result = client.post("Hello world!")

        assert result["platform"] == "twitter"
        assert result["post_id"] == "tweet_123"
        assert "tweet_123" in result["url"]

    @patch("monai.social.api.get_anonymizer")
    def test_post_failure_raises(self, mock_anon, mock_config, twitter_creds):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        mock_http.post.return_value = mock_resp
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = TwitterClient(mock_config, twitter_creds)
        with pytest.raises(SocialAPIError, match="403"):
            client.post("Hello!")

    @patch("monai.social.api.get_anonymizer")
    def test_post_thread(self, mock_anon, mock_config, twitter_creds):
        mock_http = MagicMock()
        call_count = [0]

        def mock_post(*args, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"data": {"id": f"tweet_{call_count[0]}"}}
            return resp

        mock_http.post.side_effect = mock_post
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = TwitterClient(mock_config, twitter_creds)
        results = client.post_thread(["First", "Second", "Third"])

        assert len(results) == 3
        assert results[0]["post_id"] == "tweet_1"
        assert results[2]["post_id"] == "tweet_3"

    @patch("monai.social.api.get_anonymizer")
    def test_get_post_metrics(self, mock_anon, mock_config, twitter_creds):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "public_metrics": {
                    "like_count": 42,
                    "reply_count": 5,
                    "retweet_count": 10,
                    "quote_count": 3,
                    "impression_count": 1000,
                }
            }
        }
        mock_http.get.return_value = mock_resp
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = TwitterClient(mock_config, twitter_creds)
        metrics = client.get_post_metrics("tweet_123")

        assert metrics["likes"] == 42
        assert metrics["comments"] == 5
        assert metrics["shares"] == 13  # retweet + quote

    @patch("monai.social.api.get_anonymizer")
    def test_get_profile_metrics(self, mock_anon, mock_config, twitter_creds):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {"public_metrics": {"followers_count": 5000}}
        }
        mock_http.get.return_value = mock_resp
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = TwitterClient(mock_config, twitter_creds)
        result = client.get_profile_metrics()
        assert result["followers"] == 5000


# ── LinkedIn ─────────────────────────────────────────────────


class TestLinkedInClient:
    @patch("monai.social.api.get_anonymizer")
    def test_post_success(self, mock_anon, mock_config, linkedin_creds):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.headers = {"x-restli-id": "urn:li:share:123"}
        mock_resp.json.return_value = {"id": "urn:li:share:123"}
        mock_http.post.return_value = mock_resp
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = LinkedInClient(mock_config, linkedin_creds)
        result = client.post("Great insight about SaaS pricing!")

        assert result["platform"] == "linkedin"
        assert result["post_id"] == "urn:li:share:123"

    @patch("monai.social.api.get_anonymizer")
    def test_post_failure_raises(self, mock_anon, mock_config, linkedin_creds):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_http.post.return_value = mock_resp
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = LinkedInClient(mock_config, linkedin_creds)
        with pytest.raises(SocialAPIError, match="401"):
            client.post("Test")


# ── Reddit ───────────────────────────────────────────────────


class TestRedditClient:
    @patch("monai.social.api.get_anonymizer")
    def test_post_success(self, mock_anon, mock_config, reddit_creds):
        mock_http = MagicMock()

        # Auth response
        auth_resp = MagicMock()
        auth_resp.status_code = 200
        auth_resp.json.return_value = {"access_token": "reddit_token"}

        # Post response
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {
            "json": {"data": {"url": "https://reddit.com/r/test/123", "name": "t3_abc"}}
        }

        mock_http.post.side_effect = [auth_resp, post_resp]
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = RedditClient(mock_config, reddit_creds)
        result = client.post("My SaaS journey", subreddit="SaaS", title="Month 3 update")

        assert result["platform"] == "reddit"
        assert result["post_id"] == "t3_abc"

    @patch("monai.social.api.get_anonymizer")
    def test_comment(self, mock_anon, mock_config, reddit_creds):
        mock_http = MagicMock()

        auth_resp = MagicMock()
        auth_resp.status_code = 200
        auth_resp.json.return_value = {"access_token": "reddit_token"}

        comment_resp = MagicMock()
        comment_resp.status_code = 200
        comment_resp.json.return_value = {}

        mock_http.post.side_effect = [auth_resp, comment_resp]
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = RedditClient(mock_config, reddit_creds)
        result = client.comment("t3_abc", "Great post!")
        assert result["action"] == "comment"


# ── Indie Hackers ────────────────────────────────────────────


class TestIndieHackersClient:
    @patch("monai.social.api.get_anonymizer")
    def test_post_returns_browser_required(self, mock_anon, mock_config):
        mock_anon.return_value = MagicMock()
        client = IndieHackersClient(mock_config, {})
        result = client.post("Build in public update")
        assert result["status"] == "requires_browser"
        assert result["content"] == "Build in public update"

    @patch("monai.social.api.get_anonymizer")
    def test_metrics_return_zeros(self, mock_anon, mock_config):
        mock_anon.return_value = MagicMock()
        client = IndieHackersClient(mock_config, {})
        assert client.get_post_metrics("123") == {
            "likes": 0, "comments": 0, "shares": 0, "clicks": 0,
        }
        assert client.get_profile_metrics() == {"followers": 0}
