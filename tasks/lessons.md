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

### 2026-03-08 - Initial budget €500, currency EUR, self-funded after that
- **Mistake**: Used USD, no initial budget set, no self-sustainability requirement
- **Root cause**: Didn't ask about budget and currency
- **Rule**: Initial budget is €500. Currency is EUR. Once the budget runs out, monAI must fund itself from its own revenue. If it needs resources (servers, domains, tools), it pays with money it earned. Agents procure resources in their own name, not the creator's. The commercialista ensures the books balance.
