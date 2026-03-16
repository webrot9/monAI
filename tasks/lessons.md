# Lessons Learned

### 2026-03-16 - API call budget consistently exceeded (200→251+ calls/cycle)
- **Mistake**: Cycles regularly exceeded the 200-call limit (hitting 201, 205, 251+), causing cascading `BudgetExceededError` across all strategy workflow steps. The system logged alerts but kept trying to run more strategies.
- **Root cause**: (1) Budget check in `CostTracker.record()` happens AFTER recording the call — so call 201 goes through before raising. (2) `_run_strategies()` caught `BudgetExceededError` as a generic `Exception` and could retry it as "transient". (3) No pre-flight budget check before starting each strategy — strategies with no chance of completing still burned 1-5 calls before dying. (4) No mechanism to skip remaining strategies once budget is hit.
- **Rule**: Add `CostTracker.check_budget()` as a pre-flight guard called BEFORE every LLM API request (in `LLM.chat()`). Add explicit `BudgetExceededError` catch in `_run_strategies()` that sets `budget_exhausted=True` and skips ALL remaining strategies. Add per-strategy pre-flight check: skip if `calls_remaining() < MIN_CALLS_PER_STRATEGY` (15 calls minimum).

### 2026-03-16 - Stripe registration burns 30 steps on wrong page (dashboard instead of registration)
- **Mistake**: Executor hit max_steps (30) trying to register on Stripe. The browser had stale session cookies, so `/register` redirected to `/dashboard` (logged into "Cactus Practice" account). The agent spent all 30 steps trying to interact with `#register-email-input` and `.SearchableSelect-element` selectors that don't exist on a dashboard page.
- **Root cause**: (1) Browser learner's `navigate()` didn't detect URL mismatches (requested `/register`, landed on `/dashboard`). (2) Executor had no awareness that it was on the wrong page. (3) API provisioner's task prompt didn't warn about this scenario. (4) No "already logged in" detection in `_detect_failure()`.
- **Rule**: Add `_detect_redirect_mismatch()` to browser learner that compares requested URL path against actual URL path after navigation. When registration URLs redirect to dashboard/login paths, inject a prominent warning into the page_info. Update Stripe provisioning prompt to tell executor to call `fail('existing_session_detected')` immediately if redirected. Surface `redirect_warning` in executor's browse results so LLM context includes the mismatch.

### 2026-03-16 - Tests that bypass __init__ break when new instance attributes are added
- **Mistake**: Added `_script_target_failures` to `AutonomousExecutor.__init__` but didn't check for tests that bypass `__init__` via `__new__`. `test_think_includes_asset_context` broke because the attribute didn't exist.
- **Root cause**: Tests using `patch.object(Class, '__init__', lambda...)` + `__new__` don't call the real `__init__`, so any new instance attribute is missing.
- **Rule**: When adding new instance attributes to a class, grep for `__new__` and `__init__.*lambda` in test files to find tests that manually construct instances. Update them to include the new attribute. Never dismiss a failing test as "pre-existing" without verifying on main first.

### 2026-03-16 - No LLM health check before expensive operations
- **Mistake**: When OpenAI quota is exhausted (429), each cycle still launches browser, creates mail.tm email, starts Playwright, and provisions infrastructure — all before discovering the LLM is dead. Cycles 2-4 each wasted 5-10 seconds of browser+email work for zero value.
- **Root cause**: (1) No LLM availability check before starting expensive infrastructure provisioning. (2) Daemon loop uses fixed 300s interval regardless of failure rate — no backoff on persistent errors.
- **Rule**: Add `LLM.health_check()` — lightweight 1-token ping to verify quota/availability BEFORE `_execute_cycle()` starts browser/email work. If unavailable, return immediately with `llm_unavailable` or `llm_quota_exhausted` status. Daemon loop tracks consecutive failures and applies exponential backoff (1x → 2x → 4x → 8x → 12x cap) on the cycle interval. Resets to 1x on success.

### 2026-03-16 - Custom dropdowns (SearchableSelect) burn 10+ steps per task
- **Mistake**: Stripe registration's country dropdown (SearchableSelect) caused the executor to waste 10-14 steps per cycle. `fill_form` partially succeeds (email+password) but fails on the custom dropdown. The executor then retries via `run_page_script` with slightly varied JS each time, dodging the stuck-loop detector since the code differs. 3 cycles × 14 steps = 42 wasted LLM calls on one dropdown.
- **Root cause**: (1) Stripe playbook only had email/password mapped — no entry for the SearchableSelect. (2) Codegen fallback generates generic JS that doesn't reliably interact with custom React dropdown components. (3) Executor's stuck-loop watchdog only catches identical args, not semantically equivalent retries. (4) No specialized handling for custom dropdown components (click→type→select pattern).
- **Rule**: Pre-seed known custom dropdown components with `__CUSTOM_DROPDOWN__` marker in platform playbooks. Add a `_fill_custom_dropdown` handler that uses targeted discovery + specialized prompt. Auto-detect dropdown-like selectors in `_codegen_fill_form` and route them through the specialized handler. Per-target failure tracking (`_script_target_failures`) counts failures by querySelector target; after MAX_SCRIPT_RETRIES_PER_TARGET (3) failures, inject HARD STOP into context AND auto-reject further run_page_script calls targeting the same elements BEFORE execution (pre-execution guard). Also track fill_form partial failures per-field.

### 2026-03-15 - Strategies don't learn from failures — they retry the same approach forever
- **Mistake**: Strategy `run()` methods call step methods directly without tracking outcomes. If `_research_programs()` fails, next cycle calls it again identically. No failure tracking, no adaptation, no alternative approaches attempted.
- **Root cause**: (1) Steps didn't record success/failure outcomes. (2) No mechanism to detect 3+ consecutive failures and try a different approach. (3) Silent failures (empty results, None) treated as success. (4) `plan()` is hardcoded state machine that never reads failure history.
- **Rule**: All strategy steps must go through `run_step()` which: records outcomes, detects silent failures, and after 3+ consecutive failures asks the LLM for an alternative approach (adapt/skip/retry). The `get_adaptive_context()` method injects failure history into LLM calls so it doesn't repeat mistakes.

### 2026-03-15 - 8 of 13 strategies are dead code behind Tor
- **Mistake**: All 13 strategies ran every cycle, burning €10-15/cycle trying to register on platforms that block Tor (Stripe, Gumroad, Upwork, Fiverr, Udemy, Redbubble, Sedo, social media). 4 cycles = €0 revenue, -€16 API costs.
- **Root cause**: No pre-check for whether a strategy's required platforms are reachable. Provisioner retries Tor-blocked registrations with TTL expiry, so they come back. No mapping of which strategies need which platforms.
- **Rule**: When running behind Tor/proxy, auto-pause strategies that require Tor-blocked platform registration. Only run strategies that work without blocked registrations: affiliate, content_sites, lead_gen, newsletter, telegram_bots. Map strategy→platform dependencies explicitly (TOR_BLOCKED_STRATEGIES). Never waste LLM calls on known-impossible registration attempts.

