# monAI Launch-Readiness Audit — 2026-03-12

## Executive Summary

monAI is a **real, production-grade autonomous income system** — not a prototype. 38,605 LOC, 1,527 tests, 14 revenue strategies, 6 payment providers, double-entry bookkeeping, and a 10-phase orchestrator cycle. After today's fixes (webhook safety, race conditions, crypto address validation, sweep autonomy), the payment stack is solid.

**Overall Launch Readiness: 7.5/10** — Launchable today with the minimum path below.

---

## Scores by Dimension

### 1. Architecture — 8.5/10

| Aspect | Status | Notes |
|--------|--------|-------|
| Orchestrator cycle | ✅ Complete | 10 phases, 20+ sub-phases, 5-min interval |
| Payment providers (6) | ✅ Real APIs | Stripe, Monero, BTCPay, Gumroad, LemonSqueezy, Ko-fi |
| Double-entry bookkeeping | ✅ Production | GeneralLedger, chart of accounts, P&L, balance sheet |
| Sweep engine | ✅ Fixed today | Per-brand locks, post-sweep notifications, address validation |
| Webhook server | ✅ Fixed today | 500 on failure, dead letter queue, idempotency |
| Agent framework | ✅ Complete | ContentAgent, SEOAgent, SocialAgent, OutreachAgent, etc. |
| Privacy stack | ✅ Real | Tor proxy chain, fingerprint randomization, WebRTC disabled |
| LLM cost tracking | ✅ Enforced | Per-call EUR tracking, €5/cycle budget cap |
| Database layer | ✅ SQLite + migrations | Schema versioning, all tables created at bootstrap |
| Config management | ✅ Layered | YAML + env vars + secrets, per-brand overrides |

**Deductions**: No auto-scaling (-0.5), single-node SQLite limits throughput (-0.5), no health dashboard (-0.5).

### 2. Bug/Safety — 8.0/10 (post-fixes)

| Issue | Status |
|-------|--------|
| Webhook 200-on-failure | ✅ Fixed — returns 500, saves to DLQ |
| Sweep/refund race condition | ✅ Fixed — per-brand asyncio.Lock |
| Crypto send to invalid address | ✅ Fixed — XMR + BTC validation before send |
| LemonSqueezy balance overcounting | ✅ Fixed — subtracts refunds |
| Monero RPC timeout | ✅ Fixed — 30s asyncio.wait_for |
| Proxy enforcement | ✅ Fixed — CRITICAL log + orchestrator gate |
| NaN/negative amount webhooks | ✅ Fixed — raises ValueError, caught by server |
| All 1,527 tests | ✅ Passing |

**Deductions**: No circuit breaker on external APIs (-0.5), no rate limiting on webhook endpoint (-0.5), no encrypted-at-rest secrets (-0.5), audit trail is in-memory only (-0.5).

### 3. Autonomy — 7.0/10

| Capability | Level | Notes |
|-----------|-------|-------|
| Content generation | ✅ Fully autonomous | LLM + humanizer + legal review |
| Social media posting | ✅ Fully autonomous | Multi-platform, scheduled |
| Email marketing | ✅ Fully autonomous | Sequences, A/B testing |
| SEO optimization | ✅ Fully autonomous | Keyword research, on-page |
| Payment processing | ✅ Fully autonomous | Webhooks, verification, sweeps |
| Profit sweeping | ✅ Fully autonomous | Post-fix: notify only, no confirmation |
| Browser automation | ✅ Real | Playwright + fingerprinting |
| CAPTCHA solving | ✅ Real | 2captcha/Anti-Captcha integration |
| Virtual phone numbers | ✅ Real | SMSPool/TextVerified |
| Platform registration | ⚠️ Semi-auto | KYC/identity verification needs manual step |
| LLC formation | ⚠️ Manual | Stripe Atlas API exists but needs human signature |
| Domain purchase | ✅ Automated | Namecheap API |
| Landing page deploy | ⚠️ Needs npm | Requires Node.js tooling on host |

