# Lessons Learned

### 2026-03-08 - Use OpenAI APIs, not Claude/Anthropic
- **Mistake**: Assumed Claude SDK for the AI backbone
- **Root cause**: Defaulted to Anthropic ecosystem without asking
- **Rule**: User has OpenAI APIs set up. Always use OpenAI SDK (`openai` package) for all LLM calls in monAI

### 2026-03-08 - Real business strategies first, trading is supplementary
- **Mistake**: Focused too heavily on trading/arbitrage as primary strategies
- **Root cause**: Went too narrow without understanding user's full vision
- **Rule**: monAI is primarily a fully autonomous business operator (find clients, deliver, invoice). Trading and arbitrage CAN be included as additional diversified strategies, but the core is real services for real clients. Diversification is key — not just one category.