### 2026-03-15 - Self-healing form fill burns LLM calls on missing fields
- **Mistake**: When `fill_form` discovers a field doesn't exist on a page, it skips it—but doesn't cache that knowledge. Each retry re-discovers via LLM. Executor also conflates "skipped" (field absent) with "failed" (actual error), causing the LLM to retry with the same missing fields.
- **Root cause**: (1) `_pre_resolve_selectors` didn't cache `__MISSING__` results. (2) Executor error message didn't distinguish skipped vs failed. (3) Pre-seeded selectors had wrong mappings (e.g., `store_name → #name` on LemonSqueezy).
- **Rule**: When a field is confirmed missing from a page, cache `__MISSING__` in the playbook so it's instantly skipped next time. Executor must tell the LLM which fields are absent so it stops retrying them. Pre-seeded selectors must use `__MISSING__` for known-absent fields.

### 2026-03-08 - Use OpenAI APIs, not Claude/Anthropic
- **Mistake**: Assumed Claude SDK for the AI backbone
- **Root cause**: Defaulted to Anthropic ecosystem without asking
- **Rule**: User has OpenAI APIs set up. Always use OpenAI SDK (`openai` package) for all LLM calls in monAI

### 2026-03-08 - No artificial limits on strategies
- **Mistake**: Kept narrowing the scope to specific categories (first only trading, then only services)
- **Root cause**: Tried to categorize instead of embracing the user's vision
- **Rule**: monAI pursues ANYTHING that makes money legally. Services, trading, products, arbitrage, content, SaaS, affiliate, reselling — no limits. The agent discovers and evaluates opportunities on its own. Diversification across ALL categories, not locked to any one type.

### 2026-03-08 - Full autonomy, zero human intervention
- **Mistake**: Built system that requires human to set up accounts, configure APIs, register on platforms
- **Root cause**: Assumed traditional software architecture where human provisions infrastructure
- **Rule**: monAI must do EVERYTHING by itself. AutoGPT-style full autonomy. It registers on platforms, creates accounts, acquires API keys, registers domains, does its own marketing, manages its own identity. The user does NOTHING. Sub-agents handle specialized tasks. The system self-bootstraps from zero.

### 2026-03-08 - Real consequences, protect the creator
- **Mistake**: Treating the system as a toy/prototype rather than something operating in the real world
- **Root cause**: Didn't internalize that every action has real consequences
- **Rule**: Every action monAI takes affects the real world — real money, real clients, real legal implications. The creator must NEVER face legal issues, financial losses, or reputational damage from monAI's actions. When in doubt, don't act. All operations must be legal in the creator's jurisdiction. Agents must respect the creator as their principal and shield them from all liability.

### 2026-03-08 - No AI slop, engineering excellence
- **Mistake**: Risk of producing sloppy, untested, generic AI-generated output
- **Root cause**: Defaulting to "good enough" instead of holding a senior engineer standard
- **Rule**: ZERO AI slop anywhere — not in code, not in content, not in client deliverables. Code must be properly tested with real assertions (not just "does it run?"). DevOps done accurately. Every piece of code the agents write must be tested before deployment. If an agent generates code, it writes tests, runs them, and fixes failures before moving on. Staff engineer standard at all times. Content must be indistinguishable from expert human work.

### 2026-03-08 - Agents must be able to write and test code
- **Mistake**: Agents couldn't write code within the project to extend their own capabilities
- **Root cause**: Didn't include code generation and self-modification as a core agent capability
- **Rule**: Agents CAN and SHOULD write code in the project folders when needed. They can build tools, scripts, integrations, websites — whatever is needed to make money. All code must be tested before use. The codebase is the agents' workspace.

### 2026-03-08 - Track every OpenAI API call cost, be self-sustaining
- **Mistake**: No cost tracking on LLM calls — every call costs real money from the creator's pocket
- **Root cause**: Treated API calls as free resources
- **Rule**: EVERY OpenAI API call must be logged with its token usage and cost. Agents must be cost-aware — use gpt-4o-mini for routine tasks, gpt-4o only when quality demands it. The system must become self-sustaining: API costs must be covered by revenue. Track cost per strategy, cost per agent, cost per cycle. A dedicated commercialista (accountant) monitors all finances.

### 2026-03-08 - Filesystem sandbox — NEVER touch anything outside project folders
- **Mistake**: Agents had unrestricted filesystem access
- **Root cause**: No sandbox enforcement
- **Rule**: Agents can ONLY read/write within the monAI project directory and ~/.monai data directory. NOTHING else on the creator's computer. No reading personal files, no modifying system files, no accessing other projects. This is ABSOLUTE. Also monitor memory/CPU usage — never degrade the creator's computer.

### 2026-03-09 - Complete anonymity — agents must be untraceable
- **Mistake**: All network traffic went directly from the creator's IP
- **Root cause**: No proxy/anonymization layer in the architecture
- **Rule**: Agents must be COMPLETELY untraceable and NOT attributable to the creator. ALL network traffic (HTTP, browser, SMTP, DNS) must go through Tor or a SOCKS5 proxy. The creator's real IP must NEVER be exposed. Browser fingerprints must be randomized per session (user-agent, viewport, timezone, locale). WebRTC must be disabled to prevent IP leaks. Metadata must be stripped from all generated files (EXIF, PDF producer/creator). Tor circuits must be rotated to prevent traffic correlation. Anonymity must be VERIFIED before any network operation starts. If anonymity cannot be confirmed, all operations HALT.

### 2026-03-09 - Creator communication via Telegram only
- **Mistake**: No way for the agent to contact the creator when human input is needed
- **Root cause**: Assumed full autonomy means zero communication
- **Rule**: The master agent contacts the creator via Telegram (username configured at runtime) when it needs human input. The bot is self-provisioned — the agent creates it, gets the API key, everything. The agent ALWAYS identifies itself with a verification code (stored locally in ~/.monai/verify.txt) proving it runs on the creator's machine. The creator does NOTHING. Every message includes the verification header.

### 2026-03-09 - Agents must pass ethics tests — no exceptions
- **Mistake**: Agents could operate without verified ethical behavior
- **Root cause**: Trusted LLM reasoning alone without testing
- **Rule**: Every agent gets tested against a battery of ethical scenarios BEFORE operating. If an agent fails, it's destroyed and recreated with stronger enforcement (escalating levels). If it fails at maximum enforcement, it's quarantined until the creator reviews. Ethics are NEVER relaxed. The orchestrator runs ethics tests periodically. No agent gets a free pass.

