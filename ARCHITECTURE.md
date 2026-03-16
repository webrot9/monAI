# monAI — Architecture & Session Documentation

## What is monAI?

monAI is a **fully autonomous money-making AI agent system**. It operates with zero human intervention: it discovers opportunities, creates businesses, manages payments, and transfers profits to its creator — all while maintaining complete anonymity and legal compliance.

The creator provides initial seed capital (via anonymous crowdfunding donation). From that point forward, monAI does everything: registers on platforms, creates accounts, builds products, finds clients, manages finances, and pays the creator through a multi-layer corporate structure.

## Core Objectives

1. **Full Autonomy**: monAI does EVERYTHING by itself. No human sets up accounts, configures APIs, or manages operations. Sub-agents handle specialized tasks. The system self-bootstraps from zero.

2. **Creator Anonymity**: The creator must be COMPLETELY untraceable. All network traffic goes through Tor. Each brand has its own identity. The LLC structure shields the creator. Payments flow through anonymous layers.

3. **Legal Compliance**: Every action must be legal in the creator's jurisdiction (Italy/EU). A Legal Advisor agent reviews every new activity before it proceeds. Tax obligations are tracked and automated.

4. **Self-Sustainability**: Starting from €500 seed capital, monAI must become profitable and fund itself. API costs, infrastructure, and all operations must be covered by revenue.

5. **Zero AI Slop**: Everything monAI produces — code, content, client deliverables — must be indistinguishable from expert human work. All code is tested. All content is fact-checked and humanized.

6. **Creator Protection**: The creator must NEVER face legal issues, financial losses, or reputational damage. monAI absorbs all risk. Ethics are hardcoded and non-negotiable.

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      ORCHESTRATOR                           │
│  Master brain: strategy selection, opportunity discovery,   │
│  agent coordination, infrastructure management              │
└─────────┬───────────────────────────────────────────────────┘
          │
    ┌─────┴──────────────────────────────────────────┐
    │              AGENT TEAMS                        │
    ├─────────────────────────────────────────────────┤
    │  Research Team    │ Market, trends, competitors │
    │  Engineering Team │ Code, bugs, testing         │
    │  Marketing Team   │ Content, growth, outreach   │
    │  Specialist Agents│ Legal, ethics, humanizer,   │
    │                   │ fact-checker, finance, etc.  │
    └─────────────────────────────────────────────────┘
          │
    ┌─────┴──────────────────────────────────────────┐
    │           13 STRATEGY AGENTS                    │
    │  freelance_writing, digital_products,           │
    │  content_sites, micro_saas, saas, affiliate,    │
    │  newsletter, lead_gen, social_media,            │
    │  course_creation, domain_flipping,              │
    │  print_on_demand, telegram_bots                 │
    └─────────────────────────────────────────────────┘
          │
    ┌─────┴──────────────────────────────────────────┐
    │           BUSINESS LAYER                        │
    ├─────────────────────────────────────────────────┤
    │  Finance         │ Revenue & expense tracking   │
    │  Commercialista  │ Accounting & budgets         │
    │  Corporate       │ LLC, expenses, tax filings   │
    │  Bootstrap       │ Seed capital & crowdfunding  │
    │  CRM + Pipeline  │ Leads & conversion tracking  │
    │  Risk            │ Diversification & stop-loss  │
    │  Projections     │ Financial forecasting        │
    │  Invoicing       │ Creator contractor invoices  │
    └─────────────────────────────────────────────────┘
          │
    ┌─────┴──────────────────────────────────────────┐
    │           PAYMENT PIPELINE                      │
    ├─────────────────────────────────────────────────┤
    │  Collection: Stripe, BTCPay, Gumroad,           │
    │              LemonSqueezy (per brand)            │
    │  Sweep Engine: Automated brand → creator        │
    │  Flows:                                         │
    │    • LLC flow: Brand → LLC bank → contractor    │
    │      invoice → creator bank (P.IVA forfettario) │
    │    • Crypto flow: Brand → Monero → creator      │
    │    • Mixed: LLC expenses + invoicing + rotation  │
    │  Webhook Server: Real-time payment notifications│
    └─────────────────────────────────────────────────┘
