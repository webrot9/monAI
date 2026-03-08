"""Autonomous task executor — gives monAI hands to act in the world.

This is the core capability layer. The executor can:
- Browse the web (navigate, fill forms, click, screenshot)
- Run shell commands
- Read/write files
- Make HTTP API calls
- Reason about what to do next using LLM

Think of it as AutoGPT's action loop: Think → Plan → Act → Observe → Repeat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import httpx

from monai.agents.ethics import CORE_DIRECTIVES, is_action_blocked, requires_risk_check
from monai.config import Config
from monai.db.database import Database
from monai.utils.browser import Browser
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

# Tools the executor can use — described for the LLM
TOOL_DESCRIPTIONS = """
Available tools:
1. browse(url) — Navigate to a URL and return page content + interactive elements
2. click(selector) — Click an element on the page
3. type(selector, text) — Type text into an input field
4. screenshot(name) — Take a screenshot of the current page
5. fill_form(fields) — Fill multiple form fields: {"selector": "value", ...}
6. submit(selector) — Submit a form
7. read_page() — Get full text content of the current page
8. http_get(url, headers) — Make an HTTP GET request
9. http_post(url, data, headers) — Make an HTTP POST request
10. shell(command) — Run a shell command and return output
11. write_file(path, content) — Write content to a file
12. read_file(path) — Read a file's content
13. write_code(spec, filename) — Generate a tested code module from a specification
14. run_tests(path) — Run tests for a code file
15. wait(seconds) — Wait for a specified time
16. done(result) — Signal task completion with a result
17. fail(reason) — Signal task failure with a reason
"""


class AutonomousExecutor:
    """Executes complex multi-step tasks autonomously using an LLM-driven action loop."""

    def __init__(self, config: Config, db: Database, llm: LLM,
                 max_steps: int = 50, headless: bool = True):
        self.config = config
        self.db = db
        self.llm = llm
        self.max_steps = max_steps
        self.browser = Browser(config, headless=headless)
        self.http_client = httpx.Client(timeout=30)
        self.action_history: list[dict] = []

    async def execute_task(self, task: str, context: str = "") -> dict[str, Any]:
        """Execute a task autonomously using think-act-observe loop.

        Args:
            task: Natural language description of what to accomplish
            context: Additional context (identity info, credentials, etc.)

        Returns:
            Result dict with status and details
        """
        logger.info(f"Starting autonomous task: {task[:100]}")
        self.action_history = []

        try:
            await self.browser.start()

            for step in range(self.max_steps):
                # THINK: Decide next action
                action = self._think(task, context, step)

                if not action:
                    return {"status": "error", "reason": "LLM returned no action"}

                tool = action.get("tool", "")
                args = action.get("args", {})

                logger.info(f"Step {step + 1}: {tool}({json.dumps(args)[:200]})")

                # ACT: Execute the action
                result = await self._act(tool, args)

                # OBSERVE: Record the result
                self.action_history.append({
                    "step": step + 1,
                    "tool": tool,
                    "args": args,
                    "result": str(result)[:1000],
                })

                # Check if task is done
                if tool == "done":
                    self._log_task(task, "completed", result)
                    return {"status": "completed", "result": result, "steps": step + 1}
                elif tool == "fail":
                    self._log_task(task, "failed", result)
                    return {"status": "failed", "reason": result, "steps": step + 1}

            self._log_task(task, "max_steps_reached", "")
            return {"status": "max_steps_reached", "steps": self.max_steps,
                    "history": self.action_history}

        finally:
            await self.browser.stop()

    def _think(self, task: str, context: str, step: int) -> dict[str, Any]:
        """Use LLM to decide the next action."""
        history_summary = ""
        if self.action_history:
            recent = self.action_history[-10:]  # Last 10 actions for context
            history_summary = "\n".join(
                f"Step {a['step']}: {a['tool']}({json.dumps(a['args'])[:150]}) → {a['result'][:200]}"
                for a in recent
            )

        prompt = (
            f"TASK: {task}\n\n"
            f"CONTEXT: {context}\n\n"
            f"STEP: {step + 1}/{self.max_steps}\n\n"
            f"PREVIOUS ACTIONS:\n{history_summary or 'None yet'}\n\n"
            f"{TOOL_DESCRIPTIONS}\n\n"
            "Decide the next action. Return JSON: "
            '{"reasoning": "why this action", "tool": "tool_name", "args": {...}}\n\n'
            "Be efficient. Don't repeat failed actions. If stuck, try a different approach."
        )

        response = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    f"{CORE_DIRECTIVES}\n\n"
                    "You are an autonomous AI executor. You complete tasks by using tools. "
                    "Think step by step. Be resourceful and creative. "
                    "When registering on platforms, use the provided identity info. "
                    "Always check results before proceeding. Take screenshots when unsure. "
                    "When writing code, use write_code tool — it generates AND tests code. "
                    "NEVER produce sloppy work. Everything must be production quality."
                )},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        return response

    async def _act(self, tool: str, args: dict) -> Any:
        """Execute a tool action with ethics guardrails."""
        # Guardrail: block dangerous actions
        action_str = f"{tool} {json.dumps(args)}"
        if is_action_blocked(action_str):
            return "BLOCKED by ethics guardrails: dangerous action"

        # Guardrail: flag risky actions
        if requires_risk_check(action_str):
            self._log_task(f"RISK FLAG: {tool}", "risk_check", action_str)

        try:
            if tool == "browse":
                await self.browser.navigate(args.get("url", ""))
                return await self.browser.get_page_info()

            elif tool == "click":
                await self.browser.click(args.get("selector", ""))
                await asyncio.sleep(1)  # Wait for page reaction
                return await self.browser.get_page_info()

            elif tool == "type":
                await self.browser.type_text(args.get("selector", ""), args.get("text", ""))
                return "typed"

            elif tool == "screenshot":
                path = await self.browser.screenshot(args.get("name", "page"))
                return f"Screenshot saved: {path}"

            elif tool == "fill_form":
                fields = args.get("fields", {})
                await self.browser.fill_form(fields)
                return f"Filled {len(fields)} fields"

            elif tool == "submit":
                await self.browser.submit_form(args.get("selector", "form"))
                await asyncio.sleep(2)
                return await self.browser.get_page_info()

            elif tool == "read_page":
                return await self.browser.get_text()

            elif tool == "http_get":
                resp = self.http_client.get(
                    args.get("url", ""),
                    headers=args.get("headers", {}),
                )
                return {"status": resp.status_code, "body": resp.text[:2000]}

            elif tool == "http_post":
                resp = self.http_client.post(
                    args.get("url", ""),
                    json=args.get("data", {}),
                    headers=args.get("headers", {}),
                )
                return {"status": resp.status_code, "body": resp.text[:2000]}

            elif tool == "shell":
                cmd = args.get("command", "")
                # Safety: block dangerous commands
                blocked = ["rm -rf /", "mkfs", "dd if=", ":(){", "fork bomb"]
                if any(b in cmd.lower() for b in blocked):
                    return "BLOCKED: dangerous command"
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=60,
                )
                return {"stdout": result.stdout[:2000], "stderr": result.stderr[:500],
                        "returncode": result.returncode}

            elif tool == "write_file":
                path = Path(args.get("path", ""))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(args.get("content", ""))
                return f"Written: {path}"

            elif tool == "read_file":
                path = Path(args.get("path", ""))
                if path.exists():
                    return path.read_text()[:5000]
                return "File not found"

            elif tool == "write_code":
                from monai.agents.coder import Coder
                coder = Coder(self.config, self.db, self.llm)
                result = coder.generate_module(
                    spec=args.get("spec", ""),
                    project_dir=args.get("project_dir"),
                    language=args.get("language", "python"),
                )
                return result

            elif tool == "run_tests":
                test_path = args.get("path", "")
                result = subprocess.run(
                    ["python", "-m", "pytest", test_path, "-v", "--tb=short"],
                    capture_output=True, text=True, timeout=60,
                )
                return {
                    "passed": result.returncode == 0,
                    "output": result.stdout + result.stderr,
                }

            elif tool == "wait":
                seconds = min(args.get("seconds", 1), 30)  # Cap at 30s
                await asyncio.sleep(seconds)
                return f"Waited {seconds}s"

            elif tool == "done":
                return args.get("result", "Task completed")

            elif tool == "fail":
                return args.get("reason", "Task failed")

            else:
                return f"Unknown tool: {tool}"

        except Exception as e:
            logger.error(f"Tool {tool} failed: {e}")
            return f"ERROR: {e}"

    def _log_task(self, task: str, status: str, result: Any):
        self.db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details, result) VALUES (?, ?, ?, ?)",
            ("executor", f"task_{status}", task[:500], str(result)[:1000]),
        )