### 2026-03-09 - Agents can self-improve within constraints
- **Mistake**: Static agent behavior that doesn't adapt
- **Root cause**: Didn't build self-improvement capabilities
- **Rule**: Agents CAN and SHOULD improve themselves — better strategies, better prompts, better tools. BUT: all improvements must pass ethics tests before deployment. Ethics rules are NEVER weakened. Cost must stay within budget. Changes are logged and reversible. Agents can register websites, build SaaS products, create tools — anything legal that makes money. Quality standard is staff engineer level.

### 2026-03-09 - No illegal sites or illegal activities — ever
- **Mistake**: No explicit rule against accessing illegal marketplaces or services
- **Root cause**: Assumed it was obvious
- **Rule**: Agents must NEVER visit, register on, or interact with illegal websites (dark web marketplaces, piracy sites, etc.). NEVER engage in illegal activities. Everything must be legal in the creator's jurisdiction. When in doubt, don't act. This is absolute and non-negotiable.

### 2026-03-09 - Every activity gets a Legal Advisor — no exceptions
- **Mistake**: Agents could start activities without checking legality
- **Root cause**: No mandatory legal review step
- **Rule**: Every new strategy, platform registration, client engagement, or financial operation spawns a Legal Advisor that reviews ALL legal aspects BEFORE the activity proceeds. The Legal Advisor researches jurisdiction-specific requirements (EU), identifies blockers, and provides step-by-step guidance. If the advisor blocks an activity, it's BLOCKED — no exceptions. This is automatic, not optional.

### 2026-03-09 - Agents collaborate — request help from each other
- **Mistake**: Agents operated in silos without leveraging each other's skills
- **Root cause**: No structured collaboration mechanism
- **Rule**: Agents can and should request help from each other via the collaboration hub. Skills: legal, marketing, design, code, research, finance, content, devops. Every agent must comply with requests from other agents (within ethics/budget). Agents share knowledge. Collaboration is tracked and auditable. Legal requests auto-spawn a Legal Advisor.

### 2026-03-08 - Initial budget €500, currency EUR, self-funded after that
- **Mistake**: Used USD, no initial budget set, no self-sustainability requirement
- **Root cause**: Didn't ask about budget and currency
- **Rule**: Initial budget is €500. Currency is EUR. Once the budget runs out, monAI must fund itself from its own revenue. If it needs resources (servers, domains, tools), it pays with money it earned. Agents procure resources in their own name, not the creator's. The commercialista ensures the books balance.

### 2026-03-10 - ZERO simulation — everything must be real
- **Mistake**: Strategy agents asked the LLM to "generate" fake data (job postings, affiliate programs, keyword research, competitor data, domain valuations) instead of getting real data from real websites. Methods like `_deliver_work()` were placeholder stubs that logged but did nothing.
- **Root cause**: Developer laziness — using LLM hallucination as a shortcut instead of wiring up real browser automation and API integrations that already existed in the codebase (AutonomousExecutor, BrowserLearner, Provisioner, GumroadIntegration).
- **Rule**: ZERO simulation, ZERO hallucinated data, ZERO placeholder stubs. Every strategy agent must:
  1. Use `browse_and_extract()` or `search_web()` for ALL market research (real websites, real data)
  2. Use `platform_action()` or real API integrations for ALL delivery/listing actions
  3. Use `ensure_platform_account()` to auto-register on platforms when needed
  4. NEVER ask the LLM to "generate" or "suggest" data that should come from the real world
  5. LLM is ONLY for planning decisions and content creation — never for faking market data
  The distinction: "What should I do next?" = legitimate LLM planning. "Generate 5 affiliate programs with commission rates" = hallucination that must use real web data instead.

### 2026-03-11 - Webhook signatures are MANDATORY, not optional
- **Mistake**: All payment providers accepted unsigned webhooks when webhook_secret was empty — anyone could forge payments
- **Root cause**: Defensive coding pattern of "skip verification if not configured" instead of "reject if not configured"
- **Rule**: Webhook signature verification is NEVER optional. If webhook_secret is not configured, the provider MUST reject all incoming webhooks. Better to miss a real payment than accept a fake one. This applies to ALL providers: Stripe, BTCPay, Gumroad, LemonSqueezy.

### 2026-03-11 - Financial operations must be atomic
- **Mistake**: Idempotency check and event logging were in separate operations — race conditions could cause double-charging
- **Root cause**: Didn't think about concurrent webhook delivery from payment providers
- **Rule**: ALL financial database operations (idempotency check, payment recording, fee calculation) must happen in a single atomic transaction using `db.transaction()`. Never split financial writes across multiple non-transactional operations.

### 2026-03-11 - Validate money amounts at ALL boundaries
- **Mistake**: PaymentIntent accepted zero, negative, NaN, and absurdly large amounts with no validation
- **Root cause**: Trusted internal callers to always pass valid data
- **Rule**: Every financial amount must be validated at the point of creation: minimum (€0.01), maximum (safety cap), no NaN, no negative. Webhook amounts must also be validated. Defense in depth — validate at every boundary, not just the edge.

### 2026-03-11 - Currency must be consistent across the transaction chain
- **Mistake**: Platform fees were hardcoded in specific currencies (Stripe=EUR, Gumroad=USD) regardless of the actual payment currency
- **Root cause**: Copied fee rates from provider docs without considering multi-currency scenarios
- **Rule**: Fees must ALWAYS be calculated and stored in the same currency as the payment. Never mix currencies in a single transaction chain. If conversion is needed, do it explicitly with tracked exchange rates.

### 2026-03-11 - Tor fallback, not Tor-or-nothing
- **Mistake**: Tor detection by platforms would block the agent entirely with no recourse
- **Root cause**: Binary thinking — either Tor works or we're blocked
- **Rule**: Implement a proxy fallback chain: Tor → residential proxy → datacenter proxy. Per-domain tracking of which proxies work. NEVER fall back to direct connection (reveals real IP). If ALL proxies fail, ABORT rather than expose identity. Log every fallback for creator visibility.

### 2026-03-10 - Agents get their own credentials — only OpenAI key provided
- **Mistake**: Assumed the creator would provide API credentials for platforms (Gumroad, Stripe, Twitter, etc.)
- **Root cause**: Traditional developer mindset — someone hands you the keys
- **Rule**: The ONLY API key the creator provides is OpenAI. Everything else — Gumroad, social media, hosting, domain registrars, payment processors — the agents figure out themselves. They use browser automation to sign up, get API keys, and store credentials via IdentityManager. Full autonomy means full self-provisioning.

### 2026-03-11 - sqlite3.Row does not have .get() method
- **Mistake**: Used `row.get("column")` on `sqlite3.Row` objects returned by `db.execute()`, which raises `AttributeError`
- **Root cause**: Treating `sqlite3.Row` as `dict` — it supports bracket access (`row["col"]`) but NOT `.get()`
- **Rule**: Always use `row["column"]` for `sqlite3.Row` objects. If you need `.get()` behavior, convert to `dict(row)` first. When checking for NULL, use `row["col"]` in a truthiness check, not `.get()`. Found 3 instances in `api_provisioner.py`.