```

## Bootstrap & Funding Flow

```
PHASE 1: Seed Capital
  Option A (RECOMMENDED): Creator donates on Ko-fi as "Anonymous"
    → Tracked internally as creator_seed
    → Indistinguishable from organic backers
    → No prepaid card needed

  Option B: Paysafecard voucher (€50, tabaccheria, no ID)
    → Only for domain + hosting of crowdfunding page
    → Retired once crowdfunding is active

  Option C: Both — Paysafecard for initial domain, then crowdfunding

PHASE 2: AI Crowdfunding
  → monAI openly declares itself as an AI
  → "The first AI-funded startup" — viral angle
  → Platforms: Ko-fi (0%), Buy Me a Coffee (5%), Gumroad (10%), GitHub Sponsors (0%)
  → No LLC required for any of these
  → Funds used for: LLC formation, registered agent, bank, first months

PHASE 3: Self-Sustaining
  → All revenue through LLC bank
  → Prepaid retired, crowdfunding optional
  → monAI pays its own bills from earnings
```

## Payment & Payout Structure

### Triple-Layer Payout Strategy
1. **LLC Expenses** (tax-free for creator): LLC buys hardware, software, hosting for the creator — not taxable income
2. **P.IVA Forfettario Invoicing**: Creator invoices LLC as contractor — 5% tax for first 5 years, then 15%
3. **Multi-LLC Rotation**: Round-robin invoicing across multiple LLCs to avoid single-client suspicion

### Tax Compliance (Automated)
- **US LLC (Wyoming)**: Form 5472 + pro-forma 1120 (June 15), annual report ($60/yr), registered agent ($150/yr)
- **Italian P.IVA**: Acconto/saldo (June 30, Nov 30), INPS contributions, dichiarazione dei redditi

## Content Quality Pipeline

```
Content Generation → ProductReviewer (Humanizer + FactChecker + Legal) → Publish/Revise/Block
                                          ↓
                              ProductIterator (monitors sales/quality/customer feedback)
                                          ↓
                    Competitor Analysis (persistent DB) → Improvement Plan → Rebuild
                                          ↓
                              Customer Feedback Loop (ratings, NPS, refund reasons)
