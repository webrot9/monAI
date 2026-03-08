# Lessons Learned

### 2026-03-08 - Use OpenAI APIs, not Claude/Anthropic
- **Mistake**: Assumed Claude SDK for the AI backbone
- **Root cause**: Defaulted to Anthropic ecosystem without asking
- **Rule**: User has OpenAI APIs set up. Always use OpenAI SDK (`openai` package) for all LLM calls in monAI

### 2026-03-08 - Real business strategies, not trading
- **Mistake**: Included algorithmic trading and arbitrage as strategies
- **Root cause**: Went too broad without understanding user's vision
- **Rule**: monAI is a fully autonomous business operator. It finds clients, communicates with them, delivers work, invoices, and handles everything end-to-end. No passive trading/speculation. Real services for real clients.
