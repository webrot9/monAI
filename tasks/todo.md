# monAI - Opzione A: Salvare il Salvabile

## Strategy: Digital Products on Gumroad (ONE strategy, end-to-end)

## Phase 1: Split Orchestrator (2500 lines → 5-6 focused classes)
- [x] Extract `InfrastructureManager` (provisioning, identity, LLC, phone, API)
- [x] Extract `FinanceController` (ledger, finance, commercialista, exchange rates, tax, reconciliation, reporting)
- [x] Extract `StrategyRunner` (strategy registration, lifecycle, execution, auto-pause/scale)
- [x] Extract `OperationsManager` (audit, alerting, backup, spending guard, risk)
- [x] ~~Extract `TeamCoordinator`~~ — removed, team agents deleted as dead code
- [x] Slim `Orchestrator` to thin coordinator (~720 lines)

## Phase 2: Remove Dead Weight
- [x] Delete 12 unused strategies (keep only `digital_products`)
- [x] Delete unused team agents (marketing_team, research_team, eng_team) — ~3000 lines removed
- [x] Delete unused agents (social_presence, web_presence) — moved BRAND_PLATFORMS to strategy_lifecycle
- [x] Remove dead imports and wiring from orchestrator
- [x] Delete corresponding test files (6 test files removed)
- [x] Clean up task router (removed dead capabilities + task types)

## Phase 3: Fix Financial Vulnerabilities
- [x] Audit all float usage in money paths → convert to Decimal
  - Core types (PaymentIntent, PaymentResult, WebhookEvent, ProviderBalance, SweepRequest, SweepResult)
  - All 6 payment providers (Stripe, BTCPay, Gumroad, LemonSqueezy, Monero, Ko-fi)
  - Sweep engine, brand_payments, integrations/gumroad
- [x] Fix refund-after-sweep race condition (lock + deficit tracking) — already done
- [x] Add DB transaction wrapping for payment state changes
- [x] Validate concurrent sweep/refund locks work correctly — already done

## Phase 4: Test Overhaul
- [x] Identify tests with real assertions vs smoke tests — 97% already real, ~28 low-value (constants/structure checks)
- [x] ~~Delete smoke tests and no-value tests (~70%)~~ — only 3% low-value, not worth a cleanup pass
- [x] ~~Rewrite remaining tests with real behavior assertions~~ — tests already excellent
- [x] Add E2E test: Gumroad product creation → listing → sale → payment → sweep

## Phase 5: Make Digital Products Work E2E
- [x] Verify Gumroad integration is complete and real
- [x] Wire digital_products into slim orchestrator
- [x] Test full pipeline: research → create → review → list → sell → collect
- [ ] Prove it works with real Gumroad API calls

---

## Previous Completed Work (archived)
<details>
<summary>152 items completed before Opzione A</summary>

- [x] Payment pipeline, LLC provisioner, bootstrap funding, etc.
- [x] All previous sprints and features
- [x] 1936 tests (pre-refactor baseline)

</details>
