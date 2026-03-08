"""monAI — Fully autonomous money-making AI agent.

Runs as a daemon. Discovers opportunities, provisions its own infrastructure,
spawns sub-agents, manages clients, delivers work, invoices, and scales.
Zero human intervention.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime

from monai.agents.identity import IdentityManager
from monai.agents.orchestrator import Orchestrator
from monai.business.commercialista import Commercialista
from monai.business.finance import Finance
from monai.business.risk import RiskManager
from monai.config import Config
from monai.db.database import Database
from monai.strategies.digital_products import DigitalProductsAgent
from monai.strategies.freelance_writing import FreelanceWritingAgent
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
    logger.info("Shutdown signal received. Finishing current cycle...")
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def init_strategies(db: Database):
    """Seed initial strategies if none exist."""
    existing = db.execute("SELECT COUNT(*) as count FROM strategies")
    if existing[0]["count"] > 0:
        return

    strategies = [
        ("freelance_writing", "services", "Freelance writing, blogging, copywriting", 10.0),
        ("digital_products", "products", "Ebooks, templates, prompt packs, guides", 10.0),
        ("cold_outreach", "services", "Cold email/LinkedIn outreach for B2B services", 10.0),
    ]
    for name, category, description, budget in strategies:
        db.execute_insert(
            "INSERT INTO strategies (name, category, description, allocated_budget) "
            "VALUES (?, ?, ?, ?)",
            (name, category, description, budget),
        )
    logger.info(f"Initialized {len(strategies)} default strategies")


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

    # Register built-in strategy agents — each with its own caller tag for cost tracking
    fw_llm = LLM(config, caller="freelance_writing")
    fw_llm.set_db(db)
    orchestrator.register_strategy(FreelanceWritingAgent(config, db, fw_llm))

    dp_llm = LLM(config, caller="digital_products")
    dp_llm.set_db(db)
    orchestrator.register_strategy(DigitalProductsAgent(config, db, dp_llm))

    return orchestrator, db


def run_daemon(config: Config, cycle_interval: int = 300):
    """Run monAI as a continuous daemon.

    Args:
        cycle_interval: Seconds between cycles (default 5 min)
    """
    orchestrator, db = create_orchestrator(config)
    identity = IdentityManager(config, db, LLM(config))

    agent_name = identity.get_identity().get("name", "monAI")
    logger.info(f"{'='*60}")
    logger.info(f"  {agent_name} starting in autonomous daemon mode")
    logger.info(f"  Cycle interval: {cycle_interval}s")
    logger.info(f"{'='*60}")

    cycle = 0
    while not _shutdown:
        cycle += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"  CYCLE {cycle} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"{'='*60}")

        try:
            result = orchestrator.run()
            _print_cycle_summary(result, db)
        except Exception as e:
            logger.error(f"Cycle {cycle} failed: {e}", exc_info=True)

        if _shutdown:
            break

        logger.info(f"Next cycle in {cycle_interval}s...")
        for _ in range(cycle_interval):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("monAI shut down gracefully.")


def run_once(config: Config):
    """Run a single orchestration cycle."""
    orchestrator, db = create_orchestrator(config)
    result = orchestrator.run()
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


def main():
    parser = argparse.ArgumentParser(description="monAI — Autonomous money-making AI")
    parser.add_argument("command", nargs="?", default="daemon",
                        choices=["daemon", "run", "status", "init", "discover"],
                        help="Command: daemon (default), run (single cycle), status, init, discover")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between daemon cycles (default: 300)")
    args = parser.parse_args()

    config = Config.load()

    if not config.llm.api_key:
        print("Error: OPENAI_API_KEY not set.")
        print("Set it via: export OPENAI_API_KEY=sk-...")
        print("Or add it to ~/.monai/config.json")
        sys.exit(1)

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
