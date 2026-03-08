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
