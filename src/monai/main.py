"""monAI — main entry point.

Initializes the system, registers strategies, and runs the orchestrator.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from monai.agents.orchestrator import Orchestrator
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
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Config.load().data_dir / "monai.log", mode="a"),
    ],
)
logger = logging.getLogger("monai")


def init_default_strategies(db: Database):
    """Seed default strategies if none exist."""
    existing = db.execute("SELECT COUNT(*) as count FROM strategies")
    if existing[0]["count"] > 0:
        return

    strategies = [
        ("freelance_writing", "services", "Freelance writing, blogging, copywriting"),
        ("digital_products", "products", "Ebooks, templates, prompt packs, guides"),
        ("cold_outreach", "services", "Cold email outreach for B2B services"),
    ]
    for name, category, description in strategies:
        db.execute_insert(
            "INSERT INTO strategies (name, category, description, allocated_budget) "
            "VALUES (?, ?, ?, ?)",
            (name, category, description, 10.0),
        )
    logger.info(f"Initialized {len(strategies)} default strategies")


def run_cycle(config: Config):
    """Run one full orchestration cycle."""
    db = Database()
    llm = LLM(config)

    init_default_strategies(db)

    # Create orchestrator
    orchestrator = Orchestrator(config, db, llm)

    # Register strategy agents
    orchestrator.register_strategy(FreelanceWritingAgent(config, db, llm))
    orchestrator.register_strategy(DigitalProductsAgent(config, db, llm))

    # Run one cycle
    result = orchestrator.run()

    # Print summary
    finance = Finance(db)
    risk = RiskManager(config, db)
    health = risk.get_portfolio_health()

    print("\n" + "=" * 60)
    print(f"  monAI Cycle Complete — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    print(f"  Active strategies: {health['active_strategies']}")
    print(f"  Total revenue:     ${finance.get_total_revenue():.2f}")
    print(f"  Total expenses:    ${finance.get_total_expenses():.2f}")
    print(f"  Net profit:        ${finance.get_net_profit():.2f}")
    print(f"  Profitable:        {health['profitable_strategies']}")
    print(f"  Losing:            {health['losing_strategies']}")
    print("=" * 60 + "\n")

    return result


def show_status(config: Config):
    """Show current system status."""
    db = Database()
    finance = Finance(db)
    risk = RiskManager(config, db)
    health = risk.get_portfolio_health()

    print("\n" + "=" * 60)
    print("  monAI Status")
    print("=" * 60)
    print(f"  Active strategies:   {health['active_strategies']}")
    print(f"  Diversification OK:  {health['diversification_ok']}")
    print(f"  Total net profit:    ${health['total_net_profit']:.2f}")
    print(f"  Profitable:          {health['profitable_strategies']}")
    print(f"  Losing:              {health['losing_strategies']}")

    if health["strategy_details"]:
        print("\n  Strategy P&L:")
        for s in health["strategy_details"]:
            print(f"    {s['name']:25s}  Rev: ${s['revenue']:8.2f}  "
                  f"Exp: ${s['expenses']:8.2f}  Net: ${s['net']:8.2f}")

    daily = finance.get_daily_summary()
    print(f"\n  Today: Rev ${daily['revenue']:.2f} | "
          f"Exp ${daily['expenses']:.2f} | Net ${daily['net']:.2f}")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="monAI — Autonomous money-making AI agent")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "status", "init", "discover"],
                        help="Command to execute")
    parser.add_argument("--config", type=str, help="Path to config file")
    args = parser.parse_args()

    config = Config.load()

    if not config.llm.api_key:
        print("Error: OPENAI_API_KEY not set. Set it in env or ~/.monai/config.json")
        sys.exit(1)

    if args.command == "init":
        config.save()
        db = Database()
        init_default_strategies(db)
        print("monAI initialized. Config saved to ~/.monai/config.json")
    elif args.command == "status":
        show_status(config)
    elif args.command == "discover":
        db = Database()
        llm = LLM(config)
        orchestrator = Orchestrator(config, db, llm)
        opportunities = orchestrator.discover_opportunities()
        print("\nDiscovered Opportunities:")
        for i, opp in enumerate(opportunities, 1):
            print(f"\n  {i}. {opp.get('name', 'Unknown')}")
            print(f"     Category: {opp.get('category', '?')}")
            print(f"     Est. monthly: ${opp.get('estimated_monthly_revenue', 0):.0f}")
            print(f"     Startup cost: ${opp.get('startup_cost', 0):.0f}")
            print(f"     Risk: {opp.get('risk_level', '?')}")
    elif args.command == "run":
        run_cycle(config)


if __name__ == "__main__":
    main()
