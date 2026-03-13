# monAI — Autonomous Money-Making AI

Fully autonomous AI agent system that discovers opportunities, provisions its own infrastructure, runs 14 revenue strategies, manages clients, delivers work, invoices, and scales — zero human intervention after setup.

## Architecture

```
Orchestrator (brain)
  |
  |-- 14 Strategy Agents (freelance_writing, micro_saas, newsletter, etc.)
  |-- SocialPresence (per-brand social media)
  |-- WebPresence (per-brand websites & landing pages)
  |-- EmailMarketing (per-brand subscriber lists & campaigns)
  |-- BrandPayments (anonymous payment collection & profit sweeping)
  |-- Pipeline (conversion CRM: impression -> customer)
  |-- MarketingTeam, ResearchTeam, FinanceExpert
  |-- EngineeringTeam (self-healing bug fixes)
  |-- Provisioner (auto-registers accounts, domains, APIs)
  |-- IdentityManager (digital identities per brand)
  |-- EthicsTester (agent quarantine on violations)
  |-- LegalAdvisor (reviews every activity before execution)
  |-- NetworkAnonymizer (Tor/SOCKS5 for all external traffic)
```

## Requirements

- Python 3.11+
- OpenAI API key (GPT-4o)
- Tor (for anonymous networking — optional but strongly recommended)

## Quick Start

### 1. Install

```bash
cd monAI

# Upgrade pip and setuptools first (required for editable installs)
pip install --upgrade pip setuptools wheel

# Install monAI with dev dependencies
pip install -e ".[dev]"
```

> **Troubleshooting**: If `pip install -e .` fails with a `build_editable` error,
> your setuptools is too old. Run `pip install --upgrade setuptools>=68.0` first.
> If that's blocked by a system-managed Python, use a virtualenv:
> ```bash
> python3 -m venv .venv && source .venv/bin/activate
> pip install -e ".[dev]"
> ```

### 2. Set your OpenAI API key

```bash
export OPENAI_API_KEY=sk-...
```

### 3. Initialize

```bash
monai init
```

This creates:
- `~/.monai/config.json` — all configuration
- `~/.monai/monai.db` — SQLite database
- Seeds the 14 default revenue strategies

### 4. (Optional) Configure Tor for anonymity

Install and start Tor:

```bash
# Ubuntu/Debian
sudo apt install tor
sudo systemctl start tor

# macOS
brew install tor
tor &
```

Tor runs on `127.0.0.1:9050` (SOCKS5) and `127.0.0.1:9051` (control) by default.
monAI routes ALL external traffic through it.

To disable anonymity checks (not recommended):

```bash
# Edit ~/.monai/config.json and set:
# "privacy": { "proxy_type": "none", "verify_anonymity": false }
```

### 5. Run

```bash
# Continuous autonomous daemon (default: 5-min cycles)
monai daemon

# Single cycle
monai run

# Custom interval (e.g. 10 minutes)
monai daemon --interval 600
```

### 6. Monitor

```bash
monai status
```

Shows: budget, net profit, strategy P&L, API costs by agent/model, active accounts, daily summary.

### 7. Discover new opportunities

```bash
monai discover
```

## Commands

| Command | Description |
|---------|-------------|
| `monai daemon` | Start monAI. Runs orchestration cycles in a loop (every 5 min by default). This is the main command. |
| `monai run` | Run a single orchestration cycle and exit. Useful for testing/debugging. |
| `monai status` | Display financial reports and strategy health (read-only, no cycle). |
| `monai init` | Initialize config and database. Run once before first use. |
| `monai discover` | Manual shortcut to preview opportunity discovery. Not required — discovery already runs automatically inside each daemon cycle. |

In practice, you only need: `monai init` (once), then `monai daemon` (forever).

## Configuration

All config lives in `~/.monai/config.json`. Created automatically on `monai init`.

### Key settings

