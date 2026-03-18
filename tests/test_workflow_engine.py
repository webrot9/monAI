"""Tests for the Workflow Engine, Task Router, and Pipelines."""

import json
import time
from unittest.mock import MagicMock

import pytest

from monai.config import Config
from monai.db.database import Database
from monai.workflows.engine import (
    Pipeline, Step, StepType, StepStatus, RunStatus, WorkflowEngine,
)
from monai.workflows.router import TaskRouter, DEFAULT_CAPABILITIES, TASK_TYPE_CAPABILITIES
from monai.workflows.pipelines import (
    digital_products_pipeline,
    get_pipeline, list_pipelines, PIPELINE_REGISTRY,
)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.data_dir = tmp_path
    return c


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.quick.return_value = "test"
    llm.chat_json.return_value = {}
    llm.chat.return_value = "test"
    return llm


# ── Pipeline DSL ──────────────────────────────────────────────


class TestPipeline:
    def test_create_pipeline(self):
        p = Pipeline("test")
        assert p.name == "test"
        assert p.steps == []

    def test_add_step(self):
        p = Pipeline("test").step("s1", agent="a", action="do_thing")
        assert len(p.steps) == 1
        assert p.steps[0].name == "s1"
        assert p.steps[0].agent == "a"
        assert p.steps[0].step_type == StepType.ACTION

    def test_chain_steps(self):
        p = (Pipeline("test")
             .step("s1", agent="a", action="a1")
             .step("s2", agent="b", action="b1", depends_on=["s1"])
             .step("s3", agent="c", action="c1", depends_on=["s2"]))
        assert len(p.steps) == 3

    def test_parallel_step(self):
        p = Pipeline("test").parallel("fan_out", steps=[
            Step("a", agent="x", action="do_x"),
            Step("b", agent="y", action="do_y"),
        ])
        assert p.steps[0].step_type == StepType.PARALLEL
        assert len(p.steps[0].parallel_steps) == 2

    def test_conditional_step(self):
        p = Pipeline("test").conditional(
            "check", condition=lambda ctx: True,
            if_true="go", if_false="stop"
        )
        assert p.steps[0].step_type == StepType.CONDITIONAL

    def test_wait_step(self):
        p = Pipeline("test").wait("pause", seconds=5)
        assert p.steps[0].step_type == StepType.WAIT
        assert p.steps[0].wait_seconds == 5

    def test_get_step(self):
        p = Pipeline("test").step("s1", agent="a", action="a1")
        assert p.get_step("s1") is not None
        assert p.get_step("nonexistent") is None

    def test_get_step_in_parallel(self):
        p = Pipeline("test").parallel("fan_out", steps=[
            Step("inner", agent="x", action="do_x"),
        ])
        assert p.get_step("inner") is not None

    def test_execution_order_linear(self):
        p = (Pipeline("test")
             .step("a")
             .step("b", depends_on=["a"])
             .step("c", depends_on=["b"]))
        order = p.get_execution_order()
        assert order == [["a"], ["b"], ["c"]]

    def test_execution_order_parallel(self):
        p = (Pipeline("test")
             .step("a")
             .step("b")
             .step("c", depends_on=["a", "b"]))
        order = p.get_execution_order()
        assert ["a", "b"] in order or set(order[0]) == {"a", "b"}
        assert order[-1] == ["c"]

    def test_execution_order_diamond(self):
        p = (Pipeline("test")
             .step("a")
             .step("b", depends_on=["a"])
             .step("c", depends_on=["a"])
             .step("d", depends_on=["b", "c"]))
        order = p.get_execution_order()
        assert order[0] == ["a"]
        assert set(order[1]) == {"b", "c"}
        assert order[2] == ["d"]


# ── Workflow Engine ───────────────────────────────────────────


