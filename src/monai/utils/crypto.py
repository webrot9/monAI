"""Config field encryption using Fernet symmetric encryption.

Sensitive config values are encrypted at rest and prefixed with 'ENC:' to
distinguish them from plaintext.  The encryption key is derived from a
passphrase (env var ``MONAI_CONFIG_KEY``) or auto-generated and stored at
``~/.monai/.config_key``.
"""

from __future__ import annotations

import base64
import fnmatch
import os
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_ENC_PREFIX = "ENC:"

# Key patterns for fields that must be encrypted on disk.
_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "*password*",
    "*secret*",
    "*api_key*",
    "*bot_token*",
    "*pin*",
    "*card_number*",
    "*card_cvv*",
    "*rpc_password*",
    "*xmr_address*",
    "*btc_address*",
    "*chat_id*",
    "*creator_username*",
)

_KEY_DIR = Path.home() / ".monai"
_KEY_FILE = _KEY_DIR / ".config_key"

# Fixed salt — acceptable because each deployment has its own unique passphrase.
_SALT = b"monai-config-encryption-salt"


def _get_fernet() -> Fernet:
    """Return a Fernet instance using the configured or auto-generated key."""
    passphrase = os.environ.get("MONAI_CONFIG_KEY")

    if not passphrase:
        # Auto-generate and persist a key file if it doesn't exist.
        _KEY_DIR.mkdir(parents=True, exist_ok=True)
        if _KEY_FILE.exists():
            passphrase = _KEY_FILE.read_text().strip()
        else:
            passphrase = Fernet.generate_key().decode()
            _KEY_FILE.write_text(passphrase)
            _KEY_FILE.chmod(0o600)

    # Derive a proper 32-byte key from the passphrase via PBKDF2.
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=480_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))
    return Fernet(key)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def encrypt_value(plaintext: str) -> str:
    """Encrypt a string and return it with the ``ENC:`` prefix."""
    if not plaintext:
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode()).decode()
    return f"{_ENC_PREFIX}{token}"


def decrypt_value(ciphertext: str) -> str:
    """Decrypt an ``ENC:``-prefixed string back to plaintext."""
    if not ciphertext or not ciphertext.startswith(_ENC_PREFIX):
        return ciphertext
    token = ciphertext[len(_ENC_PREFIX):]
    return _get_fernet().decrypt(token.encode()).decode()


def _is_sensitive(key: str) -> bool:
    """Return True if *key* matches any sensitive pattern."""
    key_lower = key.lower()
    return any(fnmatch.fnmatch(key_lower, pat) for pat in _SENSITIVE_PATTERNS)


def encrypt_config_fields(config_data: dict) -> dict:
    """Return a deep copy of *config_data* with sensitive string values encrypted."""
    return _walk(config_data, encrypt_value)


def decrypt_config_fields(config_data: dict) -> dict:
    """Return a deep copy of *config_data* with ``ENC:`` values decrypted."""
    return _walk(config_data, decrypt_value)


def _walk(data: dict, transform) -> dict:
    """Recursively walk *data*, applying *transform* to sensitive string values."""
    out: dict = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = _walk(value, transform)
        elif isinstance(value, str) and (_is_sensitive(key) or value.startswith(_ENC_PREFIX)):
            out[key] = transform(value)
        else:
            out[key] = value
    return out