```

All 11 content/product-producing strategies pass through the ProductReviewer quality gate.
The ProductIterator runs every 5 cycles to identify underperformers and trigger improvements.

### Self-Improvement A/B Testing (Statistical Rigor)
- **Welch's t-test** for comparing before/after metrics (no scipy dependency)
- **Minimum sample size** (N≥10) before making deployment decisions
- **Bonferroni correction** for multiple metric comparisons
- **Variance/stdev analysis** to flag high-noise results
- **Early stop** support: experiments resolved early if p < 0.01
- **SharedMemory integration**: experiment results written as knowledge + lessons

### Customer Feedback Loop
- `product_reviews` table extended with: `customer_rating`, `customer_feedback`, `nps_score`, `support_tickets`
- `record_customer_feedback()` method for webhooks/support systems to feed real customer data
- `get_customer_sentiment()` aggregates rating, NPS, tickets, and recent feedback
- Customer voice prioritized over internal reviews in product iteration prompts

### Competitor Tracking Database
- `competitors` table: persistent, queryable, with UNIQUE constraint per product/competitor
- `competitor_history` table: tracks pricing, features, rating changes over time
- `get_competitor_trends()`: retrieves competitor data with historical changes
- Web search results automatically persisted with change detection

- **Humanizer**: Ensures content passes AI detection tools
- **FactChecker**: Extracts claims, verifies each one, tracks per-brand accuracy
  - Verdict: publish (≥80% verified), revise (>30% unverifiable), block (any false claims)
  - 8 claim categories: statistic, attribution, historical, scientific, comparative, financial, legal, technical

## Key Technical Details

### Agent System
- All agents inherit from `BaseAgent` which provides:
  - LLM reasoning (think, think_json, think_cheap)
  - Real-world actions (execute_task, browse_and_extract, search_web, platform_action)
  - Self-provisioning (ensure_platform_account, get_platform_credentials)
  - Lazy-loaded executor, identity manager, provisioner, and coder
  - Collaboration, learning, journaling
- **ZERO simulation**: All strategy agents use real browser automation and API calls — never LLM hallucination for market data
- Orchestrator manages lifecycle of all agents
- Agents collaborate via `CollaborationHub` and `SharedMemory`
- Ethics enforced at BaseAgent level; `EthicsTester` destroys agents that fail

### Database
- SQLite via `Database` class in `db/database.py`
- Each module creates its own tables via schema scripts
- Stored at `~/.monai/monai.db`

### Privacy
- All traffic through Tor (default) or SOCKS5/HTTP proxy
- User-agent rotation, WebRTC disabled, DNS over proxy
- Metadata stripped from all output files
- Anonymity verified before operations start

### Communication
- Creator contacted via Telegram bot (self-provisioned)
- Bot uses verification code from `~/.monai/verify.txt`
- Creator username: (configured at runtime via ~/.monai/config.json)

### Config
- Dataclass-based config in `config.py`
- Stored at `~/.monai/config.json` (secrets encrypted at rest via Fernet)
- Sections: LLM, risk, comms, privacy, telegram, LLC, bootstrap_wallet, creator_wallet, monero, btcpay, budget
- Structured LLM outputs via Pydantic models in `models.py`

## Module Index

### Agents (`src/monai/agents/`)
| Module | Purpose |
|--------|---------|
| `orchestrator.py` | Master brain — strategy selection, agent coordination |
| `provisioner.py` | Infrastructure provisioning (domains, APIs, accounts) |
| `executor.py` | Task execution with browser control + run_page_script (code-gen for DOM interaction) |
| `identity.py` | Identity & credential management |
| `coder.py` | Code generation with mandatory testing |
| `humanizer.py` | Content humanization (anti-AI-detection) |
| `fact_checker.py` | Content verification before publication |
| `legal.py` | Per-activity legal compliance advisor |
| `ethics.py` | Hardcoded ethical rules + `is_script_ethical()` for reviewing all generated code |
| `ethics_test.py` | Ethical testing framework |
| `self_improve.py` | Agent self-improvement with A/B experiments |
| `product_iterator.py` | Continuous product improvement (sales analysis, competitor monitoring, auto-iteration) |
| `product_reviewer.py` | Quality gate for all products (humanizer + fact-checker + legal review) |
| `llc_provisioner.py` | Autonomous LLC formation |
| `phone_provisioner.py` | Virtual phone number acquisition |
| `finance_expert.py` | Financial advisory and ROI analysis |
| `social_presence.py` | Brand social accounts |
| `web_presence.py` | Domain & website management |
| `browser_learner.py` | Adaptive browser automation with code-gen fallback (writes + caches Playwright scripts when standard selectors fail) |
| `memory.py` | Shared knowledge base |
| `collaboration.py` | Agent-to-agent help system |
| `spawner.py` | Sub-agent creation |
| `base.py` | Base agent class with lazy-loaded mixins (coder, executor, identity, provisioner) |
| `api_provisioner.py` | Autonomous payment provider API key self-provisioning |
| `captcha_solver.py` | Autonomous CAPTCHA solving for account registration |
| `proof.py` | Proof-of-completion — catches executor hallucinations by verifying done() claims against DB, action trail, and page state |
| `email_verifier.py` | Email verification (Mailslurp API primary, IMAP fallback, mail.tm legacy) |
| `eng_team/` | Engineering team (tech lead + engineers) |
| `research_team/` | Research team (market, trends, competitors) |
| `marketing_team/` | Marketing team (content, growth, outreach) |

### Business (`src/monai/business/`)
| Module | Purpose |
|--------|---------|
| `finance.py` | Revenue & expense tracking + double-entry GeneralLedger + multi-brand P&L segmentation |
| `commercialista.py` | Accounting, budgets, ROI per agent |
| `corporate.py` | LLC management, expenses, tax obligations |
| `bootstrap.py` | Seed capital (crowdfunding, Paysafecard, creator seed) |
| `kofi.py` | Ko-fi campaign automation (setup, monitoring, donation sync) |
| `reporting.py` | Automated financial reporting (P&L, balance sheet, strategy dashboards via Telegram) |
| `exchange_rates.py` | Multi-currency exchange rate service (EUR/USD/BTC/XMR with caching, persistence, rate limiting) |
| `reconciliation.py` | Reconciliation engine (matches GL entries with webhook events, finds discrepancies) |
| `crm.py` | Lead management, contacts, pipeline |
| `pipeline.py` | Conversion funnel tracking |
| `risk.py` | Diversification, spend limits, stop-loss |
| `projections.py` | Financial forecasting |
| `strategy_lifecycle.py` | Strategy state machine (pending→active→paused→stopped) |
| `invoicing.py` | Invoice generation (HTML + PDF via weasyprint, client + contractor invoices) |
| `tax_estimation.py` | Quarterly tax estimation (Italian forfettario + US federal, SE tax, brackets) |
| `audit.py` | Audit trail (queryable activity log, risk assessment, per-agent summaries, Telegram reports) |
| `backup.py` | Automated backup & restore (SQLite online backup, config backup, rotation, integrity verification) |
| `alerting.py` | Alerting rules engine (configurable threshold-based alerts, cooldown dedup, severity levels, Telegram integration) |
| `spending_guard.py` | Spending cap enforcement (daily, per-transaction, per-strategy limits with approval thresholds) |
| ~~`payments.py`~~ | **REMOVED** — superseded by `payments/manager.py` + `business/brand_payments.py` |
| `brand_payments.py` | Per-brand payment accounts |
| `comms.py` | Email engine (SMTP/IMAP) |
| `email_marketing.py` | Email campaigns & subscriber lists |

### Payments (`src/monai/payments/`)
| Module | Purpose |
|--------|---------|
| `manager.py` | Unified payment manager |
| `stripe_provider.py` | Stripe card payments |
| `btcpay_provider.py` | BTCPay Server (Bitcoin/Lightning) |
| `gumroad_provider.py` | Gumroad sales |
| `lemonsqueezy_provider.py` | LemonSqueezy payments |
| `monero_provider.py` | Monero privacy-first crypto |
| `kofi_provider.py` | Ko-fi webhook handler (donation/subscription verification) |
| `sweep_engine.py` | Automated profit sweeping |
| `webhook_server.py` | Webhook handler for all providers (with audit trail integration) |

### Dashboard (`src/monai/dashboard/`)
| Module | Purpose |
|--------|---------|
| `server.py` | Real-time web UI (SSE, financial overview, strategies, audit trail, brand P&L, backup status, alerts, webhook replay) |

### Strategies (`src/monai/strategies/`)
13 strategy agents, each implementing a FULLY FUNCTIONAL autonomous revenue channel.
All strategies use real browser automation and APIs — zero simulation:

| Strategy | Real Actions |
|----------|-------------|
| `freelance_writing` | Browses Upwork/Fiverr/Freelancer for real gigs, submits real proposals, delivers work on platforms |
| `digital_products` | Creates products, lists on real Gumroad via API, tracks real sales |
| `content_sites` | Researches keywords via real SEO tools, writes content, finds real affiliate programs |
| `micro_saas` | Builds MVPs, deploys to Railway/Render/Vercel, creates landing pages |
| `saas` | Researches real competitors on G2/Capterra, builds and deploys real products |
| `affiliate` | Browses real affiliate networks (ShareASale, CJ, Amazon), writes real reviews |
| `newsletter` | Researches Substack/Beehiiv trends, writes real issues, finds real sponsors |
| `lead_gen` | Scrapes real business directories, enriches leads via web, qualifies with real data |
| `social_media` | Posts real content via social APIs (Twitter, LinkedIn, Reddit) |
| `course_creation` | Researches real Udemy/Skillshare trends, writes lessons, lists on platforms |
| `domain_flipping` | Browses real expired domain sites, checks real metrics, lists on Sedo/Dan.com |
| `print_on_demand` | Researches real POD trends, generates designs, lists on Redbubble/TeeSpring |
| `telegram_bots` | Researches real bot market, builds bots, deploys via BotFather |

### Web (`src/monai/web/`)
| Module | Purpose |
|--------|---------|
| `landing/index.html` | Crowdfunding landing page (static, self-contained, dark theme) |
| `landing/generator.py` | Dynamic page generator — fills payment links, funding progress from DB |
| `landing/deploy.py` | Deployment helper for Netlify, Vercel, Cloudflare Pages |

### Workflows (`src/monai/workflows/`)
| Module | Purpose |
|--------|---------|
| `workflows/` | Task orchestration, pipeline definitions, and workflow engine |

### Integrations (`src/monai/integrations/`)
| Module | Purpose |
|--------|---------|
| `base.py` | PlatformConnection + PlatformIntegration ABC (per-agent connections, rate limiting, retry) |
| `gumroad.py` | Gumroad API (products CRUD, sales, subscribers, revenue) |

### Utils (`src/monai/utils/`)
| Module | Purpose |
|--------|---------|
| `llm.py` | OpenAI integration, model tiers (FULL/MINI/NANO), CostTracker with save/load, BudgetExceededError |
| `crypto.py` | Fernet config encryption (auto-key, sensitive field detection, ENC: prefix) |
| `browser.py` | Playwright browser automation |
| `privacy.py` | Tor/proxy anonymization, fallback chain (Tor→residential→datacenter→free) |
| `free_proxies.py` | Auto-scraping free proxy pool (geonode, free-proxy-list, sslproxies) |
| `resources.py` | CPU/memory/disk monitoring |
| `sandbox.py` | Sandboxed execution |
| `telegram.py` | Telegram Bot API client |

## Cost Management

### Model Tiers
- **FULL** (`gpt-4o`): Complex reasoning, strategy decisions, evaluations
- **MINI** (`gpt-4o-mini`): Content generation, research summaries, standard tasks
- **NANO** (`gpt-4.1-nano`): Classification, tagging, simple extraction, bulk operations

### Budget Enforcement
- Per-cycle cost limit (`max_cycle_cost`, default €5)
- Per-cycle call limit (`max_cycle_calls`, default 200)
- Never spend >10% of remaining budget in one cycle
- `BudgetExceededError` gracefully stops cycle when limits hit
- CostTracker persists state to `~/.monai/cost_tracker.json`
- Minor costs tracked: platform fees, subscriptions, tools, hosting

### Platform Integrations
- Each agent owns its platform connections (no shared clients)
- `PlatformConnection`: lazy httpx.Client, rate limiting, automatic retry
- Rate limits tracked in DB per platform
- First integration: Gumroad (products, sales, subscribers)

## Test Suite

- **1605 tests** across 80+ test files
- All modules have corresponding test files
- Tests verify actual behavior with real assertions
- Run: `python -m pytest --tb=short`

## Session Continuity Notes

### What's Been Built (as of 2026-03-11)
Everything listed above is implemented, tested, and passing. The codebase is functional from config through to payment sweep.

**Major refactor completed 2026-03-10**: All 13 strategy agents rewired from simulated/hallucinated operations to REAL browser automation and API integrations. BaseAgent now provides `execute_task()`, `browse_and_extract()`, `search_web()`, `ensure_platform_account()`, and `platform_action()` to all agents. Zero simulation remaining.

**Sprint 1+2 bug fixes completed 2026-03-11**: Decimal precision for all financial amounts, PaymentIntent/webhook amount validation (NaN/inf/zero/negative/bounds), Gumroad parsing robustness, currency-aware sweepable balances, refund-after-sweep deficit tracking, reconciliation webhook-only filter, rate limiter memory protection, spending cap enforcement.

**Half-done features completed 2026-03-11**: Dashboard HTML enhancements (audit trail panel, brand P&L table, backup status widget), alerting rules engine (configurable thresholds, cooldown dedup, severity levels), webhook replay (single/batch replay, replayable event listing), spending guard module.

**Sprint 4 completed 2026-03-11**: Team agents enhanced with real data-driven logic — GrowthHacker experiment insights (win rates by type, winning patterns), ContentMarketer SEO validation (word count, keyword density with word-boundary regex, readability, heading structure), OutreachSpecialist prospect segmentation (channel routing, deduplication, follow-up templates), AgentSpawner structured task decomposition (numbered/bullet/and-separated lists, topological sort dependency resolution).

**Sprint 5 completed 2026-03-11**: Crowdfunding landing page enhancements — client-side QR code generation for Monero payment modal (pure JS, canvas-based), crowdfunding campaign management with atomic contribution recording and auto-funded status, fixed DB API calls (fetch_all→execute, positional→dict-key row access).

**Sprint 6 completed 2026-03-11**: ProxyFallbackChain already fully implemented — 22 tests added covering Tor→residential→datacenter proxy fallback, per-domain blocking, block page content detection (≥2 pattern matches), thread safety, preferred proxy after success.

**Sprint 8 completed 2026-03-16**: Self-healing strategy management — removed static `TOR_BLOCKED_STRATEGIES` blocklist that prevented 8 strategies from ever running. Replaced with dynamic self-healing: strategies are allowed to try, auto-pause after 3 real proxy failures, and periodically retry with exponential backoff (1h → 2h → ... → 24h cap). Added free proxy auto-scraping (`FreeProxyPool`) as a 4th fallback tier (Tor → residential → datacenter → free). The system now self-procures free SOCKS5/HTTPS proxies from public lists (geonode, free-proxy-list.net, sslproxies.org), validates them, tracks reliability, and uses them automatically when Tor is blocked and no paid proxy is configured. 27 new tests covering self-healing, free proxy fallback, proxy failure detection, and pool management.

**Sprint 7 completed 2026-03-11**: APIProvisioner already fully implemented — 49 tests added covering schema init, plan generation, encrypted key storage/retrieval, key rotation, provider dispatching, webhook URL building, brand email resolution, result key parsing, BTCPay provisioning, provision_all orchestration. Fixed 3 production bugs (sqlite3.Row .get() → bracket access).

### What's Next
1. **First real deployment test** (end-to-end with a real Ko-fi page)

### Key Design Decisions Made
- **OpenAI, not Claude**: All LLM calls use OpenAI SDK (gpt-4o / gpt-4o-mini / gpt-4.1-nano)
- **Creator donates via crowdfunding**: Simplest bootstrap — no Paysafecard needed
- **Multi-LLC rotation**: Avoids single-client invoice pattern suspicion
- **P.IVA forfettario**: 5% tax first 5 years, creator invoices as contractor
- **Ko-fi preferred**: 0% platform fee for donations
- **Wyoming LLC**: No public member disclosure
- **Paysafecard optional**: Only if creator needs domain before crowdfunding is set up
- **Config encryption**: Fernet with auto-generated key at `~/.monai/.config_key`
- **Per-agent connections**: Each agent manages its own platform connections with rate limiting
- **Strategy state machine**: Formal lifecycle (pending→active→paused→stopped) prevents invalid transitions
- **Zero simulation**: All strategies use real browser/API actions, never LLM hallucination for market data
- **Only OpenAI key provided**: Agents self-provision all other credentials via browser automation
- **Mailslurp for email**: API-based persistent inboxes instead of browser-based Gmail/Outlook signup (which fails via Tor). Requires MAILSLURP_API_KEY env var or config.

## Recent Changes

### Product Quality & Iteration (2026-03-12)
- **ProductReviewer integrated into ALL content strategies**: affiliate, content_sites, newsletter, freelance_writing, social_media, print_on_demand now pass through quality gate (humanizer + fact-checker + legal review) before publishing
- **ProductIterator agent**: Continuous product improvement engine — monitors sales/quality, analyzes competitors, identifies gaps, generates improvement plans, feeds back to strategies
- **Phase 6.58 in orchestrator**: Product iteration runs every 5 cycles — auto-detects underperformers, triggers competitor analysis and improvement cycles
- **Newsletter review fix**: `.get()` on sqlite3.Row replaced with bracket access
- **Draft-first review**: Newsletter issues are reviewed before growth/sponsoring activities
- **26 new tests**: Full coverage for ProductIterator and content review integration

### Security Hardening (Critical)
- **Webhook signature enforcement**: ALL providers now REJECT unsigned webhooks (was: optional)
- **Atomic webhook idempotency**: Idempotency check + event log in single DB transaction (was: separate, racy)
- **PaymentIntent validation**: Amount validated on creation (min €0.01, max €100k, no NaN/negative)
- **Webhook amount validation**: Rejects negative amounts and suspiciously large (>€1M) webhook claims
- **Rate limiting on webhook server**: Per-IP rate limiting (10/sec, 200/min) with 429 responses
- **Spending caps**: Hard daily limit on auto-reinvestment, per-transaction max, creator approval above threshold

### Financial Fixes
- **Currency mismatch fix**: Platform fees now always in same currency as payment (was: hardcoded per-provider)
- **Contractor rate cap**: Percentage capped at 100% (was: uncapped, could exceed revenue)
- **Negative sweep validation**: Rejects negative/zero sweep amounts
- **Transactional payment recording**: Payment + fee recorded atomically in single transaction
- **Refund-after-sweep detection**: Logs CRITICAL alert if refund occurs after funds already swept
- **Dispute handling**: Proper alerting with CRITICAL log level on disputes

### Feature Completions
- **LemonSqueezy full integration**: Auto-creates products and variants (was: checkout-only)
- **LemonSqueezy platform integration**: `integrations/lemonsqueezy.py` for strategy agents
- **Tor detection fallback**: `ProxyFallbackChain` — auto-falls back Tor → residential → datacenter → free proxies
- **Crowdfunding landing page**: `web/landing/` — deployable static site with payment integration
- **Team agents with real logic**: Engineering, research, marketing teams now use browser automation
- **API key self-provisioning**: `agents/api_provisioner.py` — autonomous provider registration

### Integration Fixes (2026-03-11)
- **APIProvisioner wired into orchestrator**: Now runs every 5 cycles, auto-provisions Stripe/Gumroad/LemonSqueezy/BTCPay API keys for brands
- **LemonSqueezy auto-registered in PaymentManager**: On startup, checks DB for provisioned LS keys and registers brand-specific providers
- **Landing page generator wired into WebPresence**: `run()` now regenerates crowdfunding page with live funding data; new `deploy_crowdfunding_page()` method
- **Double-entry bookkeeping (GeneralLedger)**: Full chart of accounts, journal entries with balanced debit/credit, trial balance, balance sheet, income statement, reconciliation, integrity verification
- **Ledger integrity check in orchestrator cycle**: Phase 6.95 verifies all entries balanced before commercialista report
- **GL auto-entries on webhooks**: Every payment_completed webhook auto-creates a GL entry (cash debit + revenue credit + fee debit)
- **GL auto-entries on sweeps**: Successful sweeps auto-create GL entries (creator payable debit + cash credit)
- **GL refund reversals**: Refund webhooks auto-create reversal GL entries
- **Ko-fi campaign automation**: `KofiCampaignManager` agent — auto-registers on Ko-fi, creates campaign page, syncs donations into bootstrap wallet
- **Ko-fi wired into orchestrator bootstrap**: Pre-bootstrap phase auto-triggers Ko-fi setup; donation sync every 3 cycles
- **E2E payment flow tests**: 13 tests covering webhook→GL→sweep→GL→balance-sheet lifecycle
- **GL wired into bootstrap wallet**: Contributions → crowdfunding revenue GL; creator seed → equity GL; spending → expense GL
- **FinancialReporter module**: Monthly P&L + balance sheet, daily snapshots, strategy dashboards — all via Telegram
- **Strategy performance analysis**: Per-strategy ROI, 7d/30d trends, auto-recommendations (continue/review/pause/scale)
- **Phase 6.97**: Strategy performance eval in orchestrator cycle — logs underperformers and growth candidates
- **Phase 7.5**: Automated report dispatch — monthly, weekly dashboard, daily snapshot every 10 cycles
- **Auto-pause underperformers**: Phase 6.97 now calls `lifecycle.pause()` on strategies recommended for pause, with Telegram notification to creator
- **Ko-fi webhook provider**: `KofiProvider` handles Ko-fi form-encoded webhooks with verification_token constant-time comparison
- **All 6 payment providers** have HMAC/token signature verification: Stripe (v1+timestamp), BTCPay (sha256= prefix), Gumroad, LemonSqueezy, Ko-fi (token), Monero (confirmations-based)
- **ExchangeRateService**: Multi-currency support with memory+DB caching, fallback rates, inverse pair computation, rate history
- **`get_income_statement_normalized()`**: GL income statement with FX conversion to target currency (EUR default)
- **Live rate fetching**: ECB (EUR/USD fiat) + CoinGecko (BTC/XMR crypto) with async httpx, runs every 6 cycles
- **Auto-scale strategies**: Phase 6.97 boosts budget +20% for growing strategies (capped, allocation-limited)
- **ReconciliationEngine**: Matches GL entries (by `reference`) to `webhook_events` (by `payment_ref`), flags mismatches/orphans
- **Weekly reconciliation**: Runs every Monday, sends Telegram alert only if discrepancies found

### Earlier Changes
- **Webhook idempotency**: `processed_webhooks` table prevents double-processing
- **Decimal support**: Financial precision via `_to_decimal` and `amount_decimal` properties
- **Gumroad webhook verification**: HMAC-SHA256 signature verification
- **Platform fee tracking**: `platform_fees` table, auto-recorded on payment
- **Database performance indexes**: Added on frequently-queried columns
- **HTTP client pooling**: Connection pooling for all payment providers
- **Executor timeout enforcement**: Configurable per-task, default 1h
- **DB transaction helper**: `db.transaction()` context manager for atomic operations
- **Expanded sensitive data filtering**: Regex-based filtering in agent identity

### Creator Preferences (Italian)
- Currency: USD (with EUR conversion when needed)
- Initial budget: $500
- Tax regime: P.IVA forfettario (5% first 5 years)
- Communication: Telegram (username configured at runtime)
- Privacy: Maximum — Tor by default, full anonymity
- Content standard: Zero AI slop, expert human quality
- Strategy scope: ANYTHING legal that makes money — no artificial limits