class TestWorkflowEngine:
    def test_schema_created(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        for table in ["workflow_runs", "workflow_steps", "workflow_dead_letters"]:
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            assert len(rows) == 1, f"Table {table} not created"

    def test_register_agent(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        engine.register_agent("test_agent", mock_agent)
        assert "test_agent" in engine._agents

    def test_execute_simple_pipeline(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        mock_agent.do_thing.return_value = {"done": True}
        engine.register_agent("agent_a", mock_agent)

        p = Pipeline("test").step("s1", agent="agent_a", action="do_thing")
        result = engine.execute(p)

        assert result["status"] == "completed"
        assert result["steps_completed"] == 1
        assert result["steps_failed"] == 0
        assert result["run_id"]

    def test_execute_with_context(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        mock_agent.do_thing.return_value = {"value": 42}
        engine.register_agent("a", mock_agent)

        p = Pipeline("test").step("s1", agent="a", action="do_thing")
        result = engine.execute(p, context={"key": "val"})

        assert result["status"] == "completed"
        mock_agent.do_thing.assert_called_once()

    def test_execute_missing_agent(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        p = Pipeline("test").step("s1", agent="nonexistent", action="do_thing")
        result = engine.execute(p)
        assert result["status"] == "failed"

    def test_execute_chain(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_a = MagicMock()
        mock_a.step1.return_value = {"x": 1}
        mock_b = MagicMock()
        mock_b.step2.return_value = {"y": 2}
        engine.register_agent("a", mock_a)
        engine.register_agent("b", mock_b)

        p = (Pipeline("test")
             .step("s1", agent="a", action="step1")
             .step("s2", agent="b", action="step2", depends_on=["s1"]))
        result = engine.execute(p)

        assert result["status"] == "completed"
        assert result["steps_completed"] == 2

    def test_execute_parallel(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_a = MagicMock()
        mock_a.do_a.return_value = {"a": True}
        mock_b = MagicMock()
        mock_b.do_b.return_value = {"b": True}
        engine.register_agent("a", mock_a)
        engine.register_agent("b", mock_b)

        p = Pipeline("test").parallel("fan", steps=[
            Step("sa", agent="a", action="do_a"),
            Step("sb", agent="b", action="do_b"),
        ])
        result = engine.execute(p)
        assert result["status"] == "completed"

    def test_execute_conditional_true(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)

        p = Pipeline("test").conditional(
            "check", condition=lambda ctx: ctx.get("flag") is True,
            if_true="go", if_false="stop"
        )
        result = engine.execute(p, context={"flag": True})
        assert result["status"] == "completed"
        assert result["results"]["check"]["output"]["branch"] == "go"

    def test_execute_conditional_false(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)

        p = Pipeline("test").conditional(
            "check", condition=lambda ctx: ctx.get("flag") is True,
            if_true="go", if_false="stop"
        )
        result = engine.execute(p, context={"flag": False})
        assert result["results"]["check"]["output"]["branch"] == "stop"

    def test_execute_wait(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        p = Pipeline("test").wait("pause", seconds=0)
        result = engine.execute(p)
        assert result["status"] == "completed"

    def test_retry_on_failure(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        # Fail twice, succeed on third
        mock_agent.flaky.side_effect = [Exception("fail1"), Exception("fail2"), {"ok": True}]
        engine.register_agent("a", mock_agent)

        p = Pipeline("test").step("s1", agent="a", action="flaky", max_retries=3)
        result = engine.execute(p)
        assert result["status"] == "completed"
        assert result["results"]["s1"]["attempts"] == 3

    def test_dead_letter_on_permanent_failure(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        mock_agent.always_fail.side_effect = Exception("permanent error")
        engine.register_agent("a", mock_agent)

        p = Pipeline("test").step("s1", agent="a", action="always_fail", max_retries=1)
        result = engine.execute(p)
        assert result["status"] == "failed"

        dead_letters = engine.get_dead_letters()
        assert len(dead_letters) == 1
        assert "permanent error" in dead_letters[0]["error"]

    def test_get_run(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        mock_agent.do.return_value = {}
        engine.register_agent("a", mock_agent)

        p = Pipeline("test").step("s1", agent="a", action="do")
        result = engine.execute(p)

        run = engine.get_run(result["run_id"])
        assert run is not None
        assert run["pipeline_name"] == "test"
        assert run["status"] == "completed"

    def test_get_run_steps(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        mock_agent.do.return_value = {}
        engine.register_agent("a", mock_agent)

        p = (Pipeline("test")
             .step("s1", agent="a", action="do")
             .step("s2", agent="a", action="do", depends_on=["s1"]))
        result = engine.execute(p)

        steps = engine.get_run_steps(result["run_id"])
        assert len(steps) >= 2

    def test_pipeline_stats(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        mock_agent.do.return_value = {}
        engine.register_agent("a", mock_agent)

        p = Pipeline("stats_test").step("s1", agent="a", action="do")
        engine.execute(p)
        engine.execute(p)

        stats = engine.get_pipeline_stats("stats_test")
        assert stats["total_runs"] == 2
        assert stats["success_rate"] == 100.0

    def test_resolve_dead_letter(self, config, db, mock_llm):
        engine = WorkflowEngine(config, db, mock_llm)
        mock_agent = MagicMock()
        mock_agent.fail.side_effect = Exception("error")
        engine.register_agent("a", mock_agent)

        p = Pipeline("test").step("s1", agent="a", action="fail", max_retries=1)
        engine.execute(p)

        dead_letters = engine.get_dead_letters()
        assert len(dead_letters) == 1

        engine.resolve_dead_letter(dead_letters[0]["id"])
        assert len(engine.get_dead_letters()) == 0


# ── Task Router ───────────────────────────────────────────────


class TestTaskRouter:
    def test_schema_created(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        for table in ["task_queue", "agent_capabilities"]:
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            assert len(rows) == 1

    def test_capabilities_seeded(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        rows = db.execute("SELECT COUNT(*) as c FROM agent_capabilities")
        assert rows[0]["c"] > 0

    def test_classify_marketing_task(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        assert router._classify_task("Promote our product on social media") == "marketing"

    def test_classify_research_task(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        assert router._classify_task("Research market trends") == "research"

    def test_classify_design_task(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        assert router._classify_task("Design an ebook template") == "design"

    def test_classify_finance_task(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        assert router._classify_task("Analyze budget and ROI") == "finance"

    def test_route_research_task_to_agent(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        result = router.route("Research market trends", task_type="research")
        assert result["routed_to"] is not None
        assert result["task_type"] == "research"

    def test_route_auto_classify(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        result = router.route("Research market trends for digital products")
        assert result["task_type"] == "research"
        assert result["routed_to"] is not None

    def test_update_performance(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        # Get initial state
        rows = db.execute(
            "SELECT * FROM agent_capabilities WHERE agent_name = 'digital_products' "
            "AND capability = 'ebooks'"
        )
        initial_tasks = rows[0]["tasks_completed"]

        router.update_performance("digital_products", "ebooks", True, 1000)

        rows = db.execute(
            "SELECT * FROM agent_capabilities WHERE agent_name = 'digital_products' "
            "AND capability = 'ebooks'"
        )
        assert rows[0]["tasks_completed"] == initial_tasks + 1

    def test_get_queue_empty(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        assert router.get_queue() == []

    def test_get_agent_stats(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        stats = router.get_agent_stats()
        assert len(stats) > 0
        agent_names = {s["agent_name"] for s in stats}
        assert "digital_products" in agent_names

    def test_routing_stats_empty(self, config, db, mock_llm):
        router = TaskRouter(config, db, mock_llm)
        stats = router.get_routing_stats()
        assert stats == {}


# ── Pipeline Definitions ──────────────────────────────────────


class TestPipelineDefinitions:
    def test_all_pipelines_valid(self):
        for name, factory in PIPELINE_REGISTRY.items():
            p = factory()
            assert isinstance(p, Pipeline)
            assert p.name
            assert len(p.steps) > 0

    def test_digital_products_pipeline(self):
        p = digital_products_pipeline()
        assert p.name == "digital_products"
        step_names = [s.name for s in p.steps]
        assert "research" in step_names
        assert "create" in step_names
        assert "review" in step_names
        assert "fact_check" in step_names
        assert "humanize" in step_names
        assert "list_product" in step_names

    def test_get_pipeline_existing(self):
        p = get_pipeline("digital_products")
        assert p is not None
        assert p.name == "digital_products"

    def test_get_pipeline_nonexistent(self):
        assert get_pipeline("nonexistent") is None

    def test_list_pipelines(self):
        pipelines = list_pipelines()
        assert len(pipelines) == len(PIPELINE_REGISTRY)
        names = [p["name"] for p in pipelines]
        assert "digital_products" in names

    def test_execution_orders_valid(self):
        """All pipelines should produce valid execution orders."""
        for name, factory in PIPELINE_REGISTRY.items():
            p = factory()
            order = p.get_execution_order()
            assert len(order) > 0, f"Pipeline '{name}' has empty execution order"

    def test_pipeline_descriptions(self):
        for name, factory in PIPELINE_REGISTRY.items():
            p = factory()
            assert p.description, f"Pipeline '{name}' has no description"


class TestDefaultCapabilities:
    def test_all_agents_have_capabilities(self):
        assert len(DEFAULT_CAPABILITIES) >= 1

    def test_no_empty_capabilities(self):
        for agent, caps in DEFAULT_CAPABILITIES.items():
            assert len(caps) > 0, f"Agent '{agent}' has no capabilities"

    def test_task_types_have_capabilities(self):
        for task_type, caps in TASK_TYPE_CAPABILITIES.items():
            assert len(caps) > 0, f"Task type '{task_type}' has no capabilities"

    def test_all_task_types_routable(self):
        """Every task type should have at least one agent that can handle it."""
        all_agent_caps = set()
        for caps in DEFAULT_CAPABILITIES.values():
            all_agent_caps.update(caps)

        for task_type, needed_caps in TASK_TYPE_CAPABILITIES.items():
            overlap = set(needed_caps) & all_agent_caps
            assert overlap, f"Task type '{task_type}' has no agent with matching capabilities"
