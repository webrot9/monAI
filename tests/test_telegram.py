"""Tests for monai.utils.telegram."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monai.config import TelegramConfig
from monai.utils.telegram import TelegramBot


class TestTelegramBot:
    @pytest.fixture
    def telegram_config(self, config):
        config.telegram = TelegramConfig(
            bot_token="test-token-123",
            creator_chat_id="999",
            creator_username="TestCreator",
            enabled=True,
        )
        return config

    @pytest.fixture
    def bot(self, telegram_config, db):
        with patch("monai.utils.telegram.get_anonymizer") as mock_anon:
            mock_client = MagicMock()
            mock_anon.return_value.create_http_client.return_value = mock_client
            mock_anon.return_value.maybe_rotate = MagicMock()
            return TelegramBot(telegram_config, db)

    @pytest.fixture
    def unconfigured_bot(self, config, db):
        config.telegram = TelegramConfig(
            bot_token="",
            creator_chat_id="",
            creator_username="TestCreator",
            enabled=True,
        )
        with patch("monai.utils.telegram.get_anonymizer") as mock_anon:
            mock_client = MagicMock()
            mock_anon.return_value.create_http_client.return_value = mock_client
            return TelegramBot(config, db)

    # ── Configuration ─────────────────────────────────────────

    def test_is_configured_with_token_and_chat_id(self, bot):
        assert bot.is_configured is True

    def test_not_configured_without_token(self, unconfigured_bot):
        assert unconfigured_bot.is_configured is False

    def test_has_token(self, bot):
        assert bot.has_token is True

    def test_no_token(self, unconfigured_bot):
        assert unconfigured_bot.has_token is False

    # ── State Management ──────────────────────────────────────

    def test_set_and_get_state(self, bot):
        bot._set_state("test_key", "test_value")
        assert bot._get_state("test_key") == "test_value"

    def test_get_nonexistent_state(self, bot):
        assert bot._get_state("nonexistent") is None

    def test_set_state_overwrites(self, bot):
        bot._set_state("key", "value1")
        bot._set_state("key", "value2")
        assert bot._get_state("key") == "value2"

    def test_set_bot_token(self, unconfigured_bot):
        unconfigured_bot.set_bot_token("new-token-456")
        assert unconfigured_bot._bot_token == "new-token-456"
        assert unconfigured_bot._get_state("bot_token") == "new-token-456"

    # ── Verification Protocol ─────────────────────────────────

    def test_generate_verification(self, bot):
        code = bot.generate_verification()
        assert len(code) == 8
        assert code == code.upper()  # All caps

    def test_verification_stored_in_db(self, bot):
        bot.generate_verification()
        token = bot._get_state("verification_token")
        assert token is not None
        assert len(token) == 32  # 16 bytes hex

    def test_verification_file_created(self, bot, telegram_config):
        bot.generate_verification()
        verify_file = telegram_config.data_dir / "verify.txt"
        assert verify_file.exists()
        content = verify_file.read_text()
        assert "monAI Verification Token" in content
        assert "Token:" in content
        assert "Short code:" in content

    def test_verification_code_consistent(self, bot):
        code1 = bot.generate_verification()
        code2 = bot.get_verification_code()
        assert code1 == code2

    def test_message_header_includes_code(self, bot):
        code = bot.generate_verification()
        header = bot._message_header()
        assert code in header
        assert "monAI Agent" in header
        assert "verify:" in header

    # ── Messaging ─────────────────────────────────────────────

    def test_send_message_requires_chat_id(self, unconfigured_bot):
        with pytest.raises(RuntimeError, match="chat_id unknown"):
            unconfigured_bot.send_message("test")

    def test_api_call_requires_token(self, unconfigured_bot):
        with pytest.raises(RuntimeError, match="bot token not configured"):
            unconfigured_bot._api_call("getMe")

    def test_send_message_logs_outbound(self, bot, db):
        # Mock the API call
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 42}}
        bot._get_client().post.return_value = mock_response

        bot.send_message("Hello creator")

        rows = db.execute(
            "SELECT * FROM telegram_messages WHERE direction = 'outbound'"
        )
        assert len(rows) == 1
        assert "Hello creator" in rows[0]["text"]
        assert rows[0]["message_id"] == 42

    def test_send_message_includes_header(self, bot):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        bot._get_client().post.return_value = mock_response
        bot.generate_verification()

        bot.send_message("Test message")

        call_args = bot._get_client().post.call_args
        sent_text = call_args.kwargs.get("json", {}).get("text", "")
        assert "monAI Agent" in sent_text
        assert "verify:" in sent_text

    def test_notify_creator_returns_true_on_success(self, bot):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        bot._get_client().post.return_value = mock_response

        assert bot.notify_creator("Alert!") is True

    def test_notify_creator_returns_false_on_failure(self, bot):
        bot._get_client().post.side_effect = Exception("network error")
        assert bot.notify_creator("Alert!") is False

    # ── Update Processing ─────────────────────────────────────

    def test_process_start_from_creator(self, bot):
        mock_response = MagicMock()
        # First call: getUpdates, second call: sendMessage
        mock_response.json.side_effect = [
            {
                "ok": True,
                "result": [{
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 12345},
                        "text": "/start",
                        "from": {"username": "TestCreator"},
                        "message_id": 1,
                    }
                }],
            },
            {"ok": True, "result": {"message_id": 2}},
        ]
        bot._get_client().post.return_value = mock_response

        updates = bot.process_updates()
        assert len(updates) == 1
        assert updates[0]["type"] == "start"
        assert updates[0]["username"] == "TestCreator"

    def test_process_start_from_unauthorized(self, bot):
        mock_response = MagicMock()
        mock_response.json.side_effect = [
            {
                "ok": True,
                "result": [{
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 99999},
                        "text": "/start",
                        "from": {"username": "RandomPerson"},
                        "message_id": 1,
                    }
                }],
            },
            {"ok": True, "result": {"message_id": 2}},
        ]
        bot._get_client().post.return_value = mock_response

        updates = bot.process_updates()
        assert len(updates) == 1
        assert updates[0]["type"] == "unauthorized"

    def test_process_status_request(self, bot):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 999},
                    "text": "/status",
                    "from": {"username": "TestCreator"},
                    "message_id": 1,
                }
            }],
        }
        bot._get_client().post.return_value = mock_response

        updates = bot.process_updates()
        assert len(updates) == 1
        assert updates[0]["type"] == "status_request"

    def test_process_regular_message(self, bot, db):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 999},
                    "text": "How's business going?",
                    "from": {"username": "TestCreator"},
                    "message_id": 5,
                }
            }],
        }
        bot._get_client().post.return_value = mock_response

        updates = bot.process_updates()
        assert len(updates) == 1
        assert updates[0]["type"] == "message"
        assert updates[0]["text"] == "How's business going?"

        # Should be logged
        rows = db.execute(
            "SELECT * FROM telegram_messages WHERE direction = 'inbound'"
        )
        assert len(rows) == 1

    # ── Provisioning Task ─────────────────────────────────────

    def test_provisioning_task_structure(self, unconfigured_bot):
        task = unconfigured_bot.get_provisioning_task()
        assert "task" in task
        assert "BotFather" in task["task"]
        assert task["needs_browser"] is True
        assert "on_token_acquired" in task

    # ── Send Report ───────────────────────────────────────────

    def test_send_report(self, bot):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        bot._get_client().post.return_value = mock_response

        result = bot.send_report("Daily Report", {
            "Revenue": "€500",
            "Expenses": "€50",
        })
        assert result is True

    # ── Cleanup ───────────────────────────────────────────────

    def test_close_cleans_up(self, bot):
        bot._get_client()  # Ensure client is created
        bot.close()
        assert bot._client is None
