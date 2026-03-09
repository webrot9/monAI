"""LLC Provisioner — fully autonomous business entity formation.

Creates the complete multi-layer corporate structure:
1. Forms a Wyoming LLC via registered agent service
2. Applies for EIN (IRS Form SS-4 online)
3. Opens a business bank account (Mercury — 100% online)
4. Connects payment platforms (Stripe, etc.) to the LLC bank
5. Registers the creator as external contractor
6. Assigns existing brands to the LLC

All steps use browser automation through the AutonomousExecutor.
The creator is notified via Telegram at each major milestone.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.agents.executor import AutonomousExecutor
from monai.agents.identity import IdentityManager
from monai.business.corporate import CorporateManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

# Step status tracking
LLC_PROVISION_SCHEMA = """
CREATE TABLE IF NOT EXISTS llc_provision_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_name TEXT NOT NULL,
    step_name TEXT NOT NULL,
    step_order INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    result TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    UNIQUE(entity_name, step_name)
);
"""

# Wyoming is best: no public member names, low cost, no state income tax
JURISDICTION_CONFIGS = {
    "US-WY": {
        "name": "Wyoming",
        "sos_url": "https://wyobiz.wyo.gov/Business/FilingSearch.aspx",
        "filing_url": "https://wyobiz.wyo.gov/Business/NewBusinessFiling.aspx",
        "filing_fee": 100,
        "annual_fee": 60,
        "registered_agent_required": True,
        "member_disclosure": False,
        "formation_time_days": 1,
    },
    "US-NM": {
        "name": "New Mexico",
        "sos_url": "https://www.sos.state.nm.us/business-services/",
        "filing_fee": 50,
        "annual_fee": 0,
        "registered_agent_required": False,
        "member_disclosure": False,
        "formation_time_days": 1,
    },
    "US-DE": {
        "name": "Delaware",
        "sos_url": "https://icis.corp.delaware.gov/ecorp/",
        "filing_fee": 90,
        "annual_fee": 300,
        "registered_agent_required": True,
        "member_disclosure": False,
        "formation_time_days": 1,
    },
}

# Registered agent services (cheapest and most automation-friendly)
REGISTERED_AGENTS = {
    "northwest": {
        "name": "Northwest Registered Agent",
        "url": "https://www.northwestregisteredagent.com",
        "cost_yearly": 125,
        "includes_formation": True,
        "online_signup": True,
    },
    "incfile": {
        "name": "IncFile",
        "url": "https://www.incfile.com",
        "cost_yearly": 0,  # Free registered agent for first year
        "includes_formation": True,
        "online_signup": True,
        "formation_fee": 0,  # They charge only state fee
    },
    "zenbusiness": {
        "name": "ZenBusiness",
        "url": "https://www.zenbusiness.com",
        "cost_yearly": 0,  # Free first year
        "includes_formation": True,
        "online_signup": True,
    },
}

# Bank options (100% online, LLC-friendly)
BANK_OPTIONS = {
    "mercury": {
        "name": "Mercury",
        "url": "https://mercury.com",
        "type": "business_checking",
        "monthly_fee": 0,
        "min_deposit": 0,
        "accepts_llc": True,
        "online_only": True,
        "stripe_compatible": True,
    },
    "relay": {
        "name": "Relay",
        "url": "https://relayfi.com",
        "type": "business_checking",
        "monthly_fee": 0,
        "min_deposit": 0,
        "accepts_llc": True,
        "online_only": True,
        "stripe_compatible": True,
    },
}


class LLCProvisioner(BaseAgent):
    """Autonomously creates LLC, gets EIN, opens bank, connects platforms."""

    name = "llc_provisioner"
    description = (
        "Creates the complete multi-layer corporate structure autonomously. "
        "Forms LLC, gets EIN, opens bank account, connects payment platforms."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.identity = IdentityManager(config, db, llm)
        self.executor = AutonomousExecutor(config, db, llm, max_steps=80)
        self.corporate = CorporateManager(db)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(LLC_PROVISION_SCHEMA)

    def plan(self) -> list[str]:
        """Plan LLC provisioning steps."""
        steps = [
            "Check LLC name availability in target jurisdiction",
            "Create registered agent account for LLC formation",
            "File LLC formation documents with the state",
            "Apply for EIN (IRS Form SS-4)",
            "Open business bank account (Mercury)",
            "Connect Stripe to LLC bank account",
            "Set up creator as external contractor",
            "Assign existing brands to the LLC entity",
        ]
        return steps

    # ── Main Entry Point ───────────────────────────────────────

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Execute the full LLC provisioning pipeline.

        Picks up where it left off if interrupted.
        """
        llc_name = kwargs.get("llc_name") or self.config.llc.entity_name
        jurisdiction = kwargs.get("jurisdiction") or self.config.llc.jurisdiction
        agent_service = kwargs.get("agent_service", "incfile")

        if not llc_name:
            llc_name = self._generate_llc_name()

        self.log_action("llc_provision_start",
                        f"Starting: {llc_name} in {jurisdiction}")

        # Initialize steps if not already tracked
        self._init_steps(llc_name)

        results: dict[str, Any] = {
            "llc_name": llc_name,
            "jurisdiction": jurisdiction,
            "steps": {},
        }

        # Execute each step in order, skipping completed ones
        steps = self._get_steps(llc_name)
        loop = asyncio.new_event_loop()

        try:
            for step in steps:
                if step["status"] == "completed":
                    results["steps"][step["step_name"]] = {"status": "completed"}
                    continue

                if step["attempts"] >= step["max_attempts"]:
                    results["steps"][step["step_name"]] = {
                        "status": "max_attempts_reached",
                        "error": step.get("error", ""),
                    }
                    continue

                self.log_action("llc_step_start", step["step_name"])

                try:
                    step_result = loop.run_until_complete(
                        self._execute_step(
                            step["step_name"], llc_name,
                            jurisdiction, agent_service,
                        )
                    )
                    results["steps"][step["step_name"]] = step_result

                    if step_result.get("status") != "completed":
                        # Stop pipeline on failure — next cycle will retry
                        self.log_action("llc_step_failed",
                                        f"{step['step_name']}: {step_result.get('error', '')}")
                        break

                except Exception as e:
                    logger.error(f"LLC step {step['step_name']} failed: {e}")
                    self._record_step_failure(llc_name, step["step_name"], str(e))
                    results["steps"][step["step_name"]] = {
                        "status": "error", "error": str(e),
                    }
                    break
        finally:
            loop.close()

        # Check if all steps completed
        all_done = all(
            s.get("status") == "completed"
            for s in results["steps"].values()
        )
        results["status"] = "completed" if all_done else "in_progress"

        if all_done:
            self.log_action("llc_provision_complete", llc_name)

        return results

    # ── Step Execution ─────────────────────────────────────────

    async def _execute_step(self, step_name: str, llc_name: str,
                            jurisdiction: str,
                            agent_service: str) -> dict[str, Any]:
        """Execute a single provisioning step."""
        self._increment_attempt(llc_name, step_name)

        handler = {
            "check_name_availability": self._step_check_name,
            "register_agent_account": self._step_register_agent,
            "file_llc_formation": self._step_file_formation,
            "apply_ein": self._step_apply_ein,
            "open_bank_account": self._step_open_bank,
            "connect_stripe": self._step_connect_stripe,
            "setup_contractor": self._step_setup_contractor,
            "assign_brands": self._step_assign_brands,
        }.get(step_name)

        if not handler:
            return {"status": "error", "error": f"Unknown step: {step_name}"}

        result = await handler(llc_name, jurisdiction, agent_service)

        if result.get("status") == "completed":
            self._complete_step(llc_name, step_name, result)
        else:
            self._record_step_failure(
                llc_name, step_name, result.get("error", "Unknown")
            )

        return result

    async def _step_check_name(self, llc_name: str, jurisdiction: str,
                               agent_service: str) -> dict[str, Any]:
        """Step 1: Check if the LLC name is available."""
        jur = JURISDICTION_CONFIGS.get(jurisdiction, {})
        sos_url = jur.get("sos_url", "")

        task = (
            f"Check if the LLC name '{llc_name}' is available in {jur.get('name', jurisdiction)}.\n\n"
            f"1. Go to {sos_url}\n"
            f"2. Search for the business name '{llc_name}'\n"
            f"3. Check if the name is already taken\n"
            f"4. If available, report 'available'. If taken, report 'taken'.\n\n"
            f"Return the result as: available or taken"
        )

        result = await self.executor.execute_task(task, json.dumps({
            "llc_name": llc_name,
            "jurisdiction": jurisdiction,
            "sos_url": sos_url,
        }))

        if result.get("status") == "completed":
            result_text = str(result.get("result", "")).lower()
            if "taken" in result_text:
                # Generate alternative name
                alt_name = self._generate_llc_name()
                return {
                    "status": "name_taken",
                    "error": f"Name '{llc_name}' is taken. Suggested: {alt_name}",
                    "suggested_name": alt_name,
                }
            return {"status": "completed", "name_available": True}

        return {"status": "error", "error": result.get("reason", "Check failed")}

    async def _step_register_agent(self, llc_name: str, jurisdiction: str,
                                   agent_service: str) -> dict[str, Any]:
        """Step 2: Create an account with the registered agent service."""
        agent = REGISTERED_AGENTS.get(agent_service, REGISTERED_AGENTS["incfile"])
        identity = self.identity.get_identity()

        # Check if we already have an account with this service
        existing = self.identity.get_account(agent_service)
        if existing:
            return {"status": "completed", "account": "existing"}

        password = self.identity.generate_password()
        email = identity.get("email", "")

        task = (
            f"Create an account on {agent['name']} ({agent['url']}).\n\n"
            f"Use these details:\n"
            f"- Email: {email}\n"
            f"- Password: {password}\n"
            f"- Name: {identity.get('name', 'Business Owner')}\n\n"
            f"Steps:\n"
            f"1. Go to {agent['url']}\n"
            f"2. Find the 'Sign Up' or 'Get Started' button\n"
            f"3. Create an account with the email and password above\n"
            f"4. Complete any verification steps\n"
            f"5. Report the account creation result\n\n"
            f"DO NOT enter any payment information yet."
        )

        result = await self.executor.execute_task(task, json.dumps({
            "email": email,
            "service": agent["name"],
        }))

        if result.get("status") == "completed":
            # Store the account credentials
            self.identity.store_account(
                platform=agent_service,
                identifier=email,
                credentials={"password": password},
                metadata={"service_name": agent["name"], "url": agent["url"]},
                account_type="service",
            )
            return {"status": "completed", "service": agent["name"]}

        return {"status": "error", "error": result.get("reason", "Registration failed")}

    async def _step_file_formation(self, llc_name: str, jurisdiction: str,
                                   agent_service: str) -> dict[str, Any]:
        """Step 3: File LLC formation through the registered agent service."""
        agent = REGISTERED_AGENTS.get(agent_service, REGISTERED_AGENTS["incfile"])
        jur = JURISDICTION_CONFIGS.get(jurisdiction, {})
        identity = self.identity.get_identity()
        account = self.identity.get_account(agent_service)

        if not account:
            return {"status": "error", "error": "No agent service account found"}

        task = (
            f"File LLC formation for '{llc_name}' through {agent['name']}.\n\n"
            f"Log in to {agent['url']} with:\n"
            f"- Email: {account.get('identifier', '')}\n"
            f"- Password: (stored credentials)\n\n"
            f"LLC Details:\n"
            f"- Name: {llc_name}\n"
            f"- State: {jur.get('name', jurisdiction)}\n"
            f"- Type: Limited Liability Company\n"
            f"- Management: Manager-managed\n"
            f"- Purpose: General business purposes\n"
            f"- Registered Agent: Use {agent['name']}'s registered agent service\n\n"
            f"Steps:\n"
            f"1. Log in to {agent['url']}\n"
            f"2. Start the LLC formation process\n"
            f"3. Select {jur.get('name', jurisdiction)} as the state\n"
            f"4. Enter the LLC name: {llc_name}\n"
            f"5. Fill in all required formation details\n"
            f"6. Select the free/basic package (no upsells)\n"
            f"7. Complete payment (state filing fee: ${jur.get('filing_fee', 100)})\n"
            f"8. Submit the formation\n"
            f"9. Capture and report the order confirmation number\n\n"
            f"IMPORTANT: Only proceed with the basic/free formation package. "
            f"Decline all upsells (EIN service, operating agreement, etc.)."
        )

        creds = json.loads(account.get("credentials", "{}"))
        result = await self.executor.execute_task(task, json.dumps({
            "email": account.get("identifier", ""),
            "password": creds.get("password", ""),
            "llc_name": llc_name,
            "state": jur.get("name", jurisdiction),
        }))

        if result.get("status") == "completed":
            # Create entity in corporate system
            entity_id = self.corporate.create_entity(
                name=llc_name,
                entity_type="llc_us",
                jurisdiction=jurisdiction,
                registered_agent=agent["name"],
                formation_date=datetime.now().strftime("%Y-%m-%d"),
                metadata={
                    "filing_service": agent_service,
                    "order_confirmation": result.get("result", ""),
                    "state_fee": jur.get("filing_fee", 100),
                },
            )
            return {
                "status": "completed",
                "entity_id": entity_id,
                "confirmation": result.get("result", ""),
            }

        return {"status": "error", "error": result.get("reason", "Formation failed")}

    async def _step_apply_ein(self, llc_name: str, jurisdiction: str,
                              agent_service: str) -> dict[str, Any]:
        """Step 4: Apply for EIN online (IRS Form SS-4).

        The IRS offers free online EIN application at:
        https://sa.www4.irs.gov/modiein/individual/index.jsp
        EIN is issued immediately online.
        """
        entity = self.corporate.get_primary_entity()
        if not entity:
            return {"status": "error", "error": "No entity found in DB"}

        identity = self.identity.get_identity()

        task = (
            f"Apply for an EIN (Employer Identification Number) on the IRS website.\n\n"
            f"Go to: https://sa.www4.irs.gov/modiein/individual/index.jsp\n\n"
            f"Application details:\n"
            f"- Entity type: Limited Liability Company (LLC)\n"
            f"- LLC name: {llc_name}\n"
            f"- State: {JURISDICTION_CONFIGS.get(jurisdiction, {}).get('name', jurisdiction)}\n"
            f"- Number of members: 1\n"
            f"- Responsible party: {identity.get('name', 'Business Owner')}\n"
            f"- Reason for applying: Started new business\n"
            f"- Principal activity: Consulting / Professional services\n"
            f"- Date business started: {entity.get('formation_date', datetime.now().strftime('%Y-%m-%d'))}\n\n"
            f"Steps:\n"
            f"1. Navigate to the IRS EIN online application\n"
            f"2. Select 'Limited Liability Company' as entity type\n"
            f"3. Fill in all required information\n"
            f"4. Submit the application\n"
            f"5. The EIN is issued immediately — capture the EIN number\n"
            f"6. Save/download the EIN confirmation letter\n\n"
            f"CRITICAL: Record the exact EIN number (format: XX-XXXXXXX).\n"
            f"The IRS only issues it once online — if you lose it, you must call."
        )

        result = await self.executor.execute_task(task, json.dumps({
            "llc_name": llc_name,
            "identity": identity,
        }))

        if result.get("status") == "completed":
            ein = self._extract_ein(str(result.get("result", "")))
            if ein:
                self.db.execute(
                    "UPDATE corporate_entities SET ein_or_tax_id = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (ein, entity["id"]),
                )
                return {"status": "completed", "ein": ein}
            return {
                "status": "completed",
                "ein": "pending_extraction",
                "raw_result": result.get("result", ""),
            }

        return {"status": "error", "error": result.get("reason", "EIN application failed")}

    async def _step_open_bank(self, llc_name: str, jurisdiction: str,
                              agent_service: str) -> dict[str, Any]:
        """Step 5: Open a business bank account (Mercury)."""
        entity = self.corporate.get_primary_entity()
        if not entity:
            return {"status": "error", "error": "No entity found"}

        ein = entity.get("ein_or_tax_id", "")
        identity = self.identity.get_identity()
        bank = BANK_OPTIONS["mercury"]

        password = self.identity.generate_password()
        email = identity.get("email", "")

        task = (
            f"Open a business checking account on Mercury ({bank['url']}).\n\n"
            f"Business details:\n"
            f"- Business name: {llc_name}\n"
            f"- Entity type: LLC\n"
            f"- EIN: {ein or 'pending'}\n"
            f"- State of formation: {JURISDICTION_CONFIGS.get(jurisdiction, {}).get('name', jurisdiction)}\n"
            f"- Industry: Technology consulting\n\n"
            f"Account holder:\n"
            f"- Name: {identity.get('name', 'Business Owner')}\n"
            f"- Email: {email}\n"
            f"- Password: {password}\n\n"
            f"Steps:\n"
            f"1. Go to {bank['url']}\n"
            f"2. Click 'Open an Account' or 'Get Started'\n"
            f"3. Select 'LLC' as business type\n"
            f"4. Enter business details\n"
            f"5. Enter the owner/manager information\n"
            f"6. Upload formation documents if required\n"
            f"7. Submit the application\n"
            f"8. Capture the account status (approved/pending review)\n\n"
            f"Note: Mercury may take 1-3 business days to approve. "
            f"That's OK — capture the application confirmation."
        )

        result = await self.executor.execute_task(task, json.dumps({
            "llc_name": llc_name,
            "ein": ein,
            "email": email,
        }))

        if result.get("status") == "completed":
            # Store bank account
            self.identity.store_account(
                platform="mercury",
                identifier=email,
                credentials={"password": password},
                metadata={
                    "bank_name": "Mercury",
                    "llc_name": llc_name,
                    "application_result": result.get("result", ""),
                },
                account_type="bank",
            )

            self.corporate.update_entity_bank(
                entity["id"], "Mercury", "pending_approval",
            )

            return {
                "status": "completed",
                "bank": "Mercury",
                "application": result.get("result", "submitted"),
            }

        return {"status": "error", "error": result.get("reason", "Bank opening failed")}

    async def _step_connect_stripe(self, llc_name: str, jurisdiction: str,
                                   agent_service: str) -> dict[str, Any]:
        """Step 6: Create Stripe account connected to LLC bank."""
        entity = self.corporate.get_primary_entity()
        if not entity:
            return {"status": "error", "error": "No entity found"}

        identity = self.identity.get_identity()
        ein = entity.get("ein_or_tax_id", "")
        password = self.identity.generate_password()
        email = identity.get("email", "")

        task = (
            f"Create a Stripe account for the business '{llc_name}'.\n\n"
            f"Go to: https://dashboard.stripe.com/register\n\n"
            f"Account details:\n"
            f"- Email: {email}\n"
            f"- Password: {password}\n"
            f"- Business name: {llc_name}\n"
            f"- Business type: LLC\n"
            f"- EIN: {ein or 'will provide later'}\n"
            f"- Country: United States\n"
            f"- Industry: Software / SaaS\n\n"
            f"Steps:\n"
            f"1. Go to stripe.com/register\n"
            f"2. Create the account with email and password\n"
            f"3. Fill in business verification details\n"
            f"4. Skip adding a bank account for now (we'll do it when Mercury is approved)\n"
            f"5. Complete the basic setup\n"
            f"6. Capture the Stripe account ID (starts with 'acct_')\n\n"
            f"Don't worry about full activation — the basic account is enough to start."
        )

        result = await self.executor.execute_task(task, json.dumps({
            "llc_name": llc_name,
            "email": email,
        }))

        if result.get("status") == "completed":
            self.identity.store_account(
                platform="stripe",
                identifier=email,
                credentials={"password": password},
                metadata={
                    "llc_name": llc_name,
                    "setup_result": result.get("result", ""),
                },
                account_type="payment_processor",
            )
            return {"status": "completed", "stripe": "account_created"}

        return {"status": "error", "error": result.get("reason", "Stripe setup failed")}

    async def _step_setup_contractor(self, llc_name: str, jurisdiction: str,
                                     agent_service: str) -> dict[str, Any]:
        """Step 7: Register the creator as external contractor in DB."""
        entity = self.corporate.get_primary_entity()
        if not entity:
            return {"status": "error", "error": "No entity found"}

        # Check if contractor already exists
        existing = self.corporate.get_active_contractor(entity["id"])
        if existing:
            return {"status": "completed", "contractor": existing["alias"]}

        alias = self.config.llc.contractor_alias
        if not alias:
            alias = self._generate_contractor_alias()

        contractor_id = self.corporate.create_contractor(
            alias=alias,
            entity_id=entity["id"],
            service_description=self.config.llc.contractor_service,
            rate_type=self.config.llc.contractor_rate_type,
            rate_amount=self.config.llc.contractor_rate_amount,
            rate_percentage=self.config.llc.contractor_rate_percentage,
            payment_method=self.config.llc.contractor_payment_method,
        )

        return {
            "status": "completed",
            "contractor_id": contractor_id,
            "alias": alias,
        }

    async def _step_assign_brands(self, llc_name: str, jurisdiction: str,
                                  agent_service: str) -> dict[str, Any]:
        """Step 8: Assign existing brands to the LLC."""
        entity = self.corporate.get_primary_entity()
        if not entity:
            return {"status": "error", "error": "No entity found"}

        # Find brands that aren't assigned to any entity
        strategies = self.db.execute(
            "SELECT DISTINCT name FROM strategies WHERE status = 'active'"
        )
        brand_names = [s["name"] for s in strategies]

        assigned = []
        for brand in brand_names:
            existing = self.corporate.get_brand_entity(brand)
            if not existing:
                self.corporate.assign_brand(entity["id"], brand)
                assigned.append(brand)

        return {
            "status": "completed",
            "assigned_brands": assigned,
            "total_brands": len(brand_names),
        }

    # ── Step Management ────────────────────────────────────────

    def _init_steps(self, llc_name: str):
        """Initialize the provisioning steps if not already tracked."""
        steps = [
            ("check_name_availability", 1),
            ("register_agent_account", 2),
            ("file_llc_formation", 3),
            ("apply_ein", 4),
            ("open_bank_account", 5),
            ("connect_stripe", 6),
            ("setup_contractor", 7),
            ("assign_brands", 8),
        ]
        for step_name, order in steps:
            self.db.execute(
                "INSERT OR IGNORE INTO llc_provision_steps "
                "(entity_name, step_name, step_order) VALUES (?, ?, ?)",
                (llc_name, step_name, order),
            )

    def _get_steps(self, llc_name: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM llc_provision_steps "
            "WHERE entity_name = ? ORDER BY step_order",
            (llc_name,),
        )]

    def _complete_step(self, llc_name: str, step_name: str,
                       result: dict) -> None:
        self.db.execute(
            "UPDATE llc_provision_steps SET status = 'completed', "
            "result = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE entity_name = ? AND step_name = ?",
            (json.dumps(result, default=str), llc_name, step_name),
        )

    def _record_step_failure(self, llc_name: str, step_name: str,
                             error: str) -> None:
        self.db.execute(
            "UPDATE llc_provision_steps SET status = 'failed', "
            "error = ? WHERE entity_name = ? AND step_name = ?",
            (error, llc_name, step_name),
        )

    def _increment_attempt(self, llc_name: str, step_name: str) -> None:
        self.db.execute(
            "UPDATE llc_provision_steps SET attempts = attempts + 1, "
            "status = 'in_progress' "
            "WHERE entity_name = ? AND step_name = ?",
            (llc_name, step_name),
        )

    # ── Helpers ─────────────────────────────────────────────────

    def _generate_llc_name(self) -> str:
        """Generate a generic, non-traceable LLC name using LLM."""
        response = self.think_json(
            "Generate a professional, generic LLC name for a technology "
            "holding company. The name should:\n"
            "- Sound like a real business (not AI-generated)\n"
            "- Not reference the creator or any specific brand\n"
            "- Be short (2-3 words + LLC)\n"
            "- Sound like a consulting or technology firm\n"
            "Examples: 'Meridian Peak LLC', 'Cascade Ventures LLC', "
            "'Summit Ridge Holdings LLC'\n\n"
            "Return: {\"name\": \"Your Generated Name LLC\"}",
        )
        return response.get("name", "Alpine Ventures LLC")

    def _generate_contractor_alias(self) -> str:
        """Generate a professional contractor alias."""
        response = self.think_json(
            "Generate a professional consulting business name for an "
            "individual contractor. Should sound like a real consulting "
            "firm. Examples: 'Pinnacle Advisory', 'Clearpath Consulting'.\n\n"
            "Return: {\"alias\": \"Your Generated Name\"}",
        )
        return response.get("alias", "Independent Consulting")

    @staticmethod
    def _extract_ein(text: str) -> str:
        """Extract EIN (XX-XXXXXXX format) from text."""
        import re
        match = re.search(r'\b(\d{2}-\d{7})\b', text)
        return match.group(1) if match else ""

    def get_provision_status(self) -> dict[str, Any]:
        """Get current provisioning status."""
        llc_name = self.config.llc.entity_name
        if not llc_name:
            return {"status": "not_configured", "hint": "Set llc.entity_name in config"}

        steps = self._get_steps(llc_name)
        if not steps:
            return {"status": "not_started", "llc_name": llc_name}

        completed = sum(1 for s in steps if s["status"] == "completed")
        failed = sum(1 for s in steps if s["status"] == "failed")
        current = next(
            (s["step_name"] for s in steps if s["status"] not in ("completed",)),
            "all_done",
        )

        return {
            "llc_name": llc_name,
            "total_steps": len(steps),
            "completed": completed,
            "failed": failed,
            "current_step": current,
            "progress_pct": round(completed / len(steps) * 100) if steps else 0,
            "steps": [
                {
                    "name": s["step_name"],
                    "status": s["status"],
                    "attempts": s["attempts"],
                }
                for s in steps
            ],
        }
