"""Constraint-aware planner — ensures monAI never attempts actions without prerequisites.

Before executing any provisioning or business action, this module builds a dependency
graph of required steps, checks each against the asset inventory, and produces an
executable plan with proper topological ordering. Known dependency rules are hardcoded;
novel goals are analyzed via LLM and then validated against inventory.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from monai.agents.asset_aware import AssetInventory, AssetManager
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)


class StepStatus(Enum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ProvisioningStep:
    """A single step in a provisioning plan."""
    action: str
    platform: str
    priority: int  # Lower number = higher priority
    reason: str
    estimated_cost: float = 0.0
    dependencies: list[str] = field(default_factory=list)  # Step IDs this depends on
    status: StepStatus = StepStatus.PENDING
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    failure_reason: str = ""


class CircularDependencyError(Exception):
    """Raised when the dependency graph contains a cycle."""
    pass


class DependencyGraph:
    """Manages provisioning steps with real prerequisite chains.

    Maintains a DAG of ProvisioningSteps and provides methods for querying
    execution readiness, topological ordering, and validation.
    """

    def __init__(self) -> None:
        self._steps: dict[str, ProvisioningStep] = {}

    @property
    def steps(self) -> list[ProvisioningStep]:
        return list(self._steps.values())

    def add_step(self, step: ProvisioningStep) -> str:
        """Add a step to the graph. Returns the step ID."""
        if step.id in self._steps:
            raise ValueError(f"Step with ID {step.id!r} already exists")
        for dep_id in step.dependencies:
            if dep_id not in self._steps:
                raise ValueError(
                    f"Dependency {dep_id!r} not found in graph. "
                    f"Add dependencies before dependents."
                )
        self._steps[step.id] = step
        return step.id

    def get_step(self, step_id: str) -> ProvisioningStep | None:
        return self._steps.get(step_id)

    def get_ready_steps(self) -> list[ProvisioningStep]:
        """Return steps whose dependencies are all satisfied (completed)."""
        ready = []
        for step in self._steps.values():
            if step.status not in (StepStatus.PENDING, StepStatus.READY):
                continue
            deps_satisfied = all(
                self._steps[dep_id].status == StepStatus.COMPLETED
                for dep_id in step.dependencies
                if dep_id in self._steps
            )
            if deps_satisfied:
                step.status = StepStatus.READY
                ready.append(step)
        return sorted(ready, key=lambda s: s.priority)

    def mark_completed(self, step_id: str) -> None:
        """Mark a step as done."""
        step = self._steps.get(step_id)
        if step is None:
            raise KeyError(f"Step {step_id!r} not found")
        step.status = StepStatus.COMPLETED
        logger.info("Step completed: %s (%s)", step.action, step_id)

    def mark_failed(self, step_id: str, reason: str) -> None:
        """Mark a step as failed and cascade-skip all dependents."""
        step = self._steps.get(step_id)
        if step is None:
            raise KeyError(f"Step {step_id!r} not found")
        step.status = StepStatus.FAILED
        step.failure_reason = reason
        logger.warning("Step failed: %s (%s) — %s", step.action, step_id, reason)
        self._cascade_skip(step_id, reason)

    def _cascade_skip(self, failed_id: str, reason: str) -> None:
        """Skip all steps that transitively depend on a failed step."""
        for step in self._steps.values():
            if step.status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED):
                continue
            if failed_id in step.dependencies:
                step.status = StepStatus.SKIPPED
                step.failure_reason = f"Dependency {failed_id!r} failed: {reason}"
                logger.info("Step skipped (cascade): %s (%s)", step.action, step.id)
                self._cascade_skip(step.id, reason)

    def get_execution_order(self) -> list[ProvisioningStep]:
        """Topological sort of all steps. Raises on circular dependencies."""
        self.validate()

        in_degree: dict[str, int] = {sid: 0 for sid in self._steps}
        for step in self._steps.values():
            for dep_id in step.dependencies:
                if dep_id in in_degree:
                    in_degree[step.id] = in_degree.get(step.id, 0)
                    # dep_id is a prerequisite of step — step's in-degree increases
                    pass

        # Recompute properly
        in_degree = {sid: 0 for sid in self._steps}
        adjacency: dict[str, list[str]] = {sid: [] for sid in self._steps}
        for step in self._steps.values():
            for dep_id in step.dependencies:
                if dep_id in self._steps:
                    adjacency[dep_id].append(step.id)
                    in_degree[step.id] += 1

        queue: deque[str] = deque()
        for sid, deg in in_degree.items():
            if deg == 0:
                queue.append(sid)

        result: list[ProvisioningStep] = []
        while queue:
            # Among available nodes, pick by priority
            candidates = sorted(queue, key=lambda sid: self._steps[sid].priority)
            current = candidates[0]
            queue.remove(current)
            result.append(self._steps[current])
            for neighbor in adjacency[current]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(self._steps):
            raise CircularDependencyError(
                "Dependency graph contains a cycle — cannot determine execution order"
            )
        return result

    def validate(self) -> bool:
        """Check for circular dependencies. Raises CircularDependencyError if found."""
        # Kahn's algorithm — just check if topo sort covers all nodes
        in_degree: dict[str, int] = {sid: 0 for sid in self._steps}
        for step in self._steps.values():
            for dep_id in step.dependencies:
                if dep_id in self._steps:
                    in_degree[step.id] += 1

        queue: deque[str] = deque(
            sid for sid, deg in in_degree.items() if deg == 0
        )
        visited = 0
        adjacency: dict[str, list[str]] = {sid: [] for sid in self._steps}
        for step in self._steps.values():
            for dep_id in step.dependencies:
                if dep_id in self._steps:
                    adjacency[dep_id].append(step.id)

        temp_in = dict(in_degree)
        while queue:
            node = queue.popleft()
            visited += 1
            for neighbor in adjacency[node]:
                temp_in[neighbor] -= 1
                if temp_in[neighbor] == 0:
                    queue.append(neighbor)

        if visited != len(self._steps):
            raise CircularDependencyError(
                "Dependency graph contains a cycle — cannot determine execution order"
            )
        return True

    @property
    def is_complete(self) -> bool:
        """True if all steps are in a terminal state."""
        return all(
            s.status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED)
            for s in self._steps.values()
        )

    @property
    def has_failures(self) -> bool:
        return any(s.status == StepStatus.FAILED for s in self._steps.values())

    def summary(self) -> str:
        """Human-readable plan summary."""
        if not self._steps:
            return "Empty plan — nothing to provision."
        lines = ["Provisioning Plan:"]
        try:
            ordered = self.get_execution_order()
        except CircularDependencyError:
            ordered = list(self._steps.values())
            lines.append("  WARNING: circular dependencies detected, order is approximate")

        for i, step in enumerate(ordered, 1):
            deps_str = ""
            if step.dependencies:
                dep_names = []
                for dep_id in step.dependencies:
                    dep_step = self._steps.get(dep_id)
                    dep_names.append(dep_step.action if dep_step else dep_id)
                deps_str = f" (after: {', '.join(dep_names)})"
            cost_str = f" [~${step.estimated_cost:.2f}]" if step.estimated_cost > 0 else ""
            status_str = f" [{step.status.value}]" if step.status != StepStatus.PENDING else ""
            lines.append(
                f"  {i}. [{step.platform}] {step.action}{deps_str}{cost_str}{status_str}"
            )
            lines.append(f"     Reason: {step.reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Standard dependency rules — hardcoded, not LLM-guessed
# ---------------------------------------------------------------------------

_STANDARD_RULES: list[dict[str, Any]] = [
    {
        "action": "email_creation",
        "requires": [],
        "description": "Create email account",
        "priority": 10,
        "platform": "email",
        "estimated_cost": 0.0,
    },
    {
        "action": "platform_signup",
        "requires": ["email_creation"],
        "description": "Sign up for platform account",
        "priority": 20,
        "platform": "",  # Filled per-goal
        "estimated_cost": 0.0,
    },
    {
        "action": "api_key_acquisition",
        "requires": ["platform_signup"],
        "description": "Acquire API key from platform",
        "priority": 30,
        "platform": "",
        "estimated_cost": 0.0,
    },
    {
        "action": "domain_purchase",
        "requires": ["payment_method_setup"],
        "description": "Purchase a domain name",
        "priority": 30,
        "platform": "registrar",
        "estimated_cost": 12.0,
    },
    {
        "action": "payment_method_setup",
        "requires": [],
        "description": "Set up payment method",
        "priority": 15,
        "platform": "payment",
        "estimated_cost": 0.0,
    },
    {
        "action": "payment_processing_setup",
        "requires": ["llc_formation"],
        "description": "Set up payment processing (Stripe, etc.)",
        "priority": 40,
        "platform": "stripe",
        "estimated_cost": 0.0,
    },
    {
        "action": "llc_formation",
        "requires": [],
        "description": "Form LLC or business entity",
        "priority": 5,
        "platform": "legal",
        "estimated_cost": 100.0,
    },
]


def _get_standard_rule(action: str) -> dict[str, Any] | None:
    for rule in _STANDARD_RULES:
        if rule["action"] == action:
            return rule
    return None


# Mapping from goal keywords to the standard action chains needed
_GOAL_TO_ACTIONS: dict[str, list[str]] = {
    "platform_signup": ["email_creation", "platform_signup"],
    "signup": ["email_creation", "platform_signup"],
    "register": ["email_creation", "platform_signup"],
    "create_account": ["email_creation", "platform_signup"],
    "api_key": ["email_creation", "platform_signup", "api_key_acquisition"],
    "domain": ["payment_method_setup", "domain_purchase"],
    "buy_domain": ["payment_method_setup", "domain_purchase"],
    "purchase_domain": ["payment_method_setup", "domain_purchase"],
    "payment_processing": ["llc_formation", "payment_processing_setup"],
    "stripe_setup": ["llc_formation", "payment_processing_setup"],
    "email": ["email_creation"],
    "create_email": ["email_creation"],
    # Specific infrastructure goals — minimal deps, no LLM expansion
    "telegram_bot": ["email_creation"],
    "telegram": ["email_creation"],
    "identity": [],
}


class ConstraintPlanner:
    """Uses AssetInventory + DependencyGraph to plan provisioning.

    For each goal, determines what assets are needed and which are missing,
    then builds a dependency graph with proper prerequisite chains. Known
    patterns use hardcoded rules; novel goals are analyzed via LLM.
    """

    # Payment processing threshold — above this amount, require LLC
    BUSINESS_ENTITY_THRESHOLD = 500.0  # USD

    def __init__(self, db: Database, llm: LLM) -> None:
        self.db = db
        self.llm = llm
        self.asset_manager = AssetManager(db)

    def plan(self, goals: list[str]) -> DependencyGraph:
        """Main entry point — build a constraint-aware provisioning plan.

        Args:
            goals: List of high-level goals (e.g., "signup for Upwork",
                   "acquire Stripe API key", "purchase domain example.com").

        Returns:
            DependencyGraph with all steps in proper dependency order.
        """
        inventory = self.asset_manager.get_inventory()
        graph = DependencyGraph()

        # Track which actions have already been added to avoid duplicates.
        # Key: (action_type, platform), Value: step_id
        added_steps: dict[tuple[str, str], str] = {}

        for goal in goals:
            chain = self._build_dependency_chain(goal, inventory)
            for step in chain:
                key = (step.action, step.platform)
                if key in added_steps:
                    # Rewrite dependencies to point to the existing step
                    existing_id = added_steps[key]
                    # Update any later steps in this chain that depend on
                    # the duplicate step to depend on the existing one instead
                    for later_step in chain:
                        later_step.dependencies = [
                            existing_id if d == step.id else d
                            for d in later_step.dependencies
                        ]
                    continue

                # Remap dependency IDs for steps added from earlier goals
                remapped_deps = []
                for dep_id in step.dependencies:
                    # Check if this dep_id was replaced by an existing step
                    remapped_deps.append(dep_id)
                step.dependencies = remapped_deps

                graph.add_step(step)
                added_steps[key] = step.id

        logger.info("Plan built: %d steps for %d goals", len(graph.steps), len(goals))
        return graph

    def _build_dependency_chain(
        self, goal: str, inventory: AssetInventory
    ) -> list[ProvisioningStep]:
        """Determine the prerequisite chain for a goal.

        Strategy:
        1. Check hardcoded rules first (fast, reliable for known patterns)
        2. For ANY goal, also ask LLM to validate/enrich the chain —
           the LLM sees the actual inventory and can catch missing deps
           that hardcoded rules don't cover
        3. Merge both, deduplicate

        Returns steps in dependency order (prerequisites first), with steps
        for already-satisfied prerequisites omitted.
        """
        goal_lower = goal.lower().strip()

        # 1. Try hardcoded rules (fast path for known patterns)
        standard_chain = self._match_standard_goal(goal_lower, inventory)

        if standard_chain is not None:
            # Hardcoded rules are authoritative — skip LLM enrichment.
            # LLM enrichment caused scope explosion: "telegram_bot" → 16
            # steps because the LLM inferred domain, hosting, payment
            # processing, LLC, GitHub, etc. as dependencies.
            return standard_chain

        # 2. No standard match — ask LLM for novel goals only
        return self._llm_dependency_chain(goal, inventory)

    def _match_standard_goal(
        self, goal: str, inventory: AssetInventory
    ) -> list[ProvisioningStep] | None:
        """Try to match a goal against known dependency patterns.

        Returns None if no standard pattern matches.
        """
        # Extract platform name if present
        platform = self._extract_platform(goal)

        # Find matching action chain
        matched_actions: list[str] | None = None
        for keyword, actions in _GOAL_TO_ACTIONS.items():
            if keyword in goal:
                matched_actions = actions
                break

        if matched_actions is None:
            return None

        return self._create_steps_from_actions(
            matched_actions, platform, goal, inventory
        )

    def _create_steps_from_actions(
        self,
        actions: list[str],
        platform: str,
        goal: str,
        inventory: AssetInventory,
    ) -> list[ProvisioningStep]:
        """Create ProvisioningSteps from a list of standard action names.

        Skips steps whose prerequisites are already satisfied in inventory.
        """
        steps: list[ProvisioningStep] = []
        action_to_step_id: dict[str, str] = {}
        satisfied_actions: set[str] = set()

        for action_name in actions:
            # Check if this step is already satisfied
            if self._is_satisfied(action_name, platform, inventory):
                satisfied_actions.add(action_name)
                continue

            rule = _get_standard_rule(action_name)
            if rule is None:
                continue

            step_platform = rule.get("platform", "") or platform

            # Resolve dependency IDs — only include deps that are actual
            # pending steps (not already-satisfied ones)
            dep_ids = []
            for req_action in rule.get("requires", []):
                if req_action in action_to_step_id:
                    dep_ids.append(action_to_step_id[req_action])
                # If req_action is in satisfied_actions or not in our chain,
                # the dependency is already met — skip it

            step = ProvisioningStep(
                action=action_name,
                platform=step_platform,
                priority=rule.get("priority", 50),
                reason=f"Required for: {goal}",
                estimated_cost=rule.get("estimated_cost", 0.0),
                dependencies=dep_ids,
            )
            steps.append(step)
            action_to_step_id[action_name] = step.id

        return steps

    def _is_satisfied(
        self, action: str, platform: str, inventory: AssetInventory
    ) -> bool:
        """Check if an action's output already exists in inventory."""
        if action == "email_creation":
            return inventory.has_email
        if action == "platform_signup":
            return bool(platform) and inventory.has_account(platform)
        if action == "api_key_acquisition":
            return bool(platform) and inventory.has_api_key(platform)
        if action == "domain_purchase":
            return inventory.has_domain()
        if action == "payment_method_setup":
            return inventory.has_payment_method()
        if action == "llc_formation":
            return any(a.type == "llc" and a.status == "active" for a in inventory.assets)
        if action == "payment_processing_setup":
            return bool(platform) and inventory.has_account(platform)
        return False

    def _llm_dependency_chain(
        self, goal: str, inventory: AssetInventory
    ) -> list[ProvisioningStep]:
        """Use LLM to determine prerequisites for a non-standard goal.

        The LLM response is validated against inventory to ensure we only
        create steps for things that are genuinely missing.
        """
        system_prompt = (
            "You are a prerequisite analyzer for an autonomous AI agent system. "
            "Given a goal the agent wants to achieve, determine what resources and "
            "steps are needed, in order.\n\n"
            "=== WHAT I HAVE (actual assets) ===\n"
            f"{inventory.summary()}\n\n"
            "=== WHAT I CAN DO (available capabilities) ===\n"
            "- Create email accounts (Gmail, ProtonMail, Outlook)\n"
            "- Register on platforms (Upwork, Fiverr, Stripe, Gumroad, etc.)\n"
            "- Purchase domains (Namecheap, Cloudflare)\n"
            "- Set up payment processing (Stripe, PayPal)\n"
            "- Form LLCs and business entities\n"
            "- Acquire API keys from platforms\n"
            "- Browse the web, fill forms, make HTTP calls\n"
            "- Write and test code\n"
            "- Create custom automation tools\n\n"
            "=== KNOWN RESOURCE TYPES ===\n"
            "email, platform_account, domain, api_key, payment_method, llc\n\n"
            "=== YOUR TASK ===\n"
            "Given a goal, determine ALL prerequisite steps needed. Think about:\n"
            "- What resources does this goal require that I don't have yet?\n"
            "- What's the correct ORDER (dependencies first)?\n"
            "- What could go wrong and what alternatives exist?\n\n"
            "Return JSON: {\"steps\": [...]} where each step has:\n"
            '  - "action": short identifier (snake_case)\n'
            '  - "platform": target platform/service\n'
            '  - "priority": integer (lower = do first)\n'
            '  - "reason": why this step is needed\n'
            '  - "estimated_cost": float in USD\n'
            '  - "depends_on_index": list of int indices of prerequisite steps '
            "(0-based, referring to earlier steps in the array)\n"
            '  - "already_satisfied": boolean — true if inventory already has this\n\n'
            "CRITICAL: Only include steps that are NOT already satisfied. "
            "Order steps so prerequisites come before dependents. "
            "Be specific about platforms and actions."
        )

        user_prompt = (
            f"Goal: {goal}\n\n"
            f"What steps are needed to achieve this goal? "
            f"Consider my current assets above — don't repeat what I already have."
        )

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=self.llm.get_model(LLM.TIER_MINI),
                temperature=0.2,
            )
        except Exception as e:
            logger.error("LLM failed to analyze goal %r: %s", goal, e)
            return [
                ProvisioningStep(
                    action="manual_review",
                    platform="unknown",
                    priority=1,
                    reason=f"Could not auto-plan goal: {goal}. LLM error: {e}",
                    estimated_cost=0.0,
                )
            ]

        return self._parse_llm_steps(response, goal, inventory)

    def _parse_llm_steps(
        self,
        response: dict[str, Any],
        goal: str,
        inventory: AssetInventory,
    ) -> list[ProvisioningStep]:
        """Parse and validate LLM-generated steps."""
        raw_steps = response.get("steps", [])
        if not isinstance(raw_steps, list):
            logger.warning("LLM returned non-list steps for goal %r", goal)
            return [
                ProvisioningStep(
                    action="manual_review",
                    platform="unknown",
                    priority=1,
                    reason=f"LLM returned invalid plan for: {goal}",
                    estimated_cost=0.0,
                )
            ]

        steps: list[ProvisioningStep] = []
        index_to_step_id: dict[int, str] = {}

        for i, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                continue

            # Skip steps the LLM marked as already satisfied
            if raw.get("already_satisfied", False):
                continue

            # Double-check against inventory
            action = str(raw.get("action", "unknown"))
            platform = str(raw.get("platform", "unknown"))
            if self._is_satisfied(action, platform, inventory):
                continue

            # Resolve dependency indices to step IDs
            dep_indices = raw.get("depends_on_index", [])
            if not isinstance(dep_indices, list):
                dep_indices = []
            dep_ids = [
                index_to_step_id[idx]
                for idx in dep_indices
                if isinstance(idx, int) and idx in index_to_step_id
            ]

            step = ProvisioningStep(
                action=action,
                platform=platform,
                priority=int(raw.get("priority", 50)),
                reason=str(raw.get("reason", f"Required for: {goal}")),
                estimated_cost=float(raw.get("estimated_cost", 0.0)),
                dependencies=dep_ids,
            )
            steps.append(step)
            index_to_step_id[i] = step.id

        return steps

    def _extract_platform(self, goal: str) -> str:
        """Extract a platform name from a goal string."""
        known_platforms = [
            "upwork", "fiverr", "stripe", "gumroad", "lemonsqueezy",
            "github", "linkedin", "twitter", "namecheap", "cloudflare",
            "vercel", "netlify", "heroku", "aws", "digitalocean",
            "google", "gmail", "protonmail",
        ]
        goal_lower = goal.lower()
        for p in known_platforms:
            if p in goal_lower:
                return p
        return ""

    @staticmethod
    def _get_standard_dependencies() -> list[dict[str, Any]]:
        """Return the known dependency rules for inspection/testing."""
        return [dict(r) for r in _STANDARD_RULES]
