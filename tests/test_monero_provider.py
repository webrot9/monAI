"""Tests for Monero payment provider.

Uses mocked RPC responses — no actual monero-wallet-rpc needed.
"""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from monai.payments.monero_provider import (
    MoneroProvider,
    MoneroRPCError,
    ATOMIC_UNITS_PER_XMR,
)
from monai.payments.types import PaymentIntent, PaymentStatus


@pytest.fixture
def provider():
    return MoneroProvider(
        wallet_rpc_url="http://127.0.0.1:18082",
        min_confirmations=10,
    )


def mock_rpc_response(result: dict) -> MagicMock:
    """Create a mock httpx response with JSON-RPC result."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"jsonrpc": "2.0", "id": "1", "result": result}
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


class TestMoneroProvider:
    @pytest.mark.asyncio
    async def test_generate_address(self, provider):
        mock_resp = mock_rpc_response({
            "address": "4" + "B" * 94,
            "address_index": 1,
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            address = await provider.generate_address(label="test_brand")

        assert address.startswith("4")
        assert len(address) == 95

    @pytest.mark.asyncio
    async def test_create_payment_returns_subaddress(self, provider):
        test_address = "4" + "C" * 94
        mock_resp = mock_rpc_response({
            "address": test_address,
            "address_index": 2,
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            intent = PaymentIntent(amount=0.5, product="ebook", brand="newsletter")
            result = await provider.create_payment(intent)

        assert result.success is True
        assert result.payment_ref == test_address
        assert result.currency == "XMR"
        assert "monero:" in result.checkout_url

    @pytest.mark.asyncio
    async def test_verify_payment_by_txid(self, provider):
        tx_hash = "ab" * 32
        mock_resp = mock_rpc_response({
            "transfer": {
                "txid": tx_hash,
                "amount": int(Decimal("1.5") * ATOMIC_UNITS_PER_XMR),
                "confirmations": 15,
                "height": 3000000,
            },
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.verify_payment(tx_hash)

        assert result.success is True
        assert result.amount == Decimal("1.5")
        assert result.status == PaymentStatus.COMPLETED
        assert result.raw["confirmations"] == 15

    @pytest.mark.asyncio
    async def test_verify_payment_pending_confirmations(self, provider):
        tx_hash = "cd" * 32
        mock_resp = mock_rpc_response({
            "transfer": {
                "txid": tx_hash,
                "amount": int(Decimal("0.5") * ATOMIC_UNITS_PER_XMR),
                "confirmations": 3,  # Less than min_confirmations (10)
            },
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.verify_payment(tx_hash)

        assert result.success is True
        assert result.status == PaymentStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_balance(self, provider):
        mock_resp = mock_rpc_response({
            "balance": int(Decimal("5.0") * ATOMIC_UNITS_PER_XMR),
            "unlocked_balance": int(Decimal("4.5") * ATOMIC_UNITS_PER_XMR),
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            balance = await provider.get_balance()

        assert balance.available == Decimal("4.5")
        assert balance.pending == Decimal("0.5")
        assert balance.currency == "XMR"

    @pytest.mark.asyncio
    async def test_send_payout(self, provider):
        tx_hash = "ef" * 32
        mock_resp = mock_rpc_response({
            "tx_hash": tx_hash,
            "tx_key": "key123",
            "fee": int(Decimal("0.00005") * ATOMIC_UNITS_PER_XMR),
            "amount": int(Decimal("2.0") * ATOMIC_UNITS_PER_XMR),
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.send_payout(
                to_address="4" + "D" * 94,
                amount=Decimal("2.0"),
            )

        assert result.success is True
        assert result.payment_ref == tx_hash
        assert result.raw["tx_key"] == "key123"
        assert result.raw["fee"] > 0

    @pytest.mark.asyncio
    async def test_send_payout_rpc_error(self, provider):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jsonrpc": "2.0", "id": "1",
            "error": {"code": -17, "message": "Not enough money"},
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.send_payout(
                to_address="4" + "E" * 94,
                amount=Decimal("999.0"),
            )

        assert result.success is False
        assert "Not enough money" in result.error

    @pytest.mark.asyncio
    async def test_health_check_success(self, provider):
        mock_resp = mock_rpc_response({"height": 3000000})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await provider.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self, provider):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=ConnectionError("offline"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await provider.health_check() is False

    @pytest.mark.asyncio
    async def test_poll_incoming(self, provider):
        mock_resp = mock_rpc_response({
            "in": [
                {
                    "txid": "aa" * 32,
                    "amount": int(Decimal("1.0") * ATOMIC_UNITS_PER_XMR),
                    "address": "4" + "F" * 94,
                    "confirmations": 20,
                    "height": 2999999,
                    "timestamp": 1709900000,
                    "subaddr_index": {"minor": 1},
                },
            ],
            "pending": [],
            "pool": [],
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            transfers = await provider.poll_incoming()

        assert len(transfers) == 1
        assert transfers[0]["amount"] == Decimal("1.0")
        assert transfers[0]["confirmations"] == 20
        assert transfers[0]["category"] == "in"


class TestMoneroRPCError:
    def test_error_message(self):
        err = MoneroRPCError(-17, "Not enough money")
        assert err.code == -17
        assert err.message == "Not enough money"
        assert "Monero RPC error -17" in str(err)
