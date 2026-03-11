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
- **Rule**: The master agent contacts the creator via Telegram (username: Cristal89) when it needs human input. The bot is self-provisioned — the agent creates it, gets the API key, everything. The agent ALWAYS identifies itself with a verification code (stored locally in ~/.monai/verify.txt) proving it runs on the creator's machine. The creator does NOTHING. Every message includes the verification header.

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
