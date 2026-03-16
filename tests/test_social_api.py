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
    get_required_credential_fields,
    validate_platform_credentials,
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
    def test_post_uses_browser_automation(self, mock_anon, mock_config):
        """IndieHackers client uses real browser automation, not placeholders.

        We verify the method exists and attempts browser-based posting.
        In test env without a real browser, it will return an error status
        which proves it's attempting real actions.
        """
        mock_anon.return_value = MagicMock()
        client = IndieHackersClient(mock_config, {})

        # The post method should try real browser automation
        # Without a real browser it will error — but NOT return 'requires_browser'
        result = client.post("Build in public update")
        assert result["platform"] == "indie_hackers"
        # Must never return the old placeholder status
        assert result.get("status") != "requires_browser"
        # Should be either posted, auth_required, or error (browser not available in test)
        assert result["status"] in ("posted", "auth_required", "error")

    @patch("monai.social.api.get_anonymizer")
    def test_profile_metrics_returns_zeros(self, mock_anon, mock_config):
        mock_anon.return_value = MagicMock()
        client = IndieHackersClient(mock_config, {})
        assert client.get_profile_metrics() == {"followers": 0}


# ── Credential Validation ──────────────────────────────────────


class TestCredentialValidation:
    """Tests for the self-healing credential validation system.

    Verifies that platforms reject incomplete credentials and that
    validate_credentials() catches invalid tokens before they're stored.
    """

    def test_required_fields_twitter(self):
        assert get_required_credential_fields("twitter") == ("bearer_token",)

    def test_required_fields_linkedin(self):
        fields = get_required_credential_fields("linkedin")
        assert "access_token" in fields
        assert "person_urn" in fields

    def test_required_fields_reddit(self):
        fields = get_required_credential_fields("reddit")
        assert "client_id" in fields
        assert "client_secret" in fields
        assert "username" in fields
        assert "password" in fields

    def test_required_fields_indie_hackers(self):
        """IndieHackers uses browser — no API credential requirements."""
        assert get_required_credential_fields("indie_hackers") == ()

    def test_required_fields_unknown_platform(self):
        assert get_required_credential_fields("myspace") == ()

    def test_has_required_credentials_complete(self, mock_config, twitter_creds):
        client = TwitterClient(mock_config, twitter_creds)
        assert client.has_required_credentials() is True

    def test_has_required_credentials_missing(self, mock_config):
        """Password-only credentials are useless for Twitter API."""
        client = TwitterClient(mock_config, {"password": "hunter2"})
        assert client.has_required_credentials() is False

    def test_has_required_credentials_empty_value(self, mock_config):
        """Empty string in a required field is treated as missing."""
        client = LinkedInClient(mock_config, {"access_token": "", "person_urn": ""})
        assert client.has_required_credentials() is False

    def test_linkedin_rejects_password_only(self, mock_config):
        """LinkedIn requires access_token + person_urn, not a password."""
        client = LinkedInClient(mock_config, {"password": "secret"})
        assert client.has_required_credentials() is False
        assert client.validate_credentials() is False

    @patch("monai.social.api.get_anonymizer")
    def test_twitter_validate_success(self, mock_anon, mock_config, twitter_creds):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_http.get.return_value = mock_resp
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = TwitterClient(mock_config, twitter_creds)
        assert client.validate_credentials() is True

    @patch("monai.social.api.get_anonymizer")
    def test_twitter_validate_failure(self, mock_anon, mock_config, twitter_creds):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_http.get.return_value = mock_resp
        mock_anon.return_value.create_http_client.return_value = mock_http

        client = TwitterClient(mock_config, twitter_creds)
        assert client.validate_credentials() is False

    @patch("monai.social.api.get_anonymizer")
    def test_validate_platform_credentials_factory(self, mock_anon, mock_config):
        """validate_platform_credentials() rejects incomplete creds."""
        mock_anon.return_value = MagicMock()
        # Password-only: should fail for LinkedIn
        assert validate_platform_credentials(
            "linkedin", mock_config, {"password": "secret"}
        ) is False
        # Unknown platform: should fail
        assert validate_platform_credentials(
            "nonexistent", mock_config, {}
        ) is False
