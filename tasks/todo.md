# monAI - Opzione A: Salvare il Salvabile

## Strategy: Digital Products on Gumroad (ONE strategy, end-to-end)

## Phase 1: Split Orchestrator (2500 lines → 5-6 focused classes)
- [ ] Extract `InfrastructureManager` (provisioning, identity, LLC, phone, API)
- [ ] Extract `FinanceController` (ledger, finance, commercialista, exchange rates, tax, reconciliation, reporting)
- [ ] Extract `StrategyRunner` (strategy registration, lifecycle, execution, auto-pause/scale)
- [ ] Extract `OperationsManager` (audit, alerting, backup, spending guard, risk)
- [ ] Extract `TeamCoordinator` (research, marketing, engineering, workflow engine)
- [ ] Slim `Orchestrator` to thin coordinator (~200 lines)

## Phase 2: Remove Dead Weight
- [ ] Delete 12 unused strategies (keep only `digital_products`)
- [ ] Delete unused team agents (marketing_team, research_team, eng_team) — not needed for MVP
- [ ] Delete unused agents (social_presence, kofi_manager, domain_flipping, etc.)
- [ ] Remove dead imports and wiring from orchestrator
- [ ] Delete corresponding test files

## Phase 3: Fix Financial Vulnerabilities
- [ ] Audit all float usage in money paths → convert to Decimal
- [ ] Fix refund-after-sweep race condition (lock + deficit tracking)
- [ ] Add DB transaction wrapping for payment state changes
- [ ] Validate concurrent sweep/refund locks work correctly

## Phase 4: Test Overhaul
- [ ] Identify tests with real assertions vs smoke tests
- [ ] Delete smoke tests and no-value tests (~70%)
- [ ] Rewrite remaining tests with real behavior assertions
- [ ] Add E2E test: Gumroad product creation → listing → sale → payment → sweep

## Phase 5: Make Digital Products Work E2E
- [ ] Verify Gumroad integration is complete and real
- [ ] Wire digital_products into slim orchestrator
- [ ] Test full pipeline: research → create → review → list → sell → collect
- [ ] Prove it works with real Gumroad API calls

---

## Previous Completed Work (archived)
<details>
<summary>152 items completed before Opzione A</summary>

- [x] Payment pipeline, LLC provisioner, bootstrap funding, etc.
- [x] All previous sprints and features
- [x] 1936 tests (pre-refactor baseline)

</details>
