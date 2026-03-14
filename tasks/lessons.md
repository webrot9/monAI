# Lessons Learned

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
