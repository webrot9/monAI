"""Tests for the Email Verifier."""

from unittest.mock import MagicMock, patch

import pytest

from monai.config import Config
from monai.db.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def verifier(config, db):
    with patch("monai.agents.email_verifier.get_anonymizer") as mock_anon:
        mock_anon.return_value.create_http_client.return_value = MagicMock()
        from monai.agents.email_verifier import EmailVerifier
        return EmailVerifier(config, db)


class TestEmailVerifierSchema:
    def test_creates_table(self, verifier, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='email_verifications'"
        )
        assert len(rows) == 1


class TestExtractVerification:
    """Core logic: extracting codes and links from email text."""

    def test_extract_code_with_keyword(self, verifier):
        text = "Your verification code is: 847291. Enter it to confirm."
        result = verifier._extract_verification(text)
        assert result is not None
        assert result["type"] == "code"
        assert result["value"] == "847291"

    def test_extract_otp(self, verifier):
        text = "Your OTP: 5923. Do not share this code."
        result = verifier._extract_verification(text)
        assert result is not None
        assert result["type"] == "code"
        assert result["value"] == "5923"

    def test_extract_pin(self, verifier):
        text = "Confirm your account. PIN: 123456"
        result = verifier._extract_verification(text)
        assert result is not None
        assert result["type"] == "code"
        assert result["value"] == "123456"

    def test_extract_standalone_6digit(self, verifier):
        text = "Welcome! Please verify your email. Your code:\n 482917 \nEnter it now."
        result = verifier._extract_verification(text)
        assert result is not None
        assert result["type"] == "code"
        assert result["value"] == "482917"

    def test_no_false_positive_on_year(self, verifier):
        """Should NOT extract '2024' from a copyright line as a code."""
        text = "Welcome to our platform. © 2024 All rights reserved."
        result = verifier._extract_verification(text)
        # 'welcome' is a verify signal but '2024' is embedded, not a code
        # The tightened regex requires standalone digits
        assert result is None

    def test_extract_verification_link(self, verifier):
        text = "Please verify your email by clicking: https://app.com/verify?token=abc123xyz"
        result = verifier._extract_verification(text)
        assert result is not None
        assert result["type"] == "link"
        assert "verify" in result["value"]
        assert "token=abc123xyz" in result["value"]

    def test_extract_confirm_link(self, verifier):
        text = "Confirm your registration: https://platform.io/confirm/user/9f8a7b"
        result = verifier._extract_verification(text)
        assert result is not None
        assert result["type"] == "link"
        assert "confirm" in result["value"]

    def test_no_extraction_from_irrelevant_email(self, verifier):
        """Newsletter with no verification content should return None."""
        text = "Check out our latest blog post about productivity tips!"
        result = verifier._extract_verification(text)
        assert result is None

    def test_code_priority_over_link(self, verifier):
        """When both code and link are present, code wins (tried first)."""
        text = (
            "Verify your email. Your code: 583921. "
            "Or click: https://app.com/verify?key=xyz"
        )
        result = verifier._extract_verification(text)
        assert result["type"] == "code"
        assert result["value"] == "583921"


class TestImapDetection:
    def test_gmail(self, verifier):
        host, port = verifier._detect_imap("user@gmail.com")
        assert host == "imap.gmail.com"
        assert port == 993

    def test_outlook(self, verifier):
        host, port = verifier._detect_imap("user@outlook.com")
        assert host == "outlook.office365.com"

    def test_hotmail(self, verifier):
        host, port = verifier._detect_imap("user@hotmail.com")
        assert host == "outlook.office365.com"

    def test_protonmail(self, verifier):
        host, port = verifier._detect_imap("user@protonmail.com")
        assert host == "127.0.0.1"
        assert port == 1143

    def test_unknown_domain_fallback(self, verifier):
        host, port = verifier._detect_imap("user@customdomain.io")
        assert host == "imap.customdomain.io"
        assert port == 993


class TestPlatformDomains:
    def test_known_platform(self, verifier):
        domains = verifier._get_platform_domains("upwork")
        assert "upwork.com" in domains
        assert "upwork" in domains  # broad search term

    def test_github(self, verifier):
        domains = verifier._get_platform_domains("github")
        assert "github.com" in domains

    def test_unknown_platform(self, verifier):
        domains = verifier._get_platform_domains("newplatform")
        assert "newplatform.com" in domains
        assert "newplatform" in domains


class TestDecodeSubject:
    def test_plain_subject(self, verifier):
        assert verifier._decode_subject("Verify your email") == "Verify your email"

    def test_empty_subject(self, verifier):
        assert verifier._decode_subject("") == ""


class TestGetEmailBody:
    def test_plain_text_body(self, verifier):
        import email as email_mod
        msg = email_mod.message_from_string(
            "Subject: Test\n"
            "Content-Type: text/plain\n\n"
            "Your code is 123456"
        )
        body = verifier._get_email_body(msg)
        assert "123456" in body

    def test_html_body_strips_tags(self, verifier):
        import email as email_mod
        msg = email_mod.message_from_string(
            "Subject: Test\n"
            "Content-Type: text/html\n\n"
            "<html><body><p>Your code is <b>654321</b></p></body></html>"
        )
        body = verifier._get_email_body(msg)
        assert "654321" in body
        assert "<b>" not in body  # HTML tags stripped


class TestWaitForVerificationDB:
    def test_records_request_in_db(self, verifier, db):
        """Verify that wait_for_verification records the attempt."""
        # This will fail quickly since there's no IMAP server and no temp email
        # But it should still record the request in the DB
        with patch.object(verifier, '_try_temp_email', return_value={"status": "timeout", "attempts": 0}):
            verifier.wait_for_verification("test@example.com", "github", timeout=1)

        rows = db.execute("SELECT * FROM email_verifications")
        assert len(rows) == 1
        assert rows[0]["email_address"] == "test@example.com"
        assert rows[0]["platform"] == "github"
