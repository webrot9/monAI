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
class CreatorWalletConfig:
    """Creator's crypto wallets — optional, only for crypto flow."""
    xmr_address: str = ""
    btc_address: str = ""
    sweep_threshold_eur: float = 50.0
    sweep_interval_hours: int = 24
    min_confirmations_xmr: int = 10
    min_confirmations_btc: int = 3


@dataclass
class LLCConfig:
    """Holding LLC for multi-layer payout (primary method, no crypto needed).

    Flow: Brand platforms auto-payout → LLC bank → Contractor invoice → Creator.
    """
    enabled: bool = False  # Set True once LLC is formed
    entity_name: str = ""  # "XYZ Holdings LLC"
    entity_type: str = "llc_us"  # llc_us, llc_uk, srl_it
    jurisdiction: str = "US-WY"  # Wyoming default (no public member disclosure)
    contractor_alias: str = ""  # Professional alias for invoicing
    contractor_service: str = "Management consulting and technical advisory"
    contractor_rate_type: str = "percentage"  # percentage, monthly
    contractor_rate_percentage: float = 90.0  # 90% of revenue to contractor
    contractor_rate_amount: float = 0.0  # Fixed amount if monthly
    contractor_payment_method: str = "bank_transfer"


@dataclass
class MoneroConfig:
    """Monero wallet RPC connection for the brand wallets."""
    wallet_rpc_url: str = "http://127.0.0.1:18082"
    rpc_user: str = ""
    rpc_password: str = ""
    proxy_url: str = ""  # Route through Tor: socks5://127.0.0.1:9050


@dataclass
class BTCPayConfig:
    """BTCPay Server for self-hosted crypto payment processing."""
    server_url: str = ""  # https://btcpay.yourdomain.com
    api_key: str = ""
    store_id: str = ""


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    comms: CommsConfig = field(default_factory=CommsConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    creator_wallet: CreatorWalletConfig = field(default_factory=CreatorWalletConfig)
    llc: LLCConfig = field(default_factory=LLCConfig)
    monero: MoneroConfig = field(default_factory=MoneroConfig)
    btcpay: BTCPayConfig = field(default_factory=BTCPayConfig)
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
            if "creator_wallet" in data:
                config.creator_wallet = CreatorWalletConfig(**data["creator_wallet"])
            if "llc" in data:
                config.llc = LLCConfig(**data["llc"])
            if "monero" in data:
                config.monero = MoneroConfig(**data["monero"])
            if "btcpay" in data:
                config.btcpay = BTCPayConfig(**data["btcpay"])
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
            "llc": {
                "enabled": self.llc.enabled,
                "entity_name": self.llc.entity_name,
                "entity_type": self.llc.entity_type,
                "jurisdiction": self.llc.jurisdiction,
                "contractor_alias": self.llc.contractor_alias,
                "contractor_service": self.llc.contractor_service,
                "contractor_rate_type": self.llc.contractor_rate_type,
                "contractor_rate_percentage": self.llc.contractor_rate_percentage,
                "contractor_rate_amount": self.llc.contractor_rate_amount,
                "contractor_payment_method": self.llc.contractor_payment_method,
            },
            "creator_wallet": {
                "xmr_address": self.creator_wallet.xmr_address,
                "btc_address": self.creator_wallet.btc_address,
                "sweep_threshold_eur": self.creator_wallet.sweep_threshold_eur,
                "sweep_interval_hours": self.creator_wallet.sweep_interval_hours,
                "min_confirmations_xmr": self.creator_wallet.min_confirmations_xmr,
                "min_confirmations_btc": self.creator_wallet.min_confirmations_btc,
            },
            "monero": {
                "wallet_rpc_url": self.monero.wallet_rpc_url,
                "rpc_user": self.monero.rpc_user,
                "proxy_url": self.monero.proxy_url,
            },
            "btcpay": {
                "server_url": self.btcpay.server_url,
                "store_id": self.btcpay.store_id,
            },
            "initial_capital": self.initial_capital,
            "currency": self.currency,
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