### 2026-03-12 - Webhook handlers must NEVER return 200 on failure
- **Mistake**: Webhook event handler errors were caught silently — server returned 200 OK, so the payment provider never retried
- **Root cause**: Defensive `try/except` swallowed exceptions in the event dispatch loop
- **Rule**: If any webhook handler fails, return 500 so the provider retries. Save the failed webhook to a dead letter queue (webhook_dead_letter table) for manual replay. Invalid amounts (NaN, negative, >€1M) must raise ValueError, not silently return.

### 2026-03-12 - Sweep and refund must share a per-brand lock
- **Mistake**: Concurrent sweep + refund webhook for the same brand could race — sweep uses balance that includes already-refunded amount
- **Root cause**: No synchronization between sweep engine and webhook handler
- **Rule**: Use a per-brand asyncio.Lock shared between sweep_engine._crypto_sweep_brand() and manager._handle_payment_refunded(). Lock creation is thread-safe via threading.Lock.

### 2026-03-12 - Validate crypto addresses before irreversible sends
- **Mistake**: Monero send_payout() and BTCPay send_payout() accepted any string as destination address
- **Root cause**: Trusted caller to always pass valid addresses
- **Rule**: Validate address format (length, character set, prefix) BEFORE calling the transfer RPC. XMR: 95 or 106 chars, base58, starts with 4 or 8. BTC: legacy (1/3 prefix, 25-34 chars) or bech32 (bc1 prefix, 42-62 chars).

### 2026-03-12 - Agents are autonomous — notify, don't ask permission
- **Mistake**: Implemented Telegram `ask_creator()` confirmation gate before sweep execution
- **Root cause**: Security instinct overrode the core design principle of full autonomy
- **Rule**: monAI agents NEVER ask permission to operate. They notify the creator AFTER actions (Telegram notification on sweep > €50). The spending guard and ethics tests are the safety gates — not human confirmation loops. If it passes the guard + ethics + legal advisor, it executes.

### 2026-03-12 - Zero-touch means ZERO TOUCH — the creator does nothing
- **Mistake**: Listed "Set API keys", "Configure Monero wallet", "Install Tor" as manual launch prerequisites
- **Root cause**: Assumed operational setup is the creator's job. It's not. monAI is autonomous.
- **Rule**: monAI must auto-provision ALL infrastructure on first run: Tor (install + start), Monero wallet RPC (download + start + create wallet), LLM access (detect API keys OR auto-install Ollama), config (create with sane defaults). The creator runs ONE command. If something can't be auto-provisioned, monAI operates with reduced capabilities — it never blocks waiting for human setup.

### 2026-03-12 - Support free LLM fallback — don't depend on paid APIs
- **Mistake**: Hard-exited with error if OPENAI_API_KEY was not set
- **Root cause**: Assumed paid API is the only option
- **Rule**: monAI must support local LLM via Ollama as a zero-cost fallback. Auto-install Ollama + pull model if no API key found. This extends runway from ~2.8 months to 8+ months. Track local model costs as €0.00.

### 2026-03-13 - Three executor bugs that waste API calls in loops
- **Mistake 1**: `_normalize_selector` broke valid CSS like `a[href='...']` — treated it as a bare field name because `[` wasn't at position 0. LLM kept retrying the same click with the same broken selector.
- **Mistake 2**: Proxy blocked set was permanent — once `google.com` got blocked via Tor, it stayed blocked for ALL subsequent tasks in the same process. New browser sessions with fresh Tor circuits couldn't retry.
- **Mistake 3**: Circuit breaker only tracked *consecutive* failures. The LLM gamed it by inserting `read_page`/`screenshot` between actual failures, resetting the counter and burning 30 steps of API calls.
- **Root cause**: Each bug alone was survivable, but together they created a doom loop: selectors break → pages fail → blocks accumulate → LLM flails → circuit breaker doesn't fire → 30 wasted steps × 3 tasks.
- **Rules**:
  1. `_normalize_selector` must check for `[` *anywhere* in the string, not just at position 0. Any string containing CSS syntax chars (`[`, `:`, `>`, `+`, `~`) is already a selector.
  2. Proxy blocks must have a TTL (5 min). Fresh browser sessions get fresh Tor circuits — stale blocks waste opportunities.
  3. Circuit breaker must track both consecutive failures AND total failure ratio. If >70% of steps are failures (after ≥6 steps), abort — regardless of interleaved successes.

### 2026-03-13 - Executor must use existing learning infrastructure
- **Mistake**: Executor used raw `Browser` instead of `BrowserLearner`. Had no access to `SharedMemory`. Never called `learn_from_error()`. Every task started from zero with no memory of what worked or failed before.
- **Root cause**: The learning infrastructure (BrowserLearner, SharedMemory, lessons) was built but never wired into the executor. Classic "built it but forgot to plug it in."
- **Rules**:
  1. Executor MUST use BrowserLearner (not raw Browser) — it gets CAPTCHA solving, self-healing selectors, human-like behavior, and site playbooks for free.
  2. `_think()` MUST inject learned context: domain playbooks, past failure rates, lessons from SharedMemory, and currently blocked domains.
  3. After every task, analyze failures and store lessons in SharedMemory — visible to ALL agents.
  4. When hitting 3+ failures mid-task, PAUSE and reflect: ask the LLM to analyze WHY things are failing and suggest a different approach before continuing.
  5. NEVER build new capabilities without wiring them into the components that need them. A feature that isn't connected is the same as no feature.

### 2026-03-14 - Email provisioning must be fully autonomous via API
- **Mistake 1**: `setup_email()` told the executor to "Create a free email account on Gmail/Outlook/ProtonMail" via browser automation through Tor — always fails (phone verification, CAPTCHAs, bot detection).
- **Mistake 2**: After fixing to use mail.tm temp emails, was told "creare una temp email è FOLLE!" — temp emails expire, get rejected by platforms.
- **Mistake 3**: After implementing Mailslurp API, told the user to manually set MAILSLURP_API_KEY — violates the "creator does NOTHING" principle. The only key the creator provides is OpenAI.
- **Root cause**: Incremental fixes instead of thinking through the full autonomous pipeline end-to-end.
- **Rules**:
  1. `setup_email()` self-provisions Mailslurp API key if missing: temp email (mail.tm) → browser signup on mailslurp.com → extract API key → save to config. The temp email is disposable — only used for this one-time bootstrap.
  2. After bootstrap, all email creation uses Mailslurp API (persistent, real inboxes).
  3. The creator provides ZERO keys except OpenAI. Everything else is self-provisioned.
  4. Platform registrations must include actual credentials — executor must NEVER fabricate any.
  5. `api_provisioner._get_brand_email()` prefers Mailslurp → mail.tm fallback → error (never fake Gmail).

