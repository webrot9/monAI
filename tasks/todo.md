# monAI - Payment Pipeline Implementation

## Vision
Implementare il flusso reale dei soldi: Brand raccoglie → Anonimizza → Trasferisce al creator.
Ogni brand ha i suoi account di pagamento. I profitti vengono "swept" in crypto anonima verso il creator.

## Architecture

```
Customer pays Brand
       │
       ▼
┌─────────────────────┐
│  Payment Gateway    │  ← Stripe webhook / BTCPay callback / Gumroad webhook
│  (per-provider)     │
└────────┬────────────┘
         │ record_payment()
         ▼
┌─────────────────────┐
│  Brand Balance      │  ← SQLite tracking (already exists)
│  (sweepable_amt)    │
└────────┬────────────┘
         │ sweep triggered (threshold or schedule)
         ▼
┌─────────────────────┐
│  Crypto Gateway     │  ← Monero wallet RPC / Bitcoin RPC
│  (sweep executor)   │
└────────┬────────────┘
         │ XMR tx or BTC+CoinJoin
         ▼
┌─────────────────────┐
│  Creator Wallet     │  ← Creator's Monero address (from config)
└─────────────────────┘
```

## Implementation Plan

### Phase 1: Payment Gateway Abstraction Layer
- [ ] Create `src/monai/payments/` package with `__init__.py`
- [ ] Create `src/monai/payments/base.py` — abstract `PaymentProvider` interface
  - `verify_payment(payment_ref) -> PaymentStatus`
  - `get_balance(account_id) -> float`
  - `create_payment_link(amount, product, currency) -> str`
  - `handle_webhook(payload, headers) -> WebhookEvent`
- [ ] Create `src/monai/payments/types.py` — shared types (PaymentStatus, WebhookEvent, etc.)

### Phase 2: Stripe Integration (card payments — most customers)
- [ ] Create `src/monai/payments/stripe_provider.py`
  - Stripe Checkout Session creation (payment links)
  - Webhook handler (checkout.session.completed, charge.refunded)
  - Balance retrieval via Stripe API
  - Payout tracking
- [ ] Add `stripe>=8.0.0` to pyproject.toml dependencies
- [ ] Tests: `tests/test_stripe_provider.py`

### Phase 3: Monero Integration (anonymous sweep to creator)
- [ ] Create `src/monai/payments/monero_provider.py`
  - Wallet RPC client (create wallet, get address, get balance)
  - `send_transfer(dest_address, amount)` — actual XMR transaction
  - `verify_incoming(tx_hash)` — confirm payment received
  - Per-brand wallet generation (subaddresses)
- [ ] Add `monero>=1.1.0` (monero-python) to pyproject.toml
- [ ] Add `MoneroConfig` to config.py (wallet_rpc_url, wallet_rpc_port)
- [ ] Tests: `tests/test_monero_provider.py`

### Phase 4: Sweep Engine (automated brand → creator transfers)
- [ ] Create `src/monai/payments/sweep_engine.py`
  - `SweepEngine` class coordinating the full sweep flow
  - Threshold-based triggers (sweep when balance > X EUR)
  - Schedule-based triggers (sweep every N days)
  - Full flow: check balance → convert to XMR if needed → send to creator wallet
  - Retry logic with exponential backoff
  - Status tracking (pending → mixing → completed / failed)
- [ ] Wire `SweepEngine` into `BrandPayments.initiate_sweep()` and `complete_sweep()`
- [ ] Tests: `tests/test_sweep_engine.py`

### Phase 5: BTCPay Server Integration (self-hosted crypto payments)
- [ ] Create `src/monai/payments/btcpay_provider.py`
  - Create invoice (payment request)
  - Webhook handler (InvoiceSettled, InvoicePaymentSettled)
  - Balance check
  - Supports BTC + optional Lightning Network
- [ ] No external dependency needed (REST API via httpx)
- [ ] Tests: `tests/test_btcpay_provider.py`

### Phase 6: Platform Payout Integrations (Gumroad, LemonSqueezy)
- [ ] Create `src/monai/payments/gumroad_provider.py`
  - Webhook handler (sale, refund)
  - Sales verification via API
  - Payout tracking
- [ ] Create `src/monai/payments/lemonsqueezy_provider.py`
  - Webhook handler (order_created, subscription_payment_success)
  - Order verification
  - Payout tracking
- [ ] Tests for both

### Phase 7: Webhook Server (receives payment notifications)
- [ ] Create `src/monai/payments/webhook_server.py`
  - Lightweight HTTP server (using built-in or httpx/starlette)
  - Routes: POST /webhooks/stripe, /webhooks/btcpay, /webhooks/gumroad, /webhooks/lemonsqueezy
  - Signature verification per provider
  - Dispatches to appropriate provider handler
  - Logs all webhook events
- [ ] Add webhook URL configuration to brand accounts

### Phase 8: Wire into Orchestrator
- [ ] Create `src/monai/payments/manager.py` — unified PaymentManager (replaces/extends old payments.py)
  - Provider registry (register providers at startup)
  - Sweep scheduling (APScheduler integration)
  - Creator wallet configuration
  - Payment link generation per brand
- [ ] Wire into orchestrator cycle (new payment phase)
- [ ] Update brand_payments.py to use real providers

### Phase 9: Creator Wallet Config
- [ ] Add `CreatorWalletConfig` to config.py
  - `xmr_address: str` — creator's Monero receive address
  - `btc_address: str` — fallback Bitcoin address
  - `sweep_threshold_eur: float = 50.0` — minimum sweep amount
  - `sweep_interval_hours: int = 24`
- [ ] Add save/load support in Config

### Phase 10: Integration Tests & Verification
- [ ] End-to-end test: Stripe payment → record → sweep → Monero transfer
- [ ] End-to-end test: BTCPay payment → record → sweep → Monero transfer
- [ ] Verify all webhook signature verification works
- [ ] Verify sweep engine handles failures gracefully

## Priority Order
1. Phase 1 + 3 + 9 (abstraction + Monero + config) — core sweep capability
2. Phase 4 (sweep engine) — automated transfers
3. Phase 2 (Stripe) — most common payment method
4. Phase 5 (BTCPay) — crypto-native customers
5. Phase 7 (webhooks) — real-time payment notifications
6. Phase 6 (platforms) — Gumroad/LemonSqueezy
7. Phase 8 (orchestrator) — tie it all together
8. Phase 10 (tests) — verify everything works

## Review
<!-- Post-completion review -->
