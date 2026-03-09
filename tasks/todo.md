# monAI - Architecture Evolution Plan

## Vision
Evolve monAI from a single-orchestrator system to a multi-team autonomous organization with self-healing engineering, adaptive browser automation, anti-AI-detection capabilities, and diversified revenue channels.

## New Architecture

```
Orchestrator (CEO)
├── Engineering Team (self-healing, continuous improvement)
│   ├── TechLead (reviews errors, prioritizes bugs, assigns work)
│   └── Engineers x2-3 (fix bugs, improve modules, write tests)
│
├── Revenue Squad (one agent per channel, Darwinian selection)
│   ├── FreelanceAgent (Upwork/Fiverr — existing, needs flesh)
│   ├── DigitalProductsAgent (Gumroad/Etsy — existing, needs flesh)
│   ├── ContentSiteAgent (SEO blogs, affiliate content)
│   ├── MicroSaaSAgent (small tools, API wrappers)
│   ├── TelegramBotAgent (bots-as-a-service)
│   └── AffiliateAgent (affiliate marketing content)
│
├── Infrastructure Team
│   ├── BrowserAgent (adaptive automation, learns from failures)
│   └── PhoneProvisionerAgent (virtual numbers for signups)
│
└── Quality Team
    └── HumanizeAgent (anti-AI-detection, style matching, quality)
```

## Implementation Plan

### Phase A: Engineering Team (self-healing system)
- [x] Design `EngineeringTeam` architecture
- [ ] Create `TechLead` agent (`src/monai/agents/eng_team/tech_lead.py`)
  - Monitors error logs, agent failures, test results
  - Prioritizes issues by severity and business impact
  - Assigns work to engineer agents
  - Reviews fixes before deployment
  - Reports to orchestrator
- [ ] Create `Engineer` agent (`src/monai/agents/eng_team/engineer.py`)
  - Receives bug assignments from TechLead
  - Uses Coder agent to write fixes + tests
  - Submits fixes for review
  - Can be spawned as multiple instances
- [ ] Create `EngineeringTeam` coordinator (`src/monai/agents/eng_team/__init__.py`)
  - Manages the team lifecycle
  - Exposes `run()` that the orchestrator calls
  - Tracks bug backlog, fix rate, regression rate
- [ ] Wire into orchestrator cycle (new Phase 6.7: Engineering)
- [ ] Add DB schema for bug tracking (bugs table)

### Phase B: Adaptive Browser Automation (learning loop)
- [ ] Create `BrowserLearner` (`src/monai/agents/browser_learner.py`)
  - Wraps existing `Browser` class
  - Logs every action: URL, selector, action type, result, error
  - Categorizes failures: CAPTCHA, bot_detection, dom_change, timeout, auth_required
  - Tracks success rates per site, per action type
  - Generates countermeasures per failure type:
    - CAPTCHA → route to solving service (2captcha API)
    - Bot detection → fingerprint rotation, realistic delays, mouse movement
    - DOM changes → self-healing selectors (find by text/role/aria, not CSS)
    - Timeout → adaptive wait times
  - Maintains a "site playbook" — learned interaction patterns per domain
  - Exposes metrics: success_rate, avg_time, failure_breakdown
- [ ] Add `browser_actions` DB table (action log with outcomes)
- [ ] Add `site_playbooks` DB table (learned patterns per domain)
- [ ] Add CAPTCHA solver integration (2captcha/anti-captcha API)
- [ ] Add self-healing selector logic (fallback strategies)

### Phase C: Anti-AI Detection / Humanizer
- [ ] Create `Humanizer` (`src/monai/agents/humanizer.py`)
  - Post-processes all outbound content
  - Analyzes and matches target voice/style
  - Varies sentence structure (breaks AI-typical patterns)
  - Injects specificity, opinions, natural imperfections
  - Maintains style profiles per client/platform
  - Self-critique loop: draft → analyze → rewrite
  - Tracks detection scores over time (self-test with detectors)
- [ ] Add `style_profiles` DB table
- [ ] Add `content_quality` DB table (tracks detection scores, rewrites)
- [ ] Integrate into all content-producing agents

### Phase D: Multi-Channel Revenue Diversification
- [ ] Create `ContentSiteAgent` (`src/monai/strategies/content_sites.py`)
  - SEO blog creation and management
  - Affiliate content with tracked links
  - Targets low-competition long-tail keywords
- [ ] Create `MicroSaaSAgent` (`src/monai/strategies/micro_saas.py`)
  - Identifies micro-SaaS opportunities
  - Uses Coder to build small tools/APIs
  - Deploys on free tiers (Vercel, Railway, etc.)
- [ ] Create `TelegramBotAgent` (`src/monai/strategies/telegram_bots.py`)
  - Builds and deploys Telegram bots as paid services
  - Targets specific niches (productivity, crypto, etc.)
- [ ] Create `AffiliateAgent` (`src/monai/strategies/affiliate.py`)
  - Review/comparison content for affiliate programs
  - Targets high-commission niches
- [ ] Register all new strategy agents with orchestrator
- [ ] Update orchestrator to run Darwinian channel selection

### Phase E: Virtual Phone Provisioning
- [ ] Create `PhoneProvisioner` (`src/monai/agents/phone_provisioner.py`)
  - Integrates with SMS API services (TextVerified, SMSPool)
  - Procures virtual numbers for platform signups
  - Manages number lifecycle (acquire, use, release)
  - Routes verification codes to requesting agents
- [ ] Add `virtual_phones` DB table
- [ ] Wire into provisioner flow

### Phase F: Orchestrator Evolution
- [ ] Add engineering team phase to cycle
- [ ] Add Darwinian revenue optimization (shift resources to winners)
- [ ] Add browser learning metrics to health checks
- [ ] Add humanizer quality gate for all outbound content
- [ ] Update cycle to support new agent teams

## Priority Order
1. **Phase A** (Engineering Team) — enables self-healing, unblocks everything else
2. **Phase C** (Humanizer) — quality moat, affects all revenue
3. **Phase B** (Browser Learner) — unblocks platform signups
4. **Phase D** (Revenue Channels) — diversified income
5. **Phase E** (Phone Provisioner) — unblocks platform registration
6. **Phase F** (Orchestrator Evolution) — ties it all together

## Review
<!-- Post-completion review -->