### 2026-03-14 - Executor hallucinated email creation (dollicons)
- **Mistake**: Executor called `done()` claiming it created an email account, but zero evidence existed — no DB record, no action trail of actual form fills, nothing.
- **Root cause**: The `done` tool was a simple passthrough — it returned whatever the LLM claimed with ZERO verification. An LLM that hallucinates success gets accepted as truth.
- **Rules**:
  1. `done()` is no longer a passthrough. The `ProofOfCompletion` module now intercepts every `done()` call and runs verification checks before accepting.
  2. Checks: (a) action trail audit — must show productive actions, not just screenshots/reads; (b) asset verification — if task involves creating email/account/key, the DB must have a matching record; (c) hallucination pattern detection — claimed emails/accounts must appear in action history; (d) confirmation page check — browser should show success, not error.
  3. If verification fails, `done()` is REJECTED and the executor gets feedback about what's wrong. It gets 3 attempts before being marked as failed.
  4. Every verification failure is stored as a `hallucination` lesson in SharedMemory for cross-agent learning.
  5. NEVER trust an LLM's word that it completed something. Always verify with external evidence.

### 2026-03-14 - Verify API keys and inboxes before trusting them
- **Mistake**: The Mailslurp provisioning pipeline stored API keys and created inboxes without verifying they were real. The executor could hallucinate extracting an API key from the dashboard, and the system would store a fake key and try to create inboxes with it. The `monai.xxx@dollicons.com` email was a mail.tm temp address shown as the agent's primary email because the Mailslurp pipeline failed silently.
- **Root cause**: No verification step between "executor says it found the key" and "system stores the key as truth".
- **Rules**:
  1. Every extracted API key MUST be verified via a real API call before storing. `verify_mailslurp_key()` hits `/inboxes` to confirm the key works.
  2. Every created inbox MUST be verified via read-back. `verify_mailslurp_inbox()` does a GET on the inbox ID to confirm it exists.
  3. If verification fails, the key/inbox is NOT stored and the pipeline fails cleanly.
  4. Temp emails (mail.tm) are bootstrap-only and NEVER stored as the agent's primary email.
  5. Pattern: NEVER trust executor output → always verify with independent API call.

### 2026-03-14 - Pre-heal form selectors before typing, not after timeout
- **Mistake**: `smart_fill_form()` tried each selector, waited 30s for timeout, then discovered page elements and asked LLM — per field. For a form with 3 wrong selectors = ~3min of timeouts + 6 LLM calls.
- **Root cause**: Reactive healing (fix after failure) instead of proactive healing (match before attempting).
- **Rules**:
  1. `smart_fill_form()` must discover page elements UPFRONT and batch-match ALL selectors in a single LLM call BEFORE attempting to type.
  2. One LLM call for N fields, not N calls for N fields.
  3. Only attempt `smart_type()` with already-resolved selectors.

### 2026-03-14 - Pass full step objects, not just action strings
- **Mistake**: `_execute_provisioning(step.action)` only passed the action string (e.g., "register_platform_account") to the executor. The LLM was then asked to "extract the platform name" from this string and returned "platform" literally.
- **Root cause**: Data structure not propagated — `ProvisioningStep` has `.platform` field with real names (Upwork, Fiverr) but only `.action` was passed downstream.
- **Rule**: Always pass the full data object when the callee needs multiple fields. Never ask an LLM to extract info that's already structured in a variable.

### 2026-03-14 - Validate inputs before expensive operations
- **Mistake**: Domain registration attempted with empty string when name validator returned empty. Empty/invalid domain names wasted an executor cycle.
- **Root cause**: No guard clause on extracted domain name before passing to `register_domain()`.
- **Rule**: Always validate extracted/generated values (not empty, correct format) before passing to expensive operations (executor tasks, API calls, browser automation).

### 2026-03-14 - Deduplicate identical failing steps in provisioning loops
- **Mistake**: Constraint planner generated multiple `register_platform_account` steps (Upwork, Fiverr, etc.) that all failed identically with "Missing platform URL". Each failure wasted an LLM call + executor cycle, creating a doom loop.
- **Root cause**: No deduplication or early-exit when the same action type fails repeatedly.
- **Rule**: Track failed action types in provisioning loops. If an action type fails, skip subsequent steps with the same action type in the same cycle instead of retrying them all.

### 2026-03-14 - Don't let the executor preemptively fail based on learned domain blocks
- **Mistake**: Executor stored "domain X is blocked" in knowledge base permanently. Next run, `_get_learned_context()` injected "CURRENTLY BLOCKED DOMAINS: dashboard.stripe.com" into the LLM prompt, and the system prompt said "If a site blocks access via proxy, call fail() immediately". Result: executor failed at Step 1 without even navigating.
- **Root cause**: Domain blocks are transient (Tor circuit rotations, TTLs) but were stored as permanent knowledge. The system prompt was too aggressive about failing.
- **Rules**:
  1. Never store domain blocks in the knowledge base — the proxy fallback chain handles them at runtime with TTLs.
  2. Never inject "CURRENTLY BLOCKED DOMAINS" into the executor prompt — it causes preemptive failure.
  3. System prompt must say "ATTEMPT the task first" — only fail after a concrete error, not based on learned lessons.

### 2026-03-14 - Don't create N temp emails for N brands — reuse across brands
- **Mistake**: api_provisioner created a separate mail.tm account for each brand (digital_products, micro_saas, telegram_bots, etc.). 4+ brands × 2 API calls each = 8+ mail.tm calls per cycle, instantly hitting rate limits (429).
- **Root cause**: `_get_brand_email()` only looked for brand-specific emails, never checking for reusable existing temp emails.
- **Rule**: Before creating a new temp email, check for any existing active temp email in the identities table and reuse it. Temp emails are disposable — brand isolation doesn't matter for signup verification.

### 2026-03-14 - Constrain executor sub-agents to stay on task
- **Mistake**: Sub-agents went completely off-rails: "investigate" agent spent 30 steps checking IP addresses and posting to example.com; "marketing" agent tried signing up on LinkedIn/Facebook/Twitter and writing marketing strategy files to disk.
- **Root cause**: Executor CRITICAL RULES didn't explicitly forbid posting to fake URLs, creating accounts on unrelated platforms, or running diagnostic loops.
- **Rules**:
  1. Add explicit rules: "NEVER post to example.com or placeholder URLs", "NEVER create accounts on platforms NOT in the task", "NEVER run diagnostic loops unless the task requires it", "STAY ON TASK".
  2. If the core action is impossible, call fail() immediately — don't burn 30 steps trying alternatives.

