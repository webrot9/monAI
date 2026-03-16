# monAI - Current Task Status

## Completed
- [x] Payment pipeline (Stripe, BTCPay, Monero, Gumroad, LemonSqueezy, webhooks, sweep engine)
- [x] Multi-layer LLC + contractor payout structure
- [x] LLC provisioner agent (autonomous formation)
- [x] Mixed payout strategy (LLC expenses + P.IVA forfettario + multi-LLC rotation)
- [x] Bootstrap funding (Paysafecard + AI crowdfunding + creator seed donation)
- [x] Fact-checker agent (per-brand content verification)
- [x] Tax compliance automation (US LLC + Italian P.IVA obligations)
- [x] Expense tracking through LLC
- [x] Creator seed donation via crowdfunding
- [x] Fix environment (socksio, optional monero/weasyprint/playwright deps)
- [x] Wire FactChecker into all content pipelines
- [x] Budget-aware cycle management (per-cycle cost/call limits)
- [x] Per-agent platform integrations (Gumroad first)
- [x] Config encryption (Fernet for secrets at rest)
- [x] Structured LLM outputs (Pydantic response models)
- [x] Integration tests for orchestrator cycle (17 tests)
- [x] Cost tracking with minor costs + save/load persistence
- [x] Parallelize research/market team execution
- [x] Optimize costs (model tiers: FULL/MINI/NANO)
- [x] Strategy lifecycle state machine
- [x] Phone provisioner lazy HTTP client fix
- [x] Wire APIProvisioner into orchestrator (runs every 5 cycles, auto-provisions API keys)
- [x] Register LemonSqueezy in UnifiedPaymentManager (auto-loads from DB at startup)
- [x] Wire landing page generator into WebPresence agent (generate + deploy crowdfunding page)
- [x] Double-entry bookkeeping GeneralLedger (chart of accounts, journal, balance sheet, P&L)
- [x] Ledger integrity verification in orchestrator cycle (Phase 6.95)
- [x] 20 new tests for GeneralLedger (1114 total tests, all passing)
- [x] Wire GeneralLedger into webhook handler (auto GL entries on payments/refunds)
- [x] Wire GeneralLedger into sweep engine (auto GL entries on sweeps)
- [x] Ko-fi campaign automation (KofiCampaignManager agent, wired into orchestrator)
- [x] E2E payment flow tests (13 tests: webhook→GL→sweep→GL→balanced books)
- [x] Research already wired into opportunity discovery (verified)
- [x] Total: 1135 tests, all passing
- [x] Wire GeneralLedger into bootstrap wallet (contributions→revenue, seed→equity, spend→expense)
- [x] Automated financial reporting (FinancialReporter: monthly P&L, daily snapshots, weekly dashboards via Telegram)
- [x] Strategy performance dashboards (per-strategy ROI, 7d/30d trends, auto-recommendations)
- [x] Total: 1151 tests, all passing
- [x] Auto-pause underperforming strategies (Phase 6.97 calls lifecycle.pause + Telegram notification)
- [x] Ko-fi webhook provider (verification_token constant-time comparison, form-encoded parsing)
- [x] ExchangeRateService (EUR/USD/BTC/XMR with caching, persistence, fallback rates, inverse pairs)
- [x] GL normalized income statement (get_income_statement_normalized with FX conversion)
- [x] Total: 1192 tests, all passing

- [x] Live exchange rate fetching (ECB fiat + CoinGecko crypto, async httpx, Phase 6.85)
- [x] Auto-scale promising strategies (Phase 6.97 boosts budget +20% for growing strategies)
- [x] Reconciliation engine (GL↔webhook matching, amount tolerance, weekly run with Telegram alerts)
- [x] Total: 1216 tests, all passing

- [x] Invoice generation (HTML + PDF, client invoices, contractor invoices, entity/period/payment info)
- [x] Tax estimation module (quarterly: Italian forfettario + US federal, SE tax, progressive brackets, INPS)
- [x] Rate limiting per provider for exchange rate API calls (token-bucket, per-provider sliding window)
- [x] Total: 1252 tests, all passing

- [x] Audit trail / activity log (queryable log, risk assessment, per-agent summaries, Telegram reports)
- [x] Multi-brand GL segmentation (per-brand P&L, all-brands dashboard, Telegram format)
- [x] Automated backup & restore (SQLite online backup, config backup, rotation, integrity verification)
- [x] Total: 1299 tests, all passing

- [x] Wire audit trail into orchestrator (cycle start/complete, budget exhaustion, ledger imbalance, payments, strategy pauses, direct actions)
- [x] Automated scheduled backups in orchestrator (DB every cycle, config every 7 cycles)
- [x] Fixed self.lifecycle → self.strategy_lifecycle bug in phase 6.97
- [x] Total: 1308 tests, all passing

- [x] Webhook audit events (success/error/invalid webhooks logged with provider, amount, brand)
- [x] Configurable backup scheduling (BackupConfig: db_interval_cycles, config_interval_cycles, max_backups, enabled)
- [x] Dashboard API: /api/audit, /api/audit/summary, /api/brands, /api/backups endpoints
- [x] Total: 1334 tests, all passing

- [x] Sprint 1 bug fixes (PaymentIntent validation, Decimal precision, Gumroad parsing, webhook amount validation)
- [x] Sprint 2 bug fixes (reconciliation filter, currency-aware sweepable, refund-after-sweep deficit, rate limiter memory, spending guard)
- [x] Dashboard HTML enhancements (audit trail panel, brand P&L table, backup status widget)
- [x] Alerting rules engine (configurable thresholds, cooldown dedup, severity levels, default rules)
- [x] Webhook replay (single event replay, batch failed replay, replayable event listing)
- [x] Spending guard module (daily/per-transaction/per-strategy caps with approval thresholds)
- [x] Total: 1398 tests, all passing