```json
{
  "llm": {
    "model": "gpt-4o",
    "model_mini": "gpt-4o-mini",
    "api_key": "sk-..."
  },
  "risk": {
    "max_strategy_allocation_pct": 30.0,
    "stop_loss_pct": 15.0,
    "max_monthly_spend_new_strategy": 10.0
  },
  "privacy": {
    "proxy_type": "tor",
    "tor_socks_port": 9050,
    "tor_control_port": 9051,
    "verify_anonymity": true,
    "rotate_user_agent": true,
    "strip_metadata": true
  },
  "telegram": {
    "enabled": true,
    "creator_username": "YOUR_TELEGRAM_USERNAME"
  },
  "initial_capital": 500.0,
  "currency": "EUR"
}
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes | OpenAI API key for GPT-4o |

## The 14 Revenue Strategies

| # | Strategy | Category | Budget | What it does |
|---|----------|----------|--------|-------------|
| 1 | freelance_writing | services | 10 | Upwork/Fiverr writing gigs |
| 2 | digital_products | products | 10 | Ebooks, templates, prompt packs |
| 3 | cold_outreach | services | 10 | B2B cold email/LinkedIn outreach |
| 4 | content_sites | content | 10 | SEO blogs, affiliate content |
| 5 | micro_saas | products | 10 | Small tools, API wrappers |
| 6 | telegram_bots | products | 5 | Telegram bots as paid services |
| 7 | affiliate | content | 5 | Review/comparison affiliate content |
| 8 | newsletter | content | 5 | Email newsletters + sponsors |
| 9 | lead_gen | services | 10 | B2B lead generation |
| 10 | social_media | services | 10 | SMB social media management |
| 11 | course_creation | products | 5 | Udemy/Skillshare/Gumroad courses |
| 12 | domain_flipping | trading | 10 | Domain acquisition and resale |
| 13 | print_on_demand | products | 5 | Redbubble/TeeSpring designs |
| 14 | saas | products | 15 | Full SaaS products |

Each strategy operates autonomously with its own brand identity, social presence, website, email list, and payment accounts.

## Per-Brand Infrastructure

Every strategy automatically gets:

- **Social media accounts** — Twitter, LinkedIn, Reddit, Indie Hackers (via SocialPresence agent)
- **Website** — Domain registration, LLM-generated landing pages, SEO, analytics (via WebPresence agent)
- **Email marketing** — Subscriber lists, campaigns, drip sequences, open/click tracking (via EmailMarketing)
- **Payment collection** — Monero/Bitcoin/Stripe/PayPal/Gumroad + anonymous profit sweeping to creator (via BrandPayments)
- **Conversion pipeline** — Full CRM funnel: impression > click > lead > prospect > customer > repeat (via Pipeline)

## Payment & Anonymity

Payments are collected per-brand and swept to the creator through privacy-preserving channels:

| Method | Privacy Level | Description |
|--------|--------------|-------------|
| Monero (XMR) | Maximum | Untraceable by protocol design |
| Bitcoin + CoinJoin | High | Mixed transactions before transfer |
| Bitcoin direct | Medium | Pseudonymous, on-chain traceable |

**The creator's identity is never exposed.** Each brand operates under its own digital identity. All network traffic goes through Tor. Metadata is stripped from all files. This is identity separation, not law evasion — everything is legal.

## Orchestration Cycle

Each cycle (default 5 minutes) runs these phases:

1. **Anonymity check** — verify proxy is working, real IP hidden
2. **Telegram** — check for creator messages
3. **Resource check** — CPU, memory, disk within limits
4. **Budget check** — can we afford to operate?
5. **Message processing** — inter-agent collaboration
6. **Infrastructure** — provision missing accounts/domains/keys
7. **Health check** — portfolio diversification, risk assessment
8. **Strategy review** — pause losing strategies, scale winners
9. **Planning** — LLM generates prioritized action list
10. **Execution** — run actions directly or delegate to sub-agents
11. **Ethics testing** — quarantine agents that fail ethics checks
12. **Strategy runs** — each active strategy executes its cycle
13. **Support teams** — finance, research, marketing, social, web
14. **Engineering** — self-healing bug fixes
15. **Reporting** — commercialista financial report
16. **Reflection** — extract lessons, share insights

## Telegram Bot

monAI auto-provisions a Telegram bot for creator communication:

1. Agent creates bot via BotFather (autonomous)
2. Creator sends `/start` to the bot
3. Agent verifies creator identity via cryptographic token in `~/.monai/verify.txt`
4. Creator can request status updates, reports, and give instructions

Set your Telegram username in `~/.monai/config.json` under `telegram.creator_username`. Then send `/start` to the bot after it self-provisions.

## Development

### Run tests

```bash
pip install -e ".[dev]"
pytest tests/ -v

# If pip install -e fails, use PYTHONPATH instead:
PYTHONPATH=src pytest tests/ -v
```

### Project structure

```
src/monai/
  agents/          # Orchestrator, base agent, identity, ethics, social, web
  business/        # CRM, finance, payments, pipeline, email marketing, invoicing
  strategies/      # The 14 revenue strategy agents
  social/          # Platform API clients (Twitter, LinkedIn, Reddit)
  workflows/       # Workflow engine, pipelines, task router
  utils/           # LLM wrapper, privacy/anonymizer, Telegram, resources
  db/              # SQLite database layer
  main.py          # CLI entry point
```

## Data & Logs

| Path | Description |
|------|-------------|
| `~/.monai/config.json` | Configuration |
| `~/.monai/monai.db` | SQLite database (all state) |
| `~/.monai/monai.log` | Application logs |
| `~/.monai/verify.txt` | Telegram verification token |

## Stopping

```bash
# Graceful shutdown (finishes current cycle)
Ctrl+C

# Or send SIGTERM
kill -TERM <pid>
```

## Ethics & Safety

- Every activity is reviewed by a Legal Advisor before execution
- Agents that fail ethics tests are quarantined automatically
- Creator's identity is never exposed (network, payment, metadata)
- All API costs are tracked and budgeted
- Stop-loss halts losing strategies automatically
- Everything is logged and auditable