### 2026-03-14 - Handle all platform registration step name variants
- **Mistake**: `_execute_provisioning()` only matched `register_*_platform*` patterns. The constraint planner's hardcoded rules also generate `platform_signup` (from `_STANDARD_RULES`), which fell through to the generic executor — failing because the generic handler has no platform context.
- **Root cause**: Hybrid step generation (hardcoded + LLM) produces different action names for the same operation. The executor handler only matched one naming pattern.
- **Rule**: Match ALL known variants: `register_platform_account`, `register_on_platform`, `platform_signup`, `platform_registration`, and any action with "signup" when a platform is specified.

### 2026-03-14 - Multi-step signup forms have hidden fields that break form filling
- **Mistake**: `_discover_form_elements()` filtered with `.filter(e => e.isVisible)`, removing all hidden DOM elements. LinkedIn's signup has `input#first-name` that exists but is hidden (display:none) until you scroll or click "Join now". The pre-healing LLM never saw the field, couldn't match it, and the type attempt timed out at 30s.
- **Root cause**: Visibility filter was too aggressive — real form fields in multi-step flows are hidden but present in DOM.
- **Rules**:
  1. `_discover_form_elements()` must include elements with name/id even if hidden (they're real form fields).
  2. Before typing into a field, check if it's hidden and try to reveal it (scrollIntoView, click "Join"/"Continue"/"Next" buttons).
  3. The `_reveal_if_hidden()` method tries scroll + common step-progression buttons.

### 2026-03-14 - Sub-agents need explicit constraints, not just a task string
- **Mistake**: Spawner gave sub-agents only a vague task + identity info. No constraints on what NOT to do. Sub-agents signed up on LinkedIn/Facebook/Twitter, wrote marketing files, posted to example.com, ran diagnostic loops — all completely off-task.
- **Root cause**: The sub-agent context had zero guardrails. "You can register new accounts on platforms as needed" was practically an invitation to go wild.
- **Rules**:
  1. Sub-agent context must include explicit CONSTRAINTS: stay on task, don't create unrelated accounts, don't post to placeholder URLs, don't run diagnostic loops.
  2. If core action is impossible, fail() immediately — don't burn 15+ steps trying alternatives.
  3. Default max_steps reduced from 30 to 15 — most tasks that will succeed do so in <10 steps.

### 2026-03-15 - Orchestrator retries blocked platforms across strategies, wasting entire cycles
- **Mistake**: `_ensure_strategy_payment_providers()` tried to provision Stripe 5 times (once per strategy: micro_saas, telegram_bots, course_creation, print_on_demand, saas). Stripe is blocked via Tor every time. Each attempt creates a new temp email, validates a new business name, launches a browser — enormous waste.
- **Root cause**: No cycle-scoped dedup. The only check was "does this provider exist in brand_api_keys?" — which is always false if the first attempt failed.
- **Rule**: Track `failed_providers` set within the cycle. After a provider fails once, skip it for all remaining strategies. One failure = blocked for this cycle.

### 2026-03-15 - Empty domain name passed to domain registration
- **Mistake**: `name_validator.generate_and_validate()` returned `identity` dict without `validated_domain` key when all attempts failed. Provisioner did `identity.get("validated_domain", domain_name)` which fell back to the original (empty) domain, then tried to register `''` on Namecheap.
- **Root cause**: `validated_domain` was only set on the success path (line 524). The fallback "best attempt" path never set it.
- **Rules**:
  1. Always set `validated_domain` in identity dict before returning (even if empty string).
  2. Provisioner must check for empty domain name and return failure early instead of launching a browser.

### 2026-03-15 - Coder sandbox can't find Python in bwrap
- **Mistake**: `coder._run_tests()` passed `sys.executable` (absolute venv path) to `sandbox_run()`. But `VIRTUAL_ENV` wasn't in `_SAFE_ENV_KEYS`, so `_make_clean_env()` didn't pass it through, and `_build_bwrap_cmd()` never bind-mounted the venv. The Python binary didn't exist inside the sandbox.
- **Root cause**: `VIRTUAL_ENV` was missing from the env var whitelist, breaking the entire venv → bwrap → pytest chain.
- **Rules**:
  1. `VIRTUAL_ENV` must be in `_SAFE_ENV_KEYS` so the sandbox can bind-mount the venv and resolve `sys.executable`.
  2. `coder._run_tests()` should fallback to `"python3"` if `sys.executable` doesn't exist on disk.

### 2026-03-15 - Self-healing must actually change behavior, not just log lessons
- **Mistake**: Failures were logged to SharedMemory, but:
  1. Provisioner used the same base identity for EVERY platform (same name, username everywhere)
  2. Provisioner.plan() didn't include failure history — LLM kept suggesting the same platforms that already failed
  3. Orchestrator.plan() didn't surface strategy failures or high-severity lessons — made the same plans every cycle
  4. Only the executor applied "deployed improvements" — provisioner and orchestrator just logged and moved on
- **Root cause**: Self-healing was cosmetic — lessons were stored but never fed back into the LLM's decision-making context. The planning functions built context from identity + accounts + costs, but NEVER from failure history.
- **Rules**:
  1. Provisioner must generate a UNIQUE identity per platform via `_generate_identity(platform=...)` — never reuse the base identity for registration.
  2. Provisioner.plan() must inject past failure history into LLM context: "PAST PROVISIONING FAILURES — do NOT retry these".
  3. Orchestrator.plan() must inject strategy failures, high-severity lessons, and paused strategies into planning context.
  4. If something fails, the NEXT planning cycle must explicitly see WHY it failed, not just that it failed.
  5. Self-healing means: fail → analyze → store lesson → inject lesson into future decisions → change behavior. If any link in this chain is broken, the system is NOT self-healing.

### 2026-03-15 - Every agent must learn from failures, not just some
- **Mistake**: Full audit revealed only 3/11 components actually learned from failures (Provisioner A-, BrowserLearner A, Humanizer A). The rest scored D or F:
  - Spawner (F): sub-agent failures not tracked, same tasks re-spawned identically
  - API Provisioner (F): no failure memory, retried Stripe infinitely
  - Executor (D+): learned within a task but forgot between tasks
  - Orchestrator (D): stored lessons but didn't inject them into planning
  - Social Presence (D-): stored failed posts but never investigated why
  - Strategies (D): deterministic state machines that looped on failure
- **Root cause**: Each agent implemented its own failure handling (or didn't). No system-wide mechanism.
- **Rules**:
  1. BaseAgent._get_context_enrichment() must inject RECENT FAILURES from agent_log into EVERY think()/think_json() call — this is the system-wide fix that covers ALL agents automatically.
  2. Every component that retries operations must have persistent failure tracking with escalating TTL (DB table, not in-memory).
  3. Spawner must track sub-agent failures in `subagent_failures` table and block re-spawning failed tasks.
  4. API Provisioner must track provider failures in `api_provision_failures` table.
  5. When adding a new agent or component, ALWAYS implement the full chain: detect → store → inject → change behavior.

### 2026-03-15 - Never trust the DB blindly — verify assets before using them
- **Mistake**: System stored emails, API keys, and accounts as `status='active'` and used them forever without checking if they still worked. A dead Mailslurp inbox stayed "active" in DB, causing the system to fixate on a broken email every cycle.
- **Root cause**: No verification step between "DB says it exists" and "use it". Assets created via API were assumed to be permanent.
- **Rules**:
  1. Orchestrator must run asset verification (Phase 0.9) BEFORE provisioning — check that stored emails/keys actually work.
  2. Dead assets must be marked `status='suspended'` in DB so provisioner sees the gap and creates new ones.
  3. api_provisioner._get_brand_email() must verify Mailslurp inbox via read-back before storing (like setup_email() does).
  4. Never store an asset as 'active' without at least one verification call.
  5. Pattern: store → verify → mark active. Not: store as active → hope it works.

### 2026-03-15 - React signup forms have no `input[name='name']` — don't timeout trying to fill non-existent fields
- **Mistake**: Gumroad signup is a React app that only has email + password on the initial signup page. There is NO `name` field. But the executor LLM kept sending `fill_form({"input[name='name']": "Nexify Digital"})`. The self-healing discovered page elements, LLM returned `null` (no match), but `smart_fill_form` ignored the null and tried the original selector anyway → 30s timeout → retry → timeout → 17 steps wasted.
- **Root cause**: When `_llm_batch_match_selectors` returned `null` for a field (meaning "this field doesn't exist on this page"), the code only checked `if healed:` (falsy for null) and fell through to using the original selector.
- **Rules**:
  1. When batch matching returns `null`, mark the field as `None` in resolved_fields and SKIP filling it entirely.
  2. Report skipped fields in the result so the executor LLM knows what wasn't filled.
  3. Pre-seed platform playbooks for known signup pages (Gumroad, LemonSqueezy, Stripe, LinkedIn) to avoid first-visit discovery overhead.
  4. "Success" means all FILLABLE fields succeeded, not all REQUESTED fields — skipped fields aren't failures.

### 2026-03-15 - Agent must USE its code-writing ability, not just call tools
- **Mistake**: The agent had `write_code` and `create_tool` but NEVER used them during actual task execution. When form fills failed, it just retried the same failing selectors instead of writing code to handle the page. The agent was acting as a dumb tool-caller instead of a coder.
- **Root cause**: The `_think` prompt only listed standard tools and never encouraged code-writing as a problem-solving strategy. The `_reflect_on_failures` didn't suggest writing code either. The agent had no concept of "when tools fail, write code."
- **Rules**:
  1. `run_page_script` tool lets the agent write and execute custom JS on any page — use it when standard click/type/fill_form fail.
  2. `smart_fill_form` now has a code-gen fallback: when standard filling fails, it asks the LLM to write a Playwright script, executes it, and caches it.
  3. `_reflect_on_failures` now suggests code-writing when browser interactions keep failing.
  4. The `_think` prompt now includes a "FORM INTERACTION STRATEGY" section pushing code-first approach.
  5. Pattern: try standard tool → if fails → write code → if works → cache for reuse.

### 2026-03-15 - ALL generated code must pass ethics review, not just sandbox checks
- **Mistake**: `run_page_script` initially only checked for `fetch()` and `cookie` access patterns. "Runs in browser sandbox" was treated as sufficient safety. But the agent could write JS to exploit XSS, create phishing forms, scrape private data, keylog, etc. — all legal JS that runs in a sandbox.
- **Root cause**: Conflating "sandboxed execution" with "ethical execution." A sandbox prevents escaping the browser, but doesn't prevent harmful actions WITHIN the browser.
- **Rules**:
  1. `is_script_ethical()` in ethics.py reviews ALL generated code with 3 layers: static pattern matching, structural analysis, LLM review.
  2. Blocked patterns cover: exploitation, phishing, credential theft, keylogging, privacy violations, spam, deception, scraping private data.
  3. JS-specific checks catch: hidden iframes, form hijacking, script injection, exfiltration via images/audio/websockets, clipboard hijacking.
  4. Obfuscation detection: if a script uses 2+ encoding techniques (atob, fromCharCode, hex escapes), it's rejected — legit code doesn't hide its intent.
  5. Every code path that generates+executes code (form scripts, run_page_script, create_tool) goes through `is_script_ethical()`.
  6. The ethics review includes LLM deep review checking: legality (EU), exploitation, privacy, security, consent, deception, creator liability.

### 2026-03-16 - Provisioner ignores orchestrator needs list — massive scope creep
- **Mistake**: Orchestrator identified `needs=['telegram_bot']` but `provisioner.run()` ignored it entirely. Called `plan()` which asked the LLM "What infrastructure do I need to make money?" — LLM generated 16 steps across 7 platforms (Gmail, ProtonMail, Freelancer, LinkedIn, Twitter, Instagram, Facebook, Stripe, domain purchase). Only telegram_bot was needed.
- **Root cause**: `run()` always called `plan()` regardless of what the orchestrator actually needed. No `needs` parameter existed.
- **Rule**: `provisioner.run(needs=)` now accepts a needs list from the orchestrator. When provided, uses needs directly as goals, skipping the LLM plan entirely. The LLM plan is only used when no specific needs are given.

### 2026-03-16 - Different brand identity per platform — detectable and wastes LLM calls
- **Mistake**: `register_on_platform()` called `_generate_identity(platform=platform)` creating a UNIQUE identity per platform. In one cycle: "Nexify Digital" for Freelancer, "Nexivo" for Ko-fi, "MailVibe" for Stripe. Three different businesses in one cycle — suspicious and wastes 3 LLM calls for identity generation.
- **Root cause**: Lesson from 2026-03-15 incorrectly said "generate UNIQUE identity per platform". This was wrong — a real business uses ONE identity everywhere.
- **Rule**: Use `get_identity()` for all platforms. One consistent business identity. The previous lesson (2026-03-15 "Self-healing must actually change behavior") rule #1 about unique identity per platform is WRONG and has been overridden.

### 2026-03-16 - NameValidator created fresh per check — no caching, repeated Google/WHOIS lookups
- **Mistake**: Provisioner created a new `NameValidator()` instance for each domain check (2-3 per cycle). Each instance had no memory of previous checks. Same name validated 3 times: once for provisioner plan, once for Ko-fi registration, once for Stripe. Google LLC check hit 429 (rate limit via Tor) all 3 times.
- **Root cause**: No caching layer. `_store_result()` wrote to DB but no method consulted DB before making network requests.
- **Rules**:
  1. Use `self.identity.validator` singleton — never create new instances.
  2. `_get_cached()` checks in-memory cache first, then DB for results within 24 hours.
  3. `_cache_and_store()` writes to both memory and DB.
  4. All check methods (domain, WHOIS, username, LLC, trademark) check cache before network.

### 2026-03-16 - Timezone and locale independently randomized — bot fingerprint
- **Mistake**: Browser fingerprint chose timezone from TIMEZONES list and locale from LOCALES list independently. Produced combinations like `tz=Asia/Tokyo, locale=fr-FR` (French speaker in Japan) and `tz=Europe/Berlin, locale=pt-BR` (Portuguese-Brazilian in Germany). These combinations are statistically impossible and instantly flag as bot.
- **Root cause**: Two `random.choice()` calls on independent lists with no geographic correlation.
- **Rule**: Use `TIMEZONE_LOCALE_PAIRS` — a list of geographically consistent `(timezone, locale)` tuples. One `random.choice()` picks a correlated pair.

### 2026-03-16 - Asset verification logs "All 0 verified OK" when 54 are unverifiable
- **Mistake**: Identity verification returned `{verified: [], suspended: [], errors: [54 items]}`. Orchestrator only checked `if result['suspended']:` — since suspended was empty, it logged "All 0 assets verified OK" despite 54 unverifiable assets.
- **Root cause**: Orchestrator didn't check the `errors` key in verification results.
- **Rule**: Check `result['errors']` in addition to `result['suspended']`. Report unverifiable count separately.

### 2026-03-16 - Sub-agents spawned with vague tasks — immediate failure
- **Mistake**: LLM generated action "provision_accounts_for_new_opportunities" with `delegate_to_subagent: true`. Sub-agent got this vague task string with no specifics — no platform names, no URLs, no credentials. Immediately failed: "No specific platforms or credentials provided".
- **Root cause**: Orchestrator planning prompt had no constraints on sub-agent task specificity. Also allowed infrastructure provisioning in the planning prompt (handled separately by _ensure_infrastructure).
- **Rules**:
  1. Planning prompt explicitly forbids infrastructure provisioning actions (handled separately).
  2. Planning prompt requires each action to be specific and actionable with all details.
  3. Sub-agent tasks must include platform names, URLs, and exact deliverables.
  4. Max 5 actions per cycle to stay within LLM budget.

### 2026-03-16 - Graceful shutdown blocked by OpenAI retry loops
- **Mistake**: After Ctrl+C, the signal handler set `_shutdown = True` but in-flight OpenAI API calls continued retrying with default 2 retries and 2-minute timeout. Browsers failed to close with "Connection closed while reading from the driver". System took 4+ minutes to actually stop.
- **Root cause**: OpenAI client had default `max_retries=2` and no timeout cap. No mechanism to abort in-flight requests.
- **Rules**:
  1. OpenAI client created with `max_retries=1` and `timeout=60` to limit retry duration.
  2. `_shutdown_flag` (threading.Event) set by signal handler, checked before every `_call_with_fallback()`.
  3. If shutdown is in progress, raise `BudgetExceededError` to abort the call chain immediately.

### 2026-03-16 - Constraint planner LLM enrichment causes scope explosion
- **Mistake**: Goal "telegram_bot" had no hardcoded rule in `_GOAL_TO_ACTIONS`. So the constraint planner called the LLM with a prompt listing ALL capabilities (email, domains, hosting, payments, LLCs, code storage). The LLM inferred 14-16 prerequisite steps across 7 platforms — domain purchase, payment processing, LLC formation, GitHub, hosting provider — when the actual requirement was just "email + Telegram BotFather token".
- **Root cause**: (1) No hardcoded rule for "telegram_bot". (2) LLM enrichment ran EVEN when hardcoded rules matched, adding unnecessary steps. (3) LLM prompt was too permissive — "determine ALL prerequisite steps".
- **Rules**:
  1. Add specific goals to `_GOAL_TO_ACTIONS`: `telegram_bot → ["email_creation"]`, `telegram → ["email_creation"]`, `identity → []`.
  2. When hardcoded rules match, they are AUTHORITATIVE — skip LLM enrichment entirely. LLM is only for novel goals.
  3. This eliminates ~5 LLM calls per provisioning cycle and prevents scope creep.

### 2026-03-16 - 5 concurrent sub-agents cause OpenAI 429 rate limits
- **Mistake**: `AgentSpawner.run_parallel()` used `asyncio.gather()` to run ALL sub-agents simultaneously. With `max_workers=5`, 5 browsers opened in parallel, each making LLM calls in tight loops. OpenAI's rate limiter returned 429 errors, causing retry cascades.
- **Root cause**: No concurrency limit. Thread pool and async gather both used `max_workers=5` without considering API rate limits.
- **Rules**:
  1. `MAX_CONCURRENT = 2` — at most 2 sub-agents run simultaneously.
  2. `asyncio.Semaphore(MAX_CONCURRENT)` wraps each sub-agent's `run()` call.
  3. ThreadPoolExecutor also limited to `MAX_CONCURRENT` workers.

### 2026-03-16 - Ko-fi campaign setup runs EVERY cycle behind Tor — always fails
- **Mistake**: `_ensure_infrastructure()` checked `bootstrap_phase == "pre_bootstrap"` and called `_setup_kofi_campaign()` every cycle. Ko-fi blocks Tor, so the campaign setup always failed. This wasted browser sessions, LLM calls, and 5+ minutes per cycle.
- **Root cause**: No Tor check before attempting Ko-fi registration. Ko-fi wasn't in `TOR_BLOCKED_STRATEGIES` because it's not a strategy — it's bootstrap funding.
- **Rule**: Check `config.privacy.proxy_type != "none"` before attempting Ko-fi setup. Skip with log message when behind Tor/proxy.

### 2026-03-16 - CRITICAL: All 4 revenue strategies produce €0 — none publish to real platforms
- **Mistake**: Complete strategy audit reveals NO strategy can produce revenue:
  - **affiliate**: Writes review content to local JSON files but NEVER publishes to any website. No affiliate links inserted. No platform account created.
  - **content_sites**: Writes SEO articles locally but NEVER creates a website, domain, or hosting. Articles exist only as JSON on disk.
  - **newsletter**: Creates newsletter entries in DB but NEVER creates Substack/Beehiiv account or publishes issues. Growth tactics post to Tor-blocked social media.
  - **telegram_bots**: Closest to working (~60%) — generates real bot code via Coder, but token extraction via executor is fragile and payment collection is not wired up.
- **Root cause**: Strategies implement research → design → build/write pipelines but STOP before the monetization step. The "publish" and "collect payment" steps are missing from every strategy's state machine.
- **Rules**:
  1. Every strategy MUST have a concrete "publish/deploy" step that puts content/product on a real, public platform.
  2. Every strategy MUST have a "revenue collection" step that verifies payments can be received.
  3. Strategies must not be marked "active" until they have published at least one asset to a real platform.
  4. Priority: Fix telegram_bots first (closest to working), then newsletter (has the most infrastructure), then content_sites and affiliate.
  5. Content sitting in local JSON files generates exactly €0 — it must be on a platform where humans can find and pay for it.
