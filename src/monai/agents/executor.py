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
from monai.agents.memory import SharedMemory
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

    # Circuit breaker: abort after this many consecutive tool failures
    MAX_CONSECUTIVE_FAILURES = 5
    # Also abort when total failure ratio exceeds this threshold
    # (prevents LLM from gaming the breaker with read_page/screenshot between fails)
    MAX_FAILURE_RATIO = 0.7  # 70% of steps failing = abort
    MIN_STEPS_FOR_RATIO = 6  # don't apply ratio check before this many steps

    def __init__(self, config: Config, db: Database, llm: LLM,
                 max_steps: int = 30, headless: bool = True,
                 timeout_seconds: int = 3600):
        self.config = config
        self.db = db
        self.llm = llm
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self._anonymizer = get_anonymizer(config)
        # HTTP client routed through proxy — no direct connections
        self.http_client = self._anonymizer.create_http_client(timeout=30)
        self.action_history: list[dict] = []
        self.memory = SharedMemory(db)

        # Use BrowserLearner (adaptive) instead of raw Browser.
        # Falls back to raw Browser if BrowserLearner can't be created.
        try:
            from monai.agents.browser_learner import BrowserLearner
            self._learner = BrowserLearner(config, db, llm, headless=headless)
            self.browser = self._learner.browser
        except Exception:
            self._learner = None
            self.browser = Browser(config, headless=headless)

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
        total_failures = 0

        try:
            if self._learner:
                await self._learner.start()
            else:
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

                # Circuit breaker: abort when failure ratio is too high
                # (prevents LLM from gaming breaker with read_page/screenshot
                # between actual failures)
                if (step >= self.MIN_STEPS_FOR_RATIO
                        and total_failures / step >= self.MAX_FAILURE_RATIO):
                    reason = (
                        f"Circuit breaker: {total_failures}/{step} steps failed "
                        f"({total_failures/step:.0%}) — aborting task"
                    )
                    logger.warning(reason)
                    self._log_task(task, "circuit_breaker", reason)
                    return {
                        "status": "failed",
                        "reason": reason,
                        "steps": step,
                        "history": self.action_history,
                    }

                # Mid-task reflection: when accumulating failures, pause
                # and ask LLM to analyze what's wrong before continuing
                if (total_failures >= 3 and total_failures % 3 == 0
                        and step > 0):
                    reflection = self._reflect_on_failures(task, context)
                    if reflection:
                        # Inject reflection into context for next _think call
                        context = f"{context}\n\nREFLECTION: {reflection}"

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
                    total_failures += 1
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
            # Post-task learning: analyze what happened and store lessons
            try:
                self._post_task_learn(task, self.action_history)
            except Exception as e:
                logger.debug(f"Post-task learning error (non-fatal): {e}")

            try:
                if self._learner:
                    await self._learner.stop()
                else:
                    await self.browser.stop()
            except Exception as e:
                logger.warning(f"Error stopping browser: {e}")
            try:
                if hasattr(self.http_client, 'close'):
                    self.http_client.close()
            except Exception as e:
                logger.warning(f"Error closing HTTP client: {e}")

    def _think(self, task: str, context: str, step: int) -> dict[str, Any]:
        """Use LLM to decide the next action, enriched with learned context."""
        history_summary = ""
        if self.action_history:
            recent = self.action_history[-10:]  # Last 10 actions for context
            history_summary = "\n".join(
                f"Step {a['step']}: {a['tool']}({json.dumps(a['args'])[:150]}) → {a['result'][:200]}"
                for a in recent
            )

        # Inject learned context: domain playbooks, past failures, lessons
        learned_context = self._get_learned_context(task)

        prompt = (
            f"TASK: {task}\n\n"
            f"CONTEXT: {context}\n\n"
            f"STEP: {step + 1}/{self.max_steps}\n\n"
            f"PREVIOUS ACTIONS:\n{history_summary or 'None yet'}\n\n"
            f"{learned_context}"
            f"{TOOL_DESCRIPTIONS}\n\n"
            "Decide the next action. Return JSON: "
            '{"reasoning": "why this action", "tool": "tool_name", "args": {...}}\n\n'
            "CRITICAL RULES:\n"
            "- NEVER repeat a failed action with the same arguments\n"
            "- If a domain is blocked, try a DIFFERENT domain or approach\n"
            "- If 3+ actions have failed, call done() or fail() instead of burning more steps\n"
            "- read_page/screenshot do NOT count as progress — only use them when genuinely needed\n"
            "- Be efficient. Change strategy when things aren't working."
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
                    "NEVER produce sloppy work. Everything must be production quality.\n\n"
                    "LEARN FROM FAILURES: When an action fails, analyze WHY and try a "
                    "fundamentally different approach. Do NOT just retry the same thing."
                )},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        return response

    def _get_learned_context(self, task: str) -> str:
        """Pull relevant learned context for the current task."""
        parts = []

        # 1. Domain playbooks — if we know patterns for target sites
        if self._learner:
            # Extract domain hints from the task
            import re
            urls = re.findall(r'https?://([^\s/]+)', task)
            for domain in urls[:3]:  # Cap at 3 domains
                playbook = self._learner.get_playbook(domain)
                if playbook:
                    parts.append(
                        f"KNOWN PATTERNS for {domain}: "
                        f"Success rate: {playbook.get('success_rate', 0):.0%}, "
                        f"Anti-bot: {playbook.get('anti_bot_measures', 'unknown')}, "
                        f"Selectors: {playbook.get('known_selectors', '{}')}"
                    )
                # Check past failure rates
                rates = self._learner.get_success_rate(domain)
                if rates:
                    rate_summary = ", ".join(
                        f"{k}: {v.get('rate', 0)}%"
                        for k, v in rates.items()
                    )
                    parts.append(f"PAST SUCCESS RATES on {domain}: {rate_summary}")

        # 2. Lessons from shared memory — what other agents learned
        try:
            lessons = self.memory.get_lessons("executor", include_shared=True)
            if lessons:
                recent_lessons = lessons[:5]
                lesson_text = "\n".join(
                    f"- {l['lesson']}" + (f" RULE: {l['rule']}" if l.get('rule') else "")
                    for l in recent_lessons
                )
                parts.append(f"LESSONS FROM PAST TASKS:\n{lesson_text}")
        except Exception:
            pass

        # 3. Blocked domains — tell the LLM what's already known to be blocked
        try:
            fallback = self._anonymizer.fallback_chain
            status = fallback.get_domain_status()
            blocked = status.get("blocked", {})
            if blocked:
                blocked_text = ", ".join(
                    f"{d} ({', '.join(types)})" for d, types in blocked.items()
                )
                parts.append(f"CURRENTLY BLOCKED DOMAINS: {blocked_text}")
        except Exception:
            pass

        if parts:
            return "LEARNED CONTEXT:\n" + "\n".join(parts) + "\n\n"
        return ""

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
                if self._learner:
                    result = await self._learner.navigate(args.get("url", ""))
                    if not result.get("success"):
                        failure = result.get("failure", "unknown")
                        error = result.get("error", "")
                        return f"ERROR: Navigation failed ({failure}): {error}"
                    return result.get("page_info", await self.browser.get_page_info())
                else:
                    await self.browser.navigate(args.get("url", ""))
                    return await self.browser.get_page_info()

            elif tool == "click":
                if self._learner:
                    url = ""
                    try:
                        page = await self.browser._get_page()
                        url = page.url
                    except Exception:
                        pass
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc if url else ""
                    result = await self._learner.smart_click(
                        args.get("selector", ""),
                        domain=domain,
                        fallback_text=args.get("text", ""),
                    )
                    if not result.get("success"):
                        return f"ERROR: Click failed: {result.get('error', 'unknown')}"
                    await asyncio.sleep(1)
                    return await self.browser.get_page_info()
                else:
                    await self.browser.click(args.get("selector", ""))
                    await asyncio.sleep(1)
                    return await self.browser.get_page_info()

            elif tool == "type":
                if self._learner:
                    url = ""
                    try:
                        page = await self.browser._get_page()
                        url = page.url
                    except Exception:
                        pass
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc if url else ""
                    result = await self._learner.smart_type(
                        args.get("selector", ""),
                        args.get("text", ""),
                        domain=domain,
                        human_like=True,
                    )
                    if not result.get("success"):
                        return f"ERROR: Type failed: {result.get('error', 'unknown')}"
                    return "typed"
                else:
                    await self.browser.type_text(args.get("selector", ""), args.get("text", ""))
                    return "typed"

            elif tool == "screenshot":
                path = await self.browser.screenshot(args.get("name", "page"))
                return f"Screenshot saved: {path}"

            elif tool == "fill_form":
                fields = args.get("fields", {})
                if self._learner:
                    url = ""
                    try:
                        page = await self.browser._get_page()
                        url = page.url
                    except Exception:
                        pass
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc if url else ""
                    result = await self._learner.smart_fill_form(fields, domain=domain)
                    if not result.get("success"):
                        failed = [k for k, v in result.get("fields", {}).items()
                                  if not v.get("success")]
                        return f"ERROR: Fill form failed on: {', '.join(failed)}"
                    return f"Filled {len(fields)} fields"
                else:
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

    def _reflect_on_failures(self, task: str, context: str) -> str | None:
        """When hitting repeated failures, pause and analyze what's going wrong.

        Uses a cheap LLM call to reason about the pattern of failures and
        suggest a fundamentally different approach.
        """
        if not self.action_history:
            return None

        failures = [
            a for a in self.action_history
            if a["result"].startswith("ERROR:") or a["result"].startswith("BLOCKED")
        ]
        if len(failures) < 3:
            return None

        failure_summary = "\n".join(
            f"- {a['tool']}({json.dumps(a['args'])[:100]}) → {a['result'][:150]}"
            for a in failures[-5:]
        )

        try:
            reflection = self.llm.quick(
                f"TASK: {task}\n\n"
                f"REPEATED FAILURES ({len(failures)} total):\n{failure_summary}\n\n"
                "Analyze the pattern. Why do these keep failing? "
                "What fundamentally different approach should be tried? "
                "Be specific and actionable. Max 3 sentences.",
                system="You analyze task execution failures and suggest alternative strategies.",
            )
            logger.info(f"Mid-task reflection: {reflection[:200]}")
            return reflection
        except Exception as e:
            logger.debug(f"Reflection failed (non-fatal): {e}")
            return None

    def _post_task_learn(self, task: str, history: list[dict]) -> None:
        """After task completion/failure, analyze what happened and store lessons.

        This is how the executor builds knowledge across tasks.
        """
        if not history or len(history) < 3:
            return

        failures = [a for a in history if a["result"].startswith("ERROR:")
                     or a["result"].startswith("BLOCKED")]
        successes = [a for a in history if not a["result"].startswith("ERROR:")
                      and not a["result"].startswith("BLOCKED")]
        failure_rate = len(failures) / len(history) if history else 0

        # Only learn from tasks with significant failures
        if failure_rate < 0.3:
            return

        # Extract domains from failures to learn site-specific patterns
        failed_domains = set()
        for a in failures:
            url = a.get("args", {}).get("url", "")
            if url:
                from urllib.parse import urlparse
                domain = urlparse(url).netloc
                if domain:
                    failed_domains.add(domain)

        # Build failure summary
        failure_types = {}
        for a in failures:
            result = a["result"][:100]
            failure_types[result] = failure_types.get(result, 0) + 1

        top_failures = sorted(failure_types.items(), key=lambda x: -x[1])[:3]
        failure_text = "; ".join(f"{msg} (x{n})" for msg, n in top_failures)

        # Use cheap LLM to extract a lesson
        try:
            lesson_response = self.llm.quick(
                f"Task: {task[:200]}\n"
                f"Steps: {len(history)}, Failures: {len(failures)} ({failure_rate:.0%})\n"
                f"Top failures: {failure_text}\n"
                f"Failed domains: {', '.join(failed_domains) or 'N/A'}\n\n"
                "Extract a CONCISE lesson (1 sentence) and a CONCRETE rule "
                "(1 sentence, actionable) to prevent this in future.\n"
                "Format: LESSON: ...\nRULE: ...",
                system="You analyze task execution outcomes and extract actionable lessons.",
            )

            # Parse response
            lesson = lesson_response.split("RULE:")[0].replace("LESSON:", "").strip()
            rule = lesson_response.split("RULE:")[-1].strip() if "RULE:" in lesson_response else ""

            if lesson:
                self.memory.record_lesson(
                    agent_name="executor",
                    category="pattern",
                    situation=f"Task: {task[:200]}. {len(failures)}/{len(history)} failed.",
                    lesson=lesson[:300],
                    rule=rule[:300],
                    severity="high" if failure_rate > 0.7 else "medium",
                )
                logger.info(f"Post-task lesson stored: {lesson[:100]}")
        except Exception as e:
            logger.debug(f"Post-task learning failed (non-fatal): {e}")

    def _log_task(self, task: str, status: str, result: Any):
        self.db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details, result) VALUES (?, ?, ?, ?)",
            ("executor", f"task_{status}", task[:500], str(result)[:1000]),
        )
