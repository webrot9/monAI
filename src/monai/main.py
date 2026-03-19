"""monAI — Fully autonomous money-making AI agent.

Runs as a daemon. Discovers opportunities, provisions its own infrastructure,
spawns sub-agents, manages clients, delivers work, invoices, and scales.
Zero human intervention.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import asyncio
import os
import signal
import sys
import threading
import time
from datetime import datetime

from monai.agents.identity import IdentityManager
from monai.agents.orchestrator import Orchestrator
from monai.payments.webhook_server import WebhookServer
from monai.business.commercialista import Commercialista
from monai.business.finance import Finance
from monai.business.risk import RiskManager
from monai.config import Config
from monai.db.database import Database
from monai.strategies.digital_products import DigitalProductsAgent
from monai.utils.llm import LLM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("monai")

# Graceful shutdown
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    if _shutdown:
        # Second signal — force exit immediately
        logger.warning("Force shutdown (second signal). Exiting now.")
        import os
        os._exit(1)
    logger.info("Shutdown signal received. Finishing current cycle...")
    _shutdown = True
    # Signal LLM layer to abort in-flight calls immediately
    from monai.utils.llm import _shutdown_flag
    _shutdown_flag.set()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def init_strategies(db: Database):
    """Seed initial strategies if none exist.

    Strategies start as 'pending' — they are only activated by
    validate_strategies() after checking that the required assets
    (email, platform accounts, payment methods, domains, etc.)
    actually exist.  This prevents phantom strategies that the
    system is "convinced" it has but can't actually execute.
    """
    existing = db.execute("SELECT COUNT(*) as count FROM strategies")
    if existing[0]["count"] > 0:
        return

    strategies = [
        ("digital_products", "products", "Ebooks, templates, prompt packs, guides on Gumroad", 50.0),
    ]
    for name, category, description, budget in strategies:
        db.execute_insert(
            "INSERT INTO strategies (name, category, description, allocated_budget, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (name, category, description, budget),
        )
    logger.info(f"Initialized {len(strategies)} strategies as 'pending' (will activate after asset validation)")


def create_orchestrator(config: Config) -> tuple[Orchestrator, Database]:
    """Create and wire up the full autonomous system."""
    db = Database()
    llm = LLM(config, caller="orchestrator")
    llm.set_db(db)  # Enable persistent cost logging

    init_strategies(db)

    # Add file logging now that config dir is guaranteed
    file_handler = logging.FileHandler(config.data_dir / "monai.log", mode="a")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)

    orchestrator = Orchestrator(config, db, llm)

    # Register the one strategy we're focusing on
    dp_llm = LLM(config, caller="digital_products")
    dp_llm.set_db(db)
    orchestrator.register_strategy(DigitalProductsAgent(config, db, dp_llm))

    # ── Startup strategy validation ──────────────────────────────
    # 1. Demote any 'active' strategies that have no registered agent
    #    (phantom entries from previous sessions)
    registered_names = set(orchestrator._strategy_agents.keys())
    demoted = orchestrator.strategy_lifecycle.demote_active_without_agent(registered_names)
    if demoted:
        logger.warning(f"Demoted phantom strategies without agents: {demoted}")

    # 2. Validate all strategies against actual asset inventory —
    #    only activate strategies whose requirements are met
    validation = orchestrator.strategy_lifecycle.validate_strategies()
    active = validation.get("activated", []) + validation.get("already_active", [])
    paused = [p["name"] if isinstance(p, dict) else p for p in validation.get("paused", [])]
    brands_reg, brands_rm = validation.get("brands_registered", 0), validation.get("brands_removed", 0)
    logger.info(
        f"Strategy validation complete: "
        f"{len(active)} active, {len(paused)} paused, "
        f"{brands_reg} brands registered, {brands_rm} brands removed"
    )

    return orchestrator, db


def _start_webhook_server(orchestrator, webhook_port: int = 8420):
    """Start webhook server in a background thread with its own event loop.

    Registers all payment providers from the orchestrator's payment manager
    and routes events through _handle_webhook_event.
    """
    webhook_server = WebhookServer(host="0.0.0.0", port=webhook_port)

    # Register all providers from the payment manager
    pm = orchestrator.payment_manager
    for name, provider in pm._providers.items():
        webhook_server.register_provider(name, provider)

    # Wire events to the payment manager's handler
    webhook_server.on_event(pm._handle_webhook_event)

    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(webhook_server.start())
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True, name="webhook-server")
    thread.start()
    logger.info(f"Webhook server started on port {webhook_port}")
    return webhook_server, loop


def run_daemon(config: Config, cycle_interval: int = 300):
    """Run monAI as a continuous daemon.

    Starts the webhook server in a background thread to receive payments,
    then runs orchestration cycles in the main thread.

    Args:
        cycle_interval: Seconds between cycles (default 5 min)
    """
    from monai.infra.daemon_state import (
        CycleMetrics, DaemonState, acquire_pid, release_pid,
    )

    # Prevent duplicate daemons
    if not acquire_pid():
        logger.error("Another monAI daemon is already running. Exiting.")
        sys.exit(1)

    state = DaemonState.load()
    state.pid = os.getpid()
    state.started_at = time.time()
    state.save()

    orchestrator, db = create_orchestrator(config)
    identity = IdentityManager(config, db, LLM(config))

    agent_name = identity.get_identity().get("name", "monAI")
    logger.info(f"{'='*60}")
    logger.info(f"  {agent_name} starting in autonomous daemon mode")
    logger.info(f"  Cycle interval: {cycle_interval}s | PID: {os.getpid()}")
    logger.info(f"{'='*60}")

    # Start webhook server in background thread
    webhook_port = getattr(config, "webhook_port", 8420)
    webhook_server = None
    webhook_loop = None
    try:
        webhook_server, webhook_loop = _start_webhook_server(orchestrator, webhook_port)
    except Exception as e:
        logger.error(f"Webhook server failed to start: {e}")
        logger.warning("Continuing without webhook server — payments will NOT be received")

    cycle = state.cycles_completed  # Resume count across restarts
    consecutive_failures = state.consecutive_failures
    max_backoff_multiplier = 12  # Cap at 12x base interval (1 hour at 300s base)

    while not _shutdown:
        cycle += 1
        cycle_start = time.time()
        logger.info(f"\n{'='*60}")
        logger.info(f"  CYCLE {cycle} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"{'='*60}")

        # Watchdog: run cycle with hard timeout (10 minutes) to detect hangs
        # Check _shutdown every second so Ctrl+C is responsive
        cycle_timeout = 600  # seconds
        cycle_failed = False
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(orchestrator.run)
                # Poll future in 1s increments so we can respond to Ctrl+C
                elapsed_wait = 0
                result = None
                while elapsed_wait < cycle_timeout:
                    if _shutdown:
                        future.cancel()
                        logger.info("Aborting cycle due to shutdown signal...")
                        break
                    try:
                        result = future.result(timeout=1)
                        break  # Cycle completed normally
                    except concurrent.futures.TimeoutError:
                        elapsed_wait += 1
                        continue
                else:
                    # Exceeded cycle_timeout — signal all executors to stop
                    from monai.agents.executor import AutonomousExecutor
                    AutonomousExecutor.cancel_cycle()
                    logger.critical(
                        f"WATCHDOG: Cycle {cycle} exceeded {cycle_timeout}s timeout — "
                        "force-completing and moving to next cycle"
                    )
                    cycle_failed = True
                    continue

                if result is not None:
                    _print_cycle_summary(result, db)
                    # Detect LLM unavailability from cycle result
                    status = result.get("status", "")
                    if status in ("llm_unavailable", "llm_quota_exhausted"):
                        cycle_failed = True
                    else:
                        cycle_failed = False
        except Exception as e:
            logger.error(f"Cycle {cycle} failed: {e}", exc_info=True)
            cycle_failed = True
            result = None

        # Record cycle metrics
        cycle_end = time.time()
        api = result.get("api_costs_session", {}) if result else {}
        metrics = CycleMetrics(
            cycle=cycle,
            started_at=cycle_start,
            duration_secs=round(cycle_end - cycle_start, 1),
            success=not cycle_failed,
            error=str(result.get("status", "")) if cycle_failed and result else "",
            api_calls=api.get("total_calls", 0),
            api_cost_eur=api.get("total_cost_eur", 0),
            strategies_run=result.get("health", {}).get("active_strategies", 0) if result else 0,
            net_profit=result.get("net_profit", 0) if result else 0,
        )
        state.record_cycle(metrics)

        if _shutdown:
            break

        # Exponential backoff: when cycles keep failing (LLM quota exhausted,
        # persistent errors), increase the wait time to avoid wasting resources
        # (browser launches, email provisioning, API retries)
        if cycle_failed:
            consecutive_failures += 1
            backoff_multiplier = min(2 ** (consecutive_failures - 1), max_backoff_multiplier)
        else:
            consecutive_failures = 0
            backoff_multiplier = 1

        wait_time = int(cycle_interval * backoff_multiplier)
        if backoff_multiplier > 1:
            logger.warning(
                f"Backoff active: {consecutive_failures} consecutive failures, "
                f"waiting {wait_time}s (base {cycle_interval}s × {backoff_multiplier}x)"
            )
        else:
            logger.info(f"Next cycle in {wait_time}s...")

        for _ in range(wait_time):
            if _shutdown:
                break
            time.sleep(1)

    # Shut down webhook server cleanly
    if webhook_server and webhook_loop:
        try:
            future = asyncio.run_coroutine_threadsafe(webhook_server.stop(), webhook_loop)
            future.result(timeout=5)
        except Exception as e:
            logger.warning(f"Error stopping webhook server: {e}")
        finally:
            webhook_loop.call_soon_threadsafe(webhook_loop.stop)

    # Clean up daemon state
    state.save()
    release_pid()
    logger.info("monAI shut down gracefully.")


def run_once(config: Config):
    """Run a single orchestration cycle with watchdog timeout."""
    orchestrator, db = create_orchestrator(config)
    cycle_timeout = 600  # 10 minutes
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(orchestrator.run)
        result = future.result(timeout=cycle_timeout)
    _print_cycle_summary(result, db)
    return result


def show_status(config: Config):
    """Show current system status with full financial report."""
    db = Database()
    finance = Finance(db)
    risk = RiskManager(config, db)
    llm = LLM(config, caller="status")
    identity = IdentityManager(config, db, llm)
    commercialista = Commercialista(config, db)

    health = risk.get_portfolio_health()
    agent_identity = identity.get_identity()
    accounts = identity.get_all_accounts()
    budget = commercialista.get_budget()

    print(f"\n{'='*60}")
    print(f"  {agent_identity.get('name', 'monAI')} — Status")
    print(f"{'='*60}")
    print(f"  Identity:            {agent_identity.get('name', 'Not set')}")
    print(f"  Accounts:            {len(accounts)}")
    print(f"  Active strategies:   {health['active_strategies']}")
    print(f"  Diversification OK:  {health['diversification_ok']}")

    # Commercialista report
    print(f"\n  {'─'*50}")
    print(f"  COMMERCIALISTA REPORT")
    print(f"  {'─'*50}")
    print(f"  Initial budget:      €{budget['initial']:.2f}")
    print(f"  Current balance:     €{budget['balance']:.2f}")
    print(f"  Total revenue:       €{budget['revenue']:.2f}")
    print(f"  Total expenses:      €{budget['expenses']:.2f}")
    print(f"  Net profit:          €{budget['net_profit']:.2f}")
    print(f"  Self-sustaining:     {budget['self_sustaining']}")
    print(f"  Burn rate:           €{budget['burn_rate_daily']:.4f}/day")
    if budget['days_until_broke'] is not None:
        print(f"  Days until broke:    {budget['days_until_broke']}")

    # Costs by agent
    costs_by_agent = commercialista.get_cost_by_agent()
    if costs_by_agent:
        print(f"\n  API Costs by Agent:")
        for c in costs_by_agent:
            print(f"    {c['agent_name']:25s}  Calls: {c['calls']:5d}  Cost: €{c['total_cost']:.4f}")

    # Costs by model
    costs_by_model = commercialista.get_cost_by_model()
    if costs_by_model:
        print(f"\n  API Costs by Model:")
        for c in costs_by_model:
            print(f"    {c['model']:25s}  Calls: {c['calls']:5d}  Cost: €{c['total_cost']:.4f}")

    if health["strategy_details"]:
        print(f"\n  Strategy P&L:")
        for s in health["strategy_details"]:
            print(f"    {s['name']:25s}  Rev: €{s['revenue']:8.2f}  "
                  f"Exp: €{s['expenses']:8.2f}  Net: €{s['net']:8.2f}")

    if accounts:
        print(f"\n  Accounts:")
        for a in accounts:
            print(f"    {a['platform']:20s} {a['type']:20s} {a['identifier']}")

    daily = finance.get_daily_summary()
    print(f"\n  Today: Rev €{daily['revenue']:.2f} | "
          f"Exp €{daily['expenses']:.2f} | Net €{daily['net']:.2f}")
    print(f"{'='*60}\n")


def discover(config: Config):
    """Discover new opportunities."""
    orchestrator, _ = create_orchestrator(config)
    opportunities = orchestrator.discover_opportunities()

    print(f"\nDiscovered Opportunities:")
    for i, opp in enumerate(opportunities, 1):
        print(f"\n  {i}. {opp.get('name', 'Unknown')}")
        print(f"     Category:       {opp.get('category', '?')}")
        print(f"     Est. monthly:   €{opp.get('estimated_monthly_revenue', 0):.0f}")
        print(f"     Startup cost:   €{opp.get('startup_cost', 0):.0f}")
        print(f"     Risk:           {opp.get('risk_level', '?')}")
        print(f"     Automatable:    {opp.get('can_automate', '?')}")
        print(f"     How to start:   {opp.get('how_to_start', '?')}")


def _print_cycle_summary(result: dict, db: Database):
    finance = Finance(db)
    budget = result.get("budget_after", result.get("budget", {}))
    api = result.get("api_costs_session", {})

    print(f"\n{'='*60}")
    print(f"  Cycle Complete — {result.get('timestamp', '')}")
    print(f"{'='*60}")
    print(f"  Net profit:     €{result.get('net_profit', 0):.2f}")
    print(f"  Budget left:    €{budget.get('balance', 0):.2f}")
    print(f"  API calls:      {api.get('total_calls', 0)} (€{api.get('total_cost_eur', 0):.4f})")
    print(f"  Strategies:     {result.get('health', {}).get('active_strategies', 0)} active")
    if result.get("provisioning", {}).get("provisioned"):
        print(f"  Provisioned:    {result['provisioning']['provisioned']}")
    if result.get("subagent_results"):
        print(f"  Sub-agents:     {len(result['subagent_results'])} tasks delegated")
    res = result.get("resources", {})
    if res:
        print(f"  Memory:         {res.get('memory_mb', 0)}MB / {res.get('memory_limit_mb', 0)}MB")
    print(f"{'='*60}\n")


def _auto_setup() -> dict:
    """Run zero-touch infrastructure setup before anything else.

    Ensures Tor, Monero wallet RPC, LLM access, and config are all
    provisioned automatically. The creator never touches a config file.
    """
    from monai.infra.auto_setup import InfraSetup
    setup = InfraSetup()
    results = setup.run_all()

    if not results.get("ready"):
        # Log what failed but DON'T exit — let the system try to operate
        failed = [k for k, v in results.items()
                  if isinstance(v, dict) and v.get("status") == "failed"]
        if failed:
            logger.warning(
                f"Auto-setup: some components failed: {failed}. "
                f"monAI will continue with reduced capabilities."
            )
    else:
        logger.info("Auto-setup: all infrastructure ready")

    return results


def main():
    parser = argparse.ArgumentParser(description="monAI — Autonomous money-making AI")
    parser.add_argument("command", nargs="?", default="daemon",
                        choices=["daemon", "run", "status", "init", "discover", "dashboard"],
                        help="Command: daemon (default), run (single cycle), status, init, discover, dashboard")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between daemon cycles (default: 300)")
    parser.add_argument("--port", type=int, default=8421,
                        help="Dashboard server port (default: 8421)")
    parser.add_argument("--skip-setup", action="store_true",
                        help="Skip auto-setup (for testing)")
    args = parser.parse_args()

    # Zero-touch infrastructure setup — runs BEFORE config load
    if not args.skip_setup and args.command in ("daemon", "run", "init"):
        _auto_setup()

    config = Config.load()

    if not config.llm.api_key:
        # Don't hard-fail — auto-setup may have configured Ollama
        logger.warning(
            "No LLM API key found. If Ollama is running locally, monAI will "
            "use it. Otherwise, set OPENAI_API_KEY or ANTHROPIC_API_KEY."
        )
        if args.command in ("daemon", "run"):
            # One more check — did auto-setup configure a local key?
            config = Config.load()  # Reload after auto-setup
            if not config.llm.api_key:
                print("Error: No LLM access available.")
                print("Options:")
                print("  1. export OPENAI_API_KEY=sk-...")
                print("  2. export ANTHROPIC_API_KEY=sk-ant-...")
                print("  3. Install Ollama: curl -fsSL https://ollama.ai/install.sh | sh")
                sys.exit(1)

    # Validate payout configuration
    if args.command in ("daemon", "run"):
        has_payout = (
            config.retoswap.enabled
            or config.creator_wallet.xmr_address
            or config.llc.enabled
        )
        if not has_payout:
            logger.warning(
                "No payout method configured. Revenue will accumulate but NOT be swept. "
                "Configure one of: retoswap (PayPal F&F/cash), creator_wallet.xmr_address, or LLC."
            )

    if args.command == "dashboard":
        import asyncio
        from monai.dashboard.server import run_dashboard
        asyncio.run(run_dashboard(config, port=args.port))
        return

    if args.command == "init":
        config.save()
        db = Database()
        init_strategies(db)
        identity = IdentityManager(config, db, LLM(config))
        agent = identity.get_identity()
        print(f"monAI initialized as: {agent.get('name', 'monAI')}")
        print(f"Config: ~/.monai/config.json")
        print(f"Run: monai daemon")
    elif args.command == "status":
        show_status(config)
    elif args.command == "discover":
        discover(config)
    elif args.command == "run":
        run_once(config)
    elif args.command == "daemon":
        run_daemon(config, cycle_interval=args.interval)


if __name__ == "__main__":
    main()
