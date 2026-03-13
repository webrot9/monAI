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
import sys
import time
from pathlib import Path
from typing import Any

from monai.agents.ethics import CORE_DIRECTIVES, is_action_blocked, requires_risk_check
from monai.config import Config
from monai.db.database import Database
from monai.utils.browser import Browser
from monai.utils.llm import LLM
from monai.utils.privacy import get_anonymizer
from monai.utils.sandbox import is_path_allowed, safe_read, safe_write, sandbox_run

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

    # Circuit breaker: abort task after this many consecutive tool failures
    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, config: Config, db: Database, llm: LLM,
                 max_steps: int = 30, headless: bool = True,
                 timeout_seconds: int = 3600):
        self.config = config
        self.db = db
        self.llm = llm
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.browser = Browser(config, headless=headless)
        self._anonymizer = get_anonymizer(config)
        # HTTP client routed through proxy — no direct connections
        self.http_client = self._anonymizer.create_http_client(timeout=30)
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
        start_time = time.time()
        consecutive_failures = 0

        try:
            await self.browser.start()

            for step in range(self.max_steps):
                # Enforce time limit
                elapsed = time.time() - start_time
                if elapsed > self.timeout_seconds:
                    self._log_task(task, "timeout", f"Exceeded {self.timeout_seconds}s")
                    return {
                        "status": "timeout",
                        "steps": step,
                        "elapsed_seconds": int(elapsed),
                        "history": self.action_history,
                    }

                # Circuit breaker: abort after too many consecutive failures
                if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    reason = (
                        f"Circuit breaker: {consecutive_failures} consecutive tool "
                        f"failures — aborting task to stop wasting API calls"
                    )
                    logger.warning(reason)
                    self._log_task(task, "circuit_breaker", reason)
                    return {
                        "status": "failed",
                        "reason": reason,
                        "steps": step,
                        "history": self.action_history,
                    }

                # THINK: Decide next action
                action = self._think(task, context, step)

                if not action:
                    return {"status": "error", "reason": "LLM returned no action"}

                tool = action.get("tool", "")
                args = action.get("args", {})

                logger.info(f"Step {step + 1}: {tool}({json.dumps(args)[:200]})")

                # ACT: Execute the action
                result = await self._act(tool, args)
                result_str = str(result)

                # Track consecutive failures for circuit breaker
                is_failure = (
                    result_str.startswith("ERROR:")
                    or result_str.startswith("BLOCKED")
                    or "Timeout" in result_str
                    or "timed out" in result_str.lower()
                )
                if is_failure:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

                # OBSERVE: Record the result
                self.action_history.append({
                    "step": step + 1,
                    "tool": tool,
                    "args": args,
                    "result": result_str[:1000],
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
            try:
                await self.browser.stop()
            except Exception as e:
                logger.warning(f"Error stopping browser: {e}")
            try:
                if hasattr(self.http_client, 'close'):
                    self.http_client.close()
            except Exception as e:
                logger.warning(f"Error closing HTTP client: {e}")

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
                url = args.get("url", "")
                if not url.startswith(("http://", "https://")):
                    return "BLOCKED: only http/https URLs allowed"
                try:
                    self._anonymizer.maybe_rotate()
                    resp = self.http_client.get(
                        url, headers=args.get("headers", {}), timeout=30,
                    )
                    return {"status": resp.status_code, "body": resp.text[:2000]}
                except Exception as e:
                    return {"status": 0, "error": str(e)[:200]}

            elif tool == "http_post":
                url = args.get("url", "")
                if not url.startswith(("http://", "https://")):
                    return "BLOCKED: only http/https URLs allowed"
                try:
                    self._anonymizer.maybe_rotate()
                    resp = self.http_client.post(
                        url, json=args.get("data", {}),
                        headers=args.get("headers", {}), timeout=30,
                    )
                    return {"status": resp.status_code, "body": resp.text[:2000]}
                except Exception as e:
                    return {"status": 0, "error": str(e)[:200]}

            elif tool == "shell":
                cmd = args.get("command", "")
                if is_action_blocked(cmd):
                    return "BLOCKED: dangerous command"
                # Validate command against whitelist — no arbitrary shell execution
                from monai.agents.ethics import is_shell_command_allowed
                if not is_shell_command_allowed(cmd):
                    return "BLOCKED: command not in allowed list. Only safe commands permitted."
                # Parse and execute in OS-level sandbox (namespace isolation +
                # sanitized env + forced cwd + no shell=True)
                import shlex
                try:
                    cmd_parts = shlex.split(cmd)
                except ValueError as e:
                    return f"BLOCKED: invalid command syntax: {e}"
                return sandbox_run(cmd_parts)

            elif tool == "write_file":
                path = args.get("path", "")
                if not is_path_allowed(path):
                    return f"SANDBOX VIOLATION: Cannot write outside allowed directories"
                safe_write(path, args.get("content", ""))
                return f"Written: {path}"

            elif tool == "read_file":
                path = args.get("path", "")
                if not is_path_allowed(path):
                    return f"SANDBOX VIOLATION: Cannot read outside allowed directories"
                try:
                    return safe_read(path)[:5000]
                except FileNotFoundError:
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
                result = sandbox_run(
                    [sys.executable, "-m", "pytest", test_path, "-v", "--tb=short"],
                    timeout=120,
                )
                return {
                    "passed": result["returncode"] == 0,
                    "output": result["stdout"] + result["stderr"],
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
