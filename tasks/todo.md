# monAI - Autonomous Money-Making Agent

## Goal
Build a modular AI agent system that autonomously generates money through diversified, legal strategies with proper risk management.

## Architecture Plan

### Core Components
1. **Orchestrator** — Master agent that evaluates, selects, and allocates resources across strategies
2. **Strategy Agents** — Pluggable sub-agents, each specialized in one money-making approach
3. **Risk Manager** — Enforces diversification rules, stop-losses, and risk/reward thresholds
4. **Portfolio Tracker** — Tracks P&L, capital allocation, and performance per strategy
5. **Config & Secrets** — API keys, budgets, constraints

### Strategy Categories (by risk tier)
| Tier | Strategy | Risk | Time to Revenue | Capital Needed |
|------|----------|------|-----------------|----------------|
| Low  | Content/SEO/Affiliate | Low | Medium | Low |
| Low  | Digital Products (ebooks, templates, prompts) | Low | Medium | Low |
| Low  | Freelance Automation (writing, data, code) | Low | Fast | None |
| Med  | SaaS Micro-tools | Medium | Slow | Low-Med |
| Med  | Data Services / Web Scraping | Medium | Medium | Low |
| Med  | Social Media Growth & Monetization | Medium | Medium | Low |
| High | Algorithmic Trading (crypto/stocks) | High | Fast | Medium-High |
| High | Arbitrage (cross-platform price gaps) | Medium-High | Fast | Medium |

### Risk Rules
- No single strategy gets >30% of total capital
- Every strategy must have positive expected value (EV) before activation
- Stop-loss: halt any strategy that loses >15% of allocated capital
- Minimum 3 active strategies at all times for diversification
- New strategies start with small allocation, scale up on proven performance
- Track all expenses vs revenue — no unmonitored spending

## Implementation Phases

### Phase 1: Foundation ✅ → [ ]
- [x] Set up workflow orchestration rules
- [ ] Initialize Python project structure (pyproject.toml, src layout)
- [ ] Build core agent framework (base agent, orchestrator, message passing)
- [ ] Build configuration system (settings, API keys, budgets)
- [ ] Build portfolio tracker (P&L, allocations, history)
- [ ] Build risk manager (diversification rules, stop-loss, EV checks)

### Phase 2: First Strategies → [ ]
- [ ] Content generation agent (blog posts, articles, SEO)
- [ ] Digital products agent (ebooks, prompt packs, templates)
- [ ] Freelance agent (platform bidding, task completion)

### Phase 3: Advanced Strategies → [ ]
- [ ] Trading agent (algorithmic, paper-trade first)
- [ ] Arbitrage agent (cross-platform price monitoring)
- [ ] SaaS micro-tool agent (identify needs, build MVPs)

### Phase 4: Intelligence & Optimization → [ ]
- [ ] Performance analytics and strategy scoring
- [ ] Auto-rebalancing based on returns
- [ ] Strategy discovery (find new opportunities)
- [ ] Self-improvement loop (learn from failures)

## Review
<!-- Post-completion review -->