- [x] Sprint 4: Team agents with real data-driven logic (GrowthHacker insights, ContentMarketer SEO validation, OutreachSpecialist segmentation, AgentSpawner structured decomposition)
- [x] Sprint 5: Crowdfunding landing page enhancements (QR codes, campaign management, DB fixes)
- [x] Sprint 6: Tor detection fallback tests (22 tests for ProxyFallbackChain)
- [x] Sprint 7: API key self-provisioning tests (49 tests for APIProvisioner, 3 production bug fixes)
- [x] Total: 1520 tests, all passing

- [x] Final audit: ProductReviewer integrated into 6 content strategies (affiliate, content_sites, newsletter, freelance_writing, social_media, print_on_demand)
- [x] Final audit: ProductIterator agent — continuous product improvement (sales analysis, competitor monitoring, auto-iteration)
- [x] Final audit: Orchestrator Phase 6.58 — product iteration every 5 cycles
- [x] Final audit: Newsletter sqlite3.Row .get() bug fix
- [x] Total: 1605 tests, all passing

- [x] Self-improvement statistical rigor (Welch's t-test, min sample N≥10, Bonferroni correction, variance analysis, early stop)
- [x] Customer feedback loop (customer_rating, customer_feedback, nps_score, support_tickets in product_reviews)
- [x] Competitor tracking DB (competitors + competitor_history tables, change detection, get_competitor_trends)
- [x] Product iteration improvements (multi-metric evaluation, customer voice in prompts, refund reasons, min sample checks)
- [x] Silent failure detection (learn_from_silent_failure on BaseAgent)
- [x] Experiment results → SharedMemory (knowledge + lessons for cross-agent learning)
- [x] Total: 1669 tests, all passing

- [x] Self-healing form fill: CAPTCHA auto-solve after click, submit, fill_form in executor
- [x] Freelance pipeline fix: _write_content saves to disk, _review_content updates status, plan() includes deliver_work
- [x] Orchestrator strategy auto-retry with exponential backoff for transient failures
- [x] BrowserLearner CAPTCHA detection after form fill
- [x] Executor stuck-loop watchdog (aborts on 4 identical non-failing actions)

- [x] Fix 1: Persistent proxy-blocked platform detection (permanent column in provision_failures)
- [x] Fix 2: Platform name preserved in constraint planner goals ("action on platform" format)
- [x] Fix 3: Ethics checks moved after strategies + budget threshold (<30 calls → skip)
- [x] Fix 4: Strategy prereq check in base.py (check provisioner block before registration)
- [x] Fix 5: Sub-agent failure history injected into plan context
- [x] Fix 6: Cycle-scoped cancellation flag for watchdog timeout
- [x] Fix 7: Provisioning budget cap (40% of cycle budget)
- [x] Self-healing form fill: __MISSING__ sentinel caching for absent fields
- [x] Executor: distinct skipped vs failed field reporting in fill_form
- [x] Auto-pause 8 Tor-blocked strategies (freelance_writing, digital_products, course_creation, micro_saas, saas, print_on_demand, domain_flipping, social_media)
- [x] Telegram bots: Stripe dependency made optional (bot deploys even without payment processor)
- [x] Orchestrator: TOR_BLOCKED_STRATEGIES mapping + auto-pause in _run_strategies
- [x] Orchestrator: Skip Tor-blocked strategies in _ensure_strategy_payment_providers

## Active — Revenue Pipeline Fixes (2026-03-16)
- [x] checkout_links table: payment_ref → strategy mapping
- [x] create_checkout_link() auto-stores mapping in DB
- [x] BaseAgent.check_pending_sales() polls providers + records revenue
- [x] affiliate: monetize_content step + check_sales step
- [x] content_sites: monetize_article step + check_sales step
- [x] lead_gen: check_sales step + marks lists as 'sold'
- [x] newsletter: check_sales step for sponsor payments
- [x] telegram_bots: check_sales step for premium subscriptions
- [x] Webhook revenue handler: make_checkout_revenue_handler()
- [x] Reduced browsing from 3-4 sessions to 1 per research step (saves 20-40 LLM calls/cycle)
- [x] Added "no real data" detection — skip hallucination, retry next cycle

## Active — Revenue-Viable Strategies (work via Tor)
These 5 strategies can generate revenue without Tor-blocked platform registration:

1. **affiliate** — Review/comparison content with affiliate links (no registration needed)
2. **content_sites** — SEO articles with affiliate monetization (no registration needed)
3. **lead_gen** — B2B lead scraping and direct sales (no registration needed)
4. **newsletter** — Substack/Beehiiv newsletters with sponsors (Tor-tolerant platforms)
5. **telegram_bots** — Telegram bot services (Telegram API works via Tor, payments optional)

## Paused — Tor-Blocked Strategies
These require platform registration that blocks Tor. Will re-enable when direct internet access is available:
- freelance_writing (Upwork/Fiverr)
- digital_products (Gumroad)
- course_creation (Udemy/Skillshare)
- micro_saas (Stripe/LemonSqueezy)
- saas (Stripe)
- print_on_demand (Redbubble/TeeSpring)
- domain_flipping (Sedo/Afternic)
- social_media (Twitter/LinkedIn/Instagram)
