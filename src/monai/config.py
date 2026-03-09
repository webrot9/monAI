"""Configuration management for monAI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".monai"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_PATH = CONFIG_DIR / "monai.db"


@dataclass
class RiskConfig:
    max_strategy_allocation_pct: float = 30.0  # No single strategy gets >30%
    min_active_strategies: int = 3
    stop_loss_pct: float = 15.0  # Halt strategy if loses >15% of allocated capital
    min_roi_threshold: float = 1.0  # Minimum 1x ROI to keep strategy active
    max_monthly_spend_new_strategy: float = 10.0  # Start small, scale on results
    review_period_days: int = 30  # Re-evaluate strategies every 30 days


@dataclass
class LLMConfig:
    model: str = "gpt-4o"
    model_mini: str = "gpt-4o-mini"
    api_key: str = ""
    max_tokens: int = 4096
    temperature: float = 0.7

    def __post_init__(self):
        if not self.api_key:
            self.api_key = os.environ.get("OPENAI_API_KEY", "")


@dataclass
class PrivacyConfig:
    """Network anonymization — agents must be completely untraceable."""
    proxy_type: str = "tor"  # tor, socks5, http, none
    tor_socks_port: int = 9050
    tor_control_port: int = 9051
    tor_password: str = ""  # For Tor control protocol (circuit renewal)
    socks5_proxy: str = ""  # socks5://host:port (when not using Tor)
    http_proxy: str = ""  # http://host:port (fallback)
    rotate_user_agent: bool = True
    strip_metadata: bool = True  # Strip EXIF, PDF metadata from all output
    dns_over_proxy: bool = True  # Route DNS through proxy to prevent leaks
    verify_anonymity: bool = True  # Check real IP is hidden before operations
    max_requests_per_circuit: int = 50  # Rotate Tor circuit after N requests


@dataclass
class TelegramConfig:
    """Telegram bot for creator communication."""
    bot_token: str = ""  # Acquired autonomously via BotFather
    creator_chat_id: str = ""  # Discovered after creator sends /start
    creator_username: str = "Cristal89"  # The creator's Telegram username
    enabled: bool = True


@dataclass
class CommsConfig:
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    from_name: str = "monAI"
    from_email: str = ""


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    comms: CommsConfig = field(default_factory=CommsConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    initial_capital: float = 500.0  # €500 initial budget
    currency: str = "EUR"
    data_dir: Path = field(default_factory=lambda: CONFIG_DIR)

    @classmethod
    def load(cls) -> Config:
        config = cls()
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            if "llm" in data:
                config.llm = LLMConfig(**data["llm"])
            if "risk" in data:
                config.risk = RiskConfig(**data["risk"])
            if "comms" in data:
                config.comms = CommsConfig(**data["comms"])
            if "privacy" in data:
                config.privacy = PrivacyConfig(**data["privacy"])
            if "telegram" in data:
                config.telegram = TelegramConfig(**data["telegram"])
            if "initial_capital" in data:
                config.initial_capital = data["initial_capital"]
            if "currency" in data:
                config.currency = data["currency"]
        return config

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "llm": {
                "model": self.llm.model,
                "model_mini": self.llm.model_mini,
                "max_tokens": self.llm.max_tokens,
                "temperature": self.llm.temperature,
            },
            "risk": {
                "max_strategy_allocation_pct": self.risk.max_strategy_allocation_pct,
                "min_active_strategies": self.risk.min_active_strategies,
                "stop_loss_pct": self.risk.stop_loss_pct,
                "min_roi_threshold": self.risk.min_roi_threshold,
                "max_monthly_spend_new_strategy": self.risk.max_monthly_spend_new_strategy,
                "review_period_days": self.risk.review_period_days,
            },
            "comms": {
                "smtp_host": self.comms.smtp_host,
                "smtp_port": self.comms.smtp_port,
                "smtp_user": self.comms.smtp_user,
                "imap_host": self.comms.imap_host,
                "imap_port": self.comms.imap_port,
                "from_name": self.comms.from_name,
                "from_email": self.comms.from_email,
            },
            "privacy": {
                "proxy_type": self.privacy.proxy_type,
                "tor_socks_port": self.privacy.tor_socks_port,
                "tor_control_port": self.privacy.tor_control_port,
                "socks5_proxy": self.privacy.socks5_proxy,
                "http_proxy": self.privacy.http_proxy,
                "rotate_user_agent": self.privacy.rotate_user_agent,
                "strip_metadata": self.privacy.strip_metadata,
                "dns_over_proxy": self.privacy.dns_over_proxy,
                "verify_anonymity": self.privacy.verify_anonymity,
                "max_requests_per_circuit": self.privacy.max_requests_per_circuit,
            },
            "telegram": {
                "bot_token": self.telegram.bot_token,
                "creator_chat_id": self.telegram.creator_chat_id,
                "creator_username": self.telegram.creator_username,
                "enabled": self.telegram.enabled,
            },
            "initial_capital": self.initial_capital,
            "currency": self.currency,
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