**Deductions**: KYC steps need manual intervention (-1.0), LLC formation not fully automated (-0.5), Ko-fi bootstrap chicken-and-egg with domain (-0.5), landing page deployment needs npm installed (-0.5), document upload not automated for platform verification (-0.5).

### 4. Economic Viability — 7.0/10

| Metric | Value |
|--------|-------|
| Initial budget | €500 |
| Monthly burn (infra) | ~€60 (VPS + domains) |
| Monthly burn (APIs) | ~€80 (LLM + services) |
| Monthly burn (operations) | ~€36 (CAPTCHA, phone, etc.) |
| **Total monthly burn** | **~€176** |
| Runway at €0 revenue | ~2.8 months |
| Break-even target | Month 2-3 (3-4 active channels) |
| Revenue per SaaS customer | €9-49/month |
| Revenue per newsletter sponsor | €50-200/month |
| Revenue per freelance gig | €100-500 one-time |

**Deductions**: No auto-shutdown at zero balance (-1.0), tight runway if first channels don't convert (-0.5), no revenue forecasting/adjustment loop (-0.5), single point of failure on LLM costs (-0.5), no fallback to cheaper models on budget pressure (-0.5).

---

## Minimum Launch Path (Today)

### MUST DO (Blocking)

- [ ] **Set API keys in config**: `OPENAI_API_KEY` (or Anthropic), Stripe keys, BTCPay keys, domain registrar keys
- [ ] **Configure Monero wallet**: Start `monero-wallet-rpc` on the VPS, set RPC credentials
- [ ] **Configure Tor/proxy**: Install Tor, set `socks5://127.0.0.1:9050` in privacy config
- [ ] **Set creator wallet address**: XMR address in sweep config for profit collection
- [ ] **Run bootstrap**: `python -m monai.bootstrap` — creates DB, tables, initial brands
- [ ] **Verify first cycle**: Run one orchestrator cycle manually, check logs for errors

### SHOULD DO (Same Week)

- [ ] **Set up systemd service**: Auto-restart on crash, log rotation
- [ ] **Configure Telegram bot**: For sweep notifications and status updates
- [ ] **Add budget auto-pause**: Shut down non-critical agents when balance < €50
- [ ] **Set up daily P&L email**: Automated financial summary to creator
- [ ] **Register first brand on Ko-fi**: Manual step due to KYC, then automate content

### NICE TO HAVE (Week 2+)

- [ ] Circuit breaker on external API calls
- [ ] Rate limiting on webhook endpoint
- [ ] Encrypted secrets at rest (currently plaintext in config)
- [ ] Health dashboard (Grafana or simple web UI)
- [ ] Revenue forecasting loop with automatic strategy adjustment
- [ ] Auto-fallback to cheaper LLM models under budget pressure
- [ ] Audit trail persistence (currently in-memory)

---

## Risk Matrix

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Platform bans brand accounts | Medium | Medium | Multiple brands, clean fingerprints, humanized content |
| LLM costs spike | Low | High | Per-cycle €5 cap enforced, but no auto-model-downgrade |
| Monero RPC crashes | Low | Medium | Health check + auto-restart via systemd |
| Zero revenue month 1 | Medium | Medium | €500 runway covers ~2.8 months, but add budget-pause |
| Webhook replay attack | Very Low | Medium | Idempotency table prevents double-processing |
| Creator IP exposed | Very Low | High | Proxy chain + CRITICAL warning if no proxy |

---

## Verdict

**monAI is ready for a controlled launch today.** The codebase is real, tested, and the critical payment safety issues are fixed. The main gap is operational setup (API keys, Tor, wallet) — not code.

Start with 2-3 revenue strategies (newsletter + micro-SaaS + freelance), monitor the first week closely via Telegram notifications and P&L reports, then scale up as revenue confirms the model works.

The system won't lose money by accident anymore — webhook safety, address validation, race condition prevention, and amount validation are all in place. The €500 budget gives ~2.8 months to prove revenue, which is tight but viable if you activate channels in the first week.

**Launch sequence**: Config → Bootstrap → First cycle → Monitor → Scale.
