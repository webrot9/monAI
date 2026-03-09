"""Workflow Engine — declarative pipeline orchestration for monAI.

Provides:
- Pipeline DSL: define multi-step workflows as code
- Step types: action, conditional, parallel fan-out/fan-in, loop, wait
- State machine: tracks workflow execution state with transitions
- Retry/backoff: automatic retry with exponential backoff
- Dead letter queue: failed tasks route to error handling
- Correlation IDs: trace a request through all agents
- Execution metrics: duration, success rates, bottleneck detection

Usage:
    pipeline = (
        Pipeline("client_acquisition")
        .step("find_leads", agent="lead_gen", action="research_niches")
        .step("qualify", agent="lead_gen", action="qualify_leads", depends_on=["find_leads"])
        .parallel("outreach", steps=[
            Step("email", agent="cold_outreach", action="send_emails"),
            Step("social", agent="social_media", action="find_clients"),
        ], depends_on=["qualify"])
        .conditional("has_responses",
            condition=lambda ctx: ctx.get("responses", 0) > 0,
            if_true="send_proposals",
            if_false="adjust_targeting")
        .step("send_proposals", agent="freelance_writing", action="send_proposals")
    )

    engine = WorkflowEngine(config, db, llm)
    result = engine.execute(pipeline, context={"niche": "b2b_saas"})
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

WORKFLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,                     -- correlation ID (UUID)
    pipeline_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending, running, completed, failed, cancelled
    context TEXT,                             -- JSON: workflow context/state
    result TEXT,                              -- JSON: final result
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    total_duration_ms INTEGER,
    steps_completed INTEGER DEFAULT 0,
    steps_failed INTEGER DEFAULT 0,
    steps_total INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    step_name TEXT NOT NULL,
    step_type TEXT NOT NULL,                 -- action, conditional, parallel, loop, wait
    agent TEXT,
    action TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, running, completed, failed, skipped, retrying
    input TEXT,                              -- JSON: step input
    output TEXT,                             -- JSON: step output
    error TEXT,
    attempt INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workflow_dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    error TEXT NOT NULL,
    context TEXT,                             -- JSON: state at failure
    attempts INTEGER DEFAULT 0,
    status TEXT DEFAULT 'unresolved',         -- unresolved, retried, resolved, abandoned
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class StepType(str, Enum):
    ACTION = "action"
    CONDITIONAL = "conditional"
    PARALLEL = "parallel"
    LOOP = "loop"
    WAIT = "wait"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Step:
    """A single step in a pipeline."""
    name: str
    step_type: StepType = StepType.ACTION
    agent: str = ""
    action: str = ""
    depends_on: list[str] = field(default_factory=list)
    max_retries: int = 3
    timeout_seconds: int = 300
    # For conditional steps
    condition: Callable[[dict], bool] | None = None
    if_true: str = ""
    if_false: str = ""
    # For parallel steps
    parallel_steps: list[Step] = field(default_factory=list)
    # For loop steps
    loop_over: str = ""  # Context key containing the iterable
    loop_step: Step | None = None
    # For wait steps
    wait_seconds: int = 0


@dataclass
class Pipeline:
    """Declarative pipeline definition — a DAG of steps."""
    name: str
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    version: int = 1

    def step(self, name: str, agent: str = "", action: str = "",
             depends_on: list[str] | None = None, max_retries: int = 3,
             timeout: int = 300) -> Pipeline:
        """Add an action step to the pipeline."""
        self.steps.append(Step(
            name=name, step_type=StepType.ACTION,
            agent=agent, action=action,
            depends_on=depends_on or [],
            max_retries=max_retries, timeout_seconds=timeout,
        ))
        return self

    def parallel(self, name: str, steps: list[Step],
                 depends_on: list[str] | None = None) -> Pipeline:
        """Add a parallel fan-out step (all sub-steps run concurrently)."""
        self.steps.append(Step(
            name=name, step_type=StepType.PARALLEL,
            parallel_steps=steps,
            depends_on=depends_on or [],
        ))
        return self

    def conditional(self, name: str, condition: Callable[[dict], bool],
                    if_true: str, if_false: str = "",
                    depends_on: list[str] | None = None) -> Pipeline:
        """Add a conditional branch step."""
        self.steps.append(Step(
            name=name, step_type=StepType.CONDITIONAL,
            condition=condition, if_true=if_true, if_false=if_false,
            depends_on=depends_on or [],
        ))
        return self

    def loop(self, name: str, loop_over: str, loop_step: Step,
             depends_on: list[str] | None = None) -> Pipeline:
        """Add a loop step that iterates over a context list."""
        self.steps.append(Step(
            name=name, step_type=StepType.LOOP,
            loop_over=loop_over, loop_step=loop_step,
            depends_on=depends_on or [],
        ))
        return self

    def wait(self, name: str, seconds: int,
             depends_on: list[str] | None = None) -> Pipeline:
        """Add a wait/delay step."""
        self.steps.append(Step(
            name=name, step_type=StepType.WAIT,
            wait_seconds=seconds,
            depends_on=depends_on or [],
        ))
        return self

    def get_step(self, name: str) -> Step | None:
        """Get a step by name (searches nested parallel steps too)."""
        for step in self.steps:
            if step.name == name:
                return step
            for ps in step.parallel_steps:
                if ps.name == name:
                    return ps
        return None

    def get_execution_order(self) -> list[list[str]]:
        """Topological sort: returns groups of steps that can run in parallel."""
        completed: set[str] = set()
        remaining = {s.name: set(s.depends_on) for s in self.steps}
        order: list[list[str]] = []

        while remaining:
            # Find steps whose dependencies are all met
            ready = [name for name, deps in remaining.items()
                     if deps.issubset(completed)]
            if not ready:
                # Circular dependency or missing step
                logger.error(f"Unresolvable dependencies: {remaining}")
                break
            order.append(ready)
            for name in ready:
                completed.add(name)
                del remaining[name]

        return order


class WorkflowEngine:
    """Executes pipelines with state tracking, retries, and routing."""

    def __init__(self, config: Config, db: Database, llm: LLM,
                 agent_registry: dict[str, Any] | None = None):
        self.config = config
        self.db = db
        self.llm = llm
        self._agents = agent_registry or {}

        with db.connect() as conn:
            conn.executescript(WORKFLOW_SCHEMA)

    def register_agent(self, name: str, agent: Any):
        """Register an agent that can be referenced by pipeline steps."""
        self._agents[name] = agent

    def execute(self, pipeline: Pipeline, context: dict | None = None) -> dict[str, Any]:
        """Execute a pipeline from start to finish.

        Args:
            pipeline: The pipeline definition to execute
            context: Initial context dict (shared state across steps)

        Returns:
            Result dict with status, outputs, and metrics
        """
        run_id = str(uuid.uuid4())[:12]
        ctx = dict(context or {})
        execution_order = pipeline.get_execution_order()

        # Create run record
        self.db.execute_insert(
            "INSERT INTO workflow_runs (id, pipeline_name, status, context, steps_total) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, pipeline.name, json.dumps(ctx, default=str),
             sum(len(group) for group in execution_order)),
        )

        start_time = time.time()
        step_results: dict[str, Any] = {}
        steps_completed = 0
        steps_failed = 0

        try:
            for group in execution_order:
                # Steps in the same group have no dependencies on each other
                # They could run in parallel, but we run sequentially for simplicity
                # (spawner handles true parallelism for action steps)
                for step_name in group:
                    step = pipeline.get_step(step_name)
                    if not step:
                        logger.warning(f"Step '{step_name}' not found in pipeline")
                        continue

                    result = self._execute_step(run_id, step, ctx, step_results)
                    step_results[step_name] = result

                    if result.get("status") == "completed":
                        steps_completed += 1
                        # Merge step output into context
                        if result.get("output"):
                            ctx[step_name] = result["output"]
                    elif result.get("status") == "skipped":
                        pass  # Conditional skip
                    elif result.get("status") == "failed":
                        steps_failed += 1
                        # Check if this is a fatal failure
                        if step.max_retries <= 0 or result.get("attempts", 0) >= step.max_retries:
                            self._dead_letter(run_id, step_name,
                                              result.get("error", "unknown"), ctx,
                                              result.get("attempts", 0))

            # Determine overall status
            final_status = "completed" if steps_failed == 0 else "failed"
            duration = int((time.time() - start_time) * 1000)

            self.db.execute(
                "UPDATE workflow_runs SET status = ?, result = ?, completed_at = ?, "
                "total_duration_ms = ?, steps_completed = ?, steps_failed = ? WHERE id = ?",
                (final_status, json.dumps(step_results, default=str)[:5000],
                 datetime.now().isoformat(), duration,
                 steps_completed, steps_failed, run_id),
            )

            return {
                "run_id": run_id,
                "pipeline": pipeline.name,
                "status": final_status,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "duration_ms": duration,
                "context": ctx,
                "results": step_results,
            }

        except Exception as e:
            duration = int((time.time() - start_time) * 1000)
            self.db.execute(
                "UPDATE workflow_runs SET status = 'failed', result = ?, "
                "completed_at = ?, total_duration_ms = ? WHERE id = ?",
                (json.dumps({"error": str(e)}), datetime.now().isoformat(),
                 duration, run_id),
            )
            logger.error(f"Pipeline '{pipeline.name}' failed: {e}")
            return {
                "run_id": run_id, "pipeline": pipeline.name,
                "status": "failed", "error": str(e),
                "duration_ms": duration,
            }

    def _execute_step(self, run_id: str, step: Step,
                      ctx: dict, prior_results: dict) -> dict[str, Any]:
        """Execute a single step with retry logic."""
        if step.step_type == StepType.ACTION:
            return self._execute_action_step(run_id, step, ctx)
        elif step.step_type == StepType.CONDITIONAL:
            return self._execute_conditional_step(run_id, step, ctx)
        elif step.step_type == StepType.PARALLEL:
            return self._execute_parallel_step(run_id, step, ctx)
        elif step.step_type == StepType.LOOP:
            return self._execute_loop_step(run_id, step, ctx)
        elif step.step_type == StepType.WAIT:
            return self._execute_wait_step(run_id, step)
        return {"status": "failed", "error": f"Unknown step type: {step.step_type}"}

    def _execute_action_step(self, run_id: str, step: Step,
                             ctx: dict) -> dict[str, Any]:
        """Execute an action step with retries."""
        step_id = self._record_step_start(run_id, step)
        start = time.time()

        for attempt in range(1, step.max_retries + 1):
            try:
                # Route to the appropriate agent
                agent = self._agents.get(step.agent)
                if not agent:
                    error = f"Agent '{step.agent}' not registered"
                    self._record_step_end(step_id, "failed", error=error)
                    return {"status": "failed", "error": error, "attempts": attempt}

                # Call the agent's action method
                action_method = getattr(agent, step.action, None)
                if action_method is None:
                    # Try the generic run method
                    action_method = getattr(agent, "run", None)

                if action_method is None:
                    error = f"Action '{step.action}' not found on agent '{step.agent}'"
                    self._record_step_end(step_id, "failed", error=error)
                    return {"status": "failed", "error": error, "attempts": attempt}

                # Execute
                output = action_method(**ctx) if ctx else action_method()
                duration = int((time.time() - start) * 1000)

                self._record_step_end(step_id, "completed",
                                      output=output, duration=duration, attempt=attempt)
                return {"status": "completed", "output": output,
                        "duration_ms": duration, "attempts": attempt}

            except Exception as e:
                logger.warning(f"Step '{step.name}' attempt {attempt} failed: {e}")
                if attempt < step.max_retries:
                    # Exponential backoff
                    backoff = min(2 ** attempt, 30)
                    self._record_step_end(step_id, "retrying",
                                          error=str(e), attempt=attempt)
                    time.sleep(backoff)
                    step_id = self._record_step_start(run_id, step)
                else:
                    duration = int((time.time() - start) * 1000)
                    self._record_step_end(step_id, "failed",
                                          error=str(e), duration=duration, attempt=attempt)
                    return {"status": "failed", "error": str(e),
                            "duration_ms": duration, "attempts": attempt}

        return {"status": "failed", "error": "max retries exhausted"}

    def _execute_conditional_step(self, run_id: str, step: Step,
                                  ctx: dict) -> dict[str, Any]:
        """Evaluate a condition and route to the appropriate branch."""
        step_id = self._record_step_start(run_id, step)

        try:
            if step.condition is None:
                self._record_step_end(step_id, "failed", error="No condition defined")
                return {"status": "failed", "error": "No condition defined"}

            result = step.condition(ctx)
            branch = step.if_true if result else step.if_false

            self._record_step_end(step_id, "completed",
                                  output={"condition_result": result, "branch": branch})

            return {
                "status": "completed",
                "output": {"condition_result": result, "branch": branch},
                "next_step": branch,
            }

        except Exception as e:
            self._record_step_end(step_id, "failed", error=str(e))
            return {"status": "failed", "error": str(e)}

    def _execute_parallel_step(self, run_id: str, step: Step,
                               ctx: dict) -> dict[str, Any]:
        """Execute parallel sub-steps (fan-out, then fan-in)."""
        step_id = self._record_step_start(run_id, step)
        start = time.time()

        results = {}
        all_ok = True
        for sub_step in step.parallel_steps:
            result = self._execute_action_step(run_id, sub_step, ctx)
            results[sub_step.name] = result
            if result.get("status") != "completed":
                all_ok = False

        duration = int((time.time() - start) * 1000)
        status = "completed" if all_ok else "failed"
        self._record_step_end(step_id, status, output=results, duration=duration)

        return {"status": status, "output": results, "duration_ms": duration}

    def _execute_loop_step(self, run_id: str, step: Step,
                           ctx: dict) -> dict[str, Any]:
        """Execute a step for each item in a context list."""
        step_id = self._record_step_start(run_id, step)
        start = time.time()

        items = ctx.get(step.loop_over, [])
        if not isinstance(items, list):
            self._record_step_end(step_id, "failed",
                                  error=f"'{step.loop_over}' is not a list in context")
            return {"status": "failed", "error": f"'{step.loop_over}' not iterable"}

        results = []
        for i, item in enumerate(items):
            if step.loop_step is None:
                continue
            loop_ctx = {**ctx, "loop_item": item, "loop_index": i}
            result = self._execute_action_step(run_id, step.loop_step, loop_ctx)
            results.append(result)

        duration = int((time.time() - start) * 1000)
        all_ok = all(r.get("status") == "completed" for r in results)
        status = "completed" if all_ok else "failed"

        self._record_step_end(step_id, status, output=results, duration=duration)
        return {"status": status, "output": results,
                "items_processed": len(results), "duration_ms": duration}

    def _execute_wait_step(self, run_id: str, step: Step) -> dict[str, Any]:
        """Wait for a specified duration."""
        step_id = self._record_step_start(run_id, step)
        time.sleep(min(step.wait_seconds, 60))  # Cap at 60s per wait
        self._record_step_end(step_id, "completed",
                              output={"waited_seconds": step.wait_seconds})
        return {"status": "completed", "output": {"waited_seconds": step.wait_seconds}}

    # ── DB Tracking ───────────────────────────────────────────────

    def _record_step_start(self, run_id: str, step: Step) -> int:
        return self.db.execute_insert(
            "INSERT INTO workflow_steps (run_id, step_name, step_type, agent, "
            "action, status, started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (run_id, step.name, step.step_type.value, step.agent,
             step.action, datetime.now().isoformat()),
        )

    def _record_step_end(self, step_id: int, status: str,
                         output: Any = None, error: str | None = None,
                         duration: int | None = None, attempt: int = 1):
        self.db.execute(
            "UPDATE workflow_steps SET status = ?, output = ?, error = ?, "
            "duration_ms = ?, attempt = ?, completed_at = ? WHERE id = ?",
            (status, json.dumps(output, default=str)[:5000] if output else None,
             error, duration, attempt, datetime.now().isoformat(), step_id),
        )

    def _dead_letter(self, run_id: str, step_name: str,
                     error: str, ctx: dict, attempts: int):
        """Route a permanently failed step to the dead letter queue."""
        self.db.execute_insert(
            "INSERT INTO workflow_dead_letters (run_id, step_name, error, context, attempts) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, step_name, error, json.dumps(ctx, default=str)[:2000], attempts),
        )
        logger.error(f"Dead letter: {step_name} in run {run_id}: {error}")

    # ── Metrics & Monitoring ──────────────────────────────────────

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Get a workflow run by ID."""
        rows = self.db.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        return dict(rows[0]) if rows else None

    def get_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        """Get all steps for a workflow run."""
        rows = self.db.execute(
            "SELECT * FROM workflow_steps WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [dict(r) for r in rows]

    def get_pipeline_stats(self, pipeline_name: str) -> dict[str, Any]:
        """Get aggregate stats for a pipeline."""
        rows = self.db.execute(
            "SELECT status, COUNT(*) as count, AVG(total_duration_ms) as avg_duration, "
            "AVG(steps_completed) as avg_completed, AVG(steps_failed) as avg_failed "
            "FROM workflow_runs WHERE pipeline_name = ? GROUP BY status",
            (pipeline_name,),
        )
        stats = {r["status"]: dict(r) for r in rows}

        total_runs = sum(s.get("count", 0) for s in stats.values())
        completed = stats.get("completed", {}).get("count", 0)

        return {
            "pipeline": pipeline_name,
            "total_runs": total_runs,
            "success_rate": (completed / total_runs * 100) if total_runs else 0,
            "by_status": stats,
        }

    def get_step_stats(self, pipeline_name: str) -> list[dict[str, Any]]:
        """Get per-step performance stats for a pipeline."""
        rows = self.db.execute(
            "SELECT ws.step_name, ws.agent, ws.action, "
            "COUNT(*) as executions, "
            "SUM(CASE WHEN ws.status = 'completed' THEN 1 ELSE 0 END) as successes, "
            "AVG(ws.duration_ms) as avg_duration_ms, "
            "MAX(ws.attempt) as max_retries_used "
            "FROM workflow_steps ws "
            "JOIN workflow_runs wr ON ws.run_id = wr.id "
            "WHERE wr.pipeline_name = ? "
            "GROUP BY ws.step_name ORDER BY avg_duration_ms DESC",
            (pipeline_name,),
        )
        return [dict(r) for r in rows]

    def get_dead_letters(self, status: str = "unresolved") -> list[dict[str, Any]]:
        """Get dead letter queue entries."""
        rows = self.db.execute(
            "SELECT * FROM workflow_dead_letters WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
        return [dict(r) for r in rows]

    def resolve_dead_letter(self, dead_letter_id: int, resolution: str = "resolved"):
        """Mark a dead letter as resolved."""
        self.db.execute(
            "UPDATE workflow_dead_letters SET status = ? WHERE id = ?",
            (resolution, dead_letter_id),
        )
