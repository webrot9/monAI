"""Tests for Telegram Channel integration."""

import time
from unittest.mock import MagicMock, patch

import pytest

from monai.integrations.telegram_channel import (
    TelegramChannelClient,
    TelegramChannelError,
)


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.data_dir = "/tmp/test"
    return config


@pytest.fixture
def db(tmp_path):
    from monai.db.database import Database
    return Database(tmp_path / "test.db")


@pytest.fixture
def client(mock_config, db):
    """Create a TelegramChannelClient with mocked HTTP."""
    with patch("monai.integrations.telegram_channel.get_anonymizer") as mock_anon:
        mock_anon.return_value = MagicMock()
        c = TelegramChannelClient(
            mock_config, db,
            bot_token="123:ABC",
            channel_username="@testchannel",
        )
    # Mock the anonymizer's HTTP client for API calls
    c._anonymizer = MagicMock()
    return c


def _mock_api_response(client, ok=True, result=None, description=""):
    """Set up mock HTTP client to return a Telegram API response."""
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "ok": ok,
        "result": result or {},
        "description": description,
    }
    mock_http.post.return_value = mock_resp
    client._anonymizer.create_http_client.return_value = mock_http
    return mock_http


class TestTelegramChannelClient:
    def test_schema_created(self, client, db):
        """DB tables are created on init (by client constructor)."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('telegram_channels', 'channel_posts') ORDER BY name"
        )
        names = [dict(r)["name"] for r in rows]
        assert "channel_posts" in names
        assert "telegram_channels" in names

    def test_post_message_calls_api(self, client):
        mock_http = _mock_api_response(client, result={"message_id": 42})
        client._last_post_time = 0  # Skip rate limit wait

        result = client.post_message("Hello world")

        assert result["message_id"] == 42
        call_args = mock_http.post.call_args
        assert "sendMessage" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["chat_id"] == "@testchannel"
        assert payload["text"] == "Hello world"
        assert payload["parse_mode"] == "HTML"

    def test_post_message_records_to_db(self, client, db):
        _mock_api_response(client, result={"message_id": 99})
        client._last_post_time = 0

        client.post_message("Deal text here")

        rows = db.execute("SELECT * FROM channel_posts")
        assert len(rows) == 1
        post = dict(rows[0])
        assert post["channel_username"] == "@testchannel"
        assert post["message_id"] == 99
        assert post["text"] == "Deal text here"

    def test_post_photo_calls_api(self, client):
        mock_http = _mock_api_response(client, result={"message_id": 55})
        client._last_post_time = 0

        result = client.post_photo("https://img.example.com/photo.jpg", caption="Nice deal!")

        assert result["message_id"] == 55
        call_args = mock_http.post.call_args
        assert "sendPhoto" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["photo"] == "https://img.example.com/photo.jpg"
        assert payload["caption"] == "Nice deal!"

    def test_pin_message_success(self, client):
        _mock_api_response(client, result=True)

        assert client.pin_message(42) is True

    def test_pin_message_failure_returns_false(self, client):
        _mock_api_response(client, ok=False, description="not enough rights")

        assert client.pin_message(42) is False

    def test_get_member_count(self, client, db):
        # Seed channel record so UPDATE hits a row
        db.execute_insert(
            "INSERT INTO telegram_channels (channel_username, bot_token, brand) "
            "VALUES (?, ?, ?)",
            ("@testchannel", "123:ABC", "test"),
        )
        _mock_api_response(client, result=1500)

        count = client.get_member_count()

        assert count == 1500
        # Verify DB updated
        rows = db.execute(
            "SELECT subscriber_count FROM telegram_channels "
            "WHERE channel_username = '@testchannel'"
        )
        assert dict(rows[0])["subscriber_count"] == 1500

    def test_get_member_count_error_returns_zero(self, client):
        _mock_api_response(client, ok=False, description="chat not found")

        assert client.get_member_count() == 0

    def test_get_channel_info(self, client):
        _mock_api_response(client, result={"title": "Test Channel", "type": "channel"})

        info = client.get_channel_info()

        assert info["title"] == "Test Channel"
        assert info["type"] == "channel"

    def test_get_channel_info_error(self, client):
        _mock_api_response(client, ok=False, description="chat not found")

        info = client.get_channel_info()
        assert "error" in info

    def test_rate_limiting(self, client):
        """Posts are spaced by MIN_POST_INTERVAL."""
        _mock_api_response(client, result={"message_id": 1})

        client._last_post_time = time.time()  # Just now

        with patch("monai.integrations.telegram_channel.time.sleep") as mock_sleep:
            client.post_message("test")
            # Should have slept to enforce rate limit
            assert mock_sleep.called
            slept = mock_sleep.call_args[0][0]
            assert 0 < slept <= client.MIN_POST_INTERVAL

    def test_api_error_raises(self, client):
        _mock_api_response(client, ok=False, description="Unauthorized")

        with pytest.raises(TelegramChannelError, match="Unauthorized"):
            client._api_call("sendMessage", chat_id="@test", text="hi")

    def test_post_stats_empty(self, client, db):
        stats = client.get_post_stats(days=7)
        assert stats["total_posts"] == 0

    def test_post_stats_counts(self, client, db):
        _mock_api_response(client, result={"message_id": 1})
        client._last_post_time = 0

        # Post a few messages
        for i in range(3):
            _mock_api_response(client, result={"message_id": i + 1})
            client._last_post_time = 0
            client.post_message(f"deal {i}")

        # Mock member count API for the stats call
        _mock_api_response(client, result=0)
        stats = client.get_post_stats(days=7)
        assert stats["total_posts"] == 3
        assert stats["deal_posts"] == 3

    def test_record_post_truncates_long_text(self, client, db):
        _mock_api_response(client, result={"message_id": 1})
        client._last_post_time = 0

        long_text = "x" * 1000
        client.post_message(long_text)

        rows = db.execute("SELECT text FROM channel_posts")
        assert len(dict(rows[0])["text"]) == 500
