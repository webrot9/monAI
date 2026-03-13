"""Autonomous task executor — gives monAI hands to act in the world.

This is the core capability layer. The executor can:
- Browse the web (navigate, fill forms, click, screenshot)
- Run shell commands
- Read/write files
- Make HTTP API calls
- Coordinate with other agents (email, phone, verification)
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
from monai.agents.playbooks import detect_platforms_in_task, get_playbook_prompt
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

BROWSER TOOLS:
1. browse(url) — Navigate to a URL and return page content + interactive elements
2. click(selector) — Click an element on the page. Use CSS selectors.
3. type(selector, text) — Type text into an input field
4. screenshot(name) — Take a screenshot of the current page
5. fill_form(fields) — Fill multiple form fields at once: {"selector": "value", ...}
   PREFER this over individual type() calls for forms.
6. submit(selector) — Submit a form (default selector: "form")
7. read_page() — Get full text content of the current page. Use this to understand
   what's on the page before acting.

API TOOLS:
8. http_get(url, headers) — Make an HTTP GET request
9. http_post(url, data, headers) — Make an HTTP POST request

AGENT COORDINATION TOOLS:
10. create_temp_email() — Create a disposable email address instantly (via mail.tm API).
    Returns {"address": "...", "password": "..."}. Use this for signups.
11. check_email_verification(email, platform) — Check if a verification email arrived.
    Returns the verification code or link if found.
12. get_phone(platform) — Get a virtual phone number for SMS verification.
    Returns {"phone_number": "...", "phone_id": N}
13. check_phone_code(phone_id) — Check if an SMS verification code was received.
    Returns the code if found.

FILE & CODE TOOLS:
14. shell(command) — Run a shell command (only whitelisted commands)
15. write_file(path, content) — Write content to a file
16. read_file(path) — Read a file's content
17. write_code(spec, filename) — Generate a tested code module from a specification
18. run_tests(path) — Run tests for a code file

CONTROL TOOLS:
19. wait(seconds) — Wait for a specified time (max 30s)
20. done(result) — Signal task completion with a result summary
21. fail(reason) — Signal task failure with a reason
"""

# Anti-patterns the LLM must avoid
ANTI_PATTERNS = """
CRITICAL RULES — AVOID THESE MISTAKES:
- Do NOT navigate to made-up URLs (api.example.com, api.businessinfo.com don't exist)
- Do NOT try the same failed action twice — if it failed, change your approach
- Do NOT use screenshot/read_page as busywork — only when you need to see the page
- Do NOT try to register on Instagram/Facebook for a business directory — use actual
  freelance platforms (Upwork, Fiverr, Freelancer)
- Do NOT try random API endpoints that you haven't verified exist
- ALWAYS use read_page() after navigating to understand the page before clicking
- ALWAYS use create_temp_email() for signups — don't try to create Gmail/Yahoo manually
- ALWAYS use fill_form() instead of many individual type() calls
- If you get "BLOCKED" or "ERROR" more than twice for the same action, STOP and try
  a completely different approach or call fail() with a clear reason
- If a site requires phone verification, use get_phone() — don't skip it
"""


class AutonomousExecutor:
    """Executes complex multi-step tasks autonomously using an LLM-driven action loop."""

    # Circuit breaker thresholds
    MAX_CONSECUTIVE_FAILURES = 5  # Abort after N consecutive tool errors
    MAX_FAILURE_RATE = 0.6  # Abort if >60% of steps have failed (after 8+ steps)
    MIN_STEPS_FOR_RATE_CHECK = 8  # Don't check failure rate until this many steps

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
        # Lazy-loaded agent collaborators
        self._email_verifier = None
        self._phone_provisioner = None

    def _get_email_verifier(self):
        if self._email_verifier is None:
            from monai.agents.email_verifier import EmailVerifier
            self._email_verifier = EmailVerifier(self.config, self.db)
        return self._email_verifier

    def _get_phone_provisioner(self):
        if self._phone_provisioner is None:
            from monai.agents.phone_provisioner import PhoneProvisioner
            self._phone_provisioner = PhoneProvisioner(self.config, self.db, self.llm)
        return self._phone_provisioner

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

        # Detect platforms and inject playbook knowledge
        playbook_prompts = self._build_playbook_context(task)

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

                # Circuit breaker: consecutive failures
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

                # Circuit breaker: high total failure rate
                if (step >= self.MIN_STEPS_FOR_RATE_CHECK
                        and total_failures / step > self.MAX_FAILURE_RATE):
                    reason = (
                        f"Circuit breaker: {total_failures}/{step} steps failed "
                        f"({total_failures/step:.0%}) — task is not making progress"
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
                action = self._think(task, context, step, playbook_prompts)

                if not action:
                    return {"status": "error", "reason": "LLM returned no action"}

                tool = action.get("tool", "")
                args = action.get("args", {})

                logger.info(f"Step {step + 1}: {tool}({json.dumps(args)[:200]})")

                # ACT: Execute the action
                result = await self._act(tool, args)
                result_str = str(result)

                # Track failures for circuit breaker
                is_failure = self._is_failure_result(result_str)
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
                    "result": result_str[:2000],
                    "failed": is_failure,
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

    @staticmethod
    def _is_failure_result(result_str: str) -> bool:
        """Determine if a tool result indicates failure."""
        return (
            result_str.startswith("ERROR:")
            or result_str.startswith("BLOCKED")
            or "Timeout" in result_str
            or "timed out" in result_str.lower()
        )

    def _build_playbook_context(self, task: str) -> str:
        """Detect platforms in the task and build playbook context."""
        platforms = detect_platforms_in_task(task)
        if not platforms:
            return ""
        parts = []
        for platform in platforms:
            prompt = get_playbook_prompt(platform)
            if prompt:
                parts.append(prompt)
        return "\n".join(parts)

    def _format_history(self, max_actions: int = 20) -> str:
        """Format action history for the LLM with richer context."""
        if not self.action_history:
            return "None yet"

        recent = self.action_history[-max_actions:]
        lines = []
        for a in recent:
            status = "FAILED" if a.get("failed") else "OK"
            # Show more of the result for failures (to help LLM understand what went wrong)
            max_result_len = 500 if a.get("failed") else 300
            result_preview = a["result"][:max_result_len]
            args_str = json.dumps(a["args"])[:300]
            lines.append(
                f"Step {a['step']} [{status}]: {a['tool']}({args_str}) → {result_preview}"
            )

        # Add summary stats
        total = len(self.action_history)
        failed = sum(1 for a in self.action_history if a.get("failed"))
        lines.append(f"\n--- Stats: {total} steps total, {failed} failed ---")

        # Highlight repeated failures (anti-loop detection)
        failed_actions = [
            f"{a['tool']}({json.dumps(a['args'])[:100]})"
            for a in self.action_history if a.get("failed")
        ]
        if failed_actions:
            from collections import Counter
            repeats = Counter(failed_actions)
            repeated = [f"  {action} (failed {count}x)" for action, count in repeats.items() if count >= 2]
            if repeated:
                lines.append("REPEATED FAILURES (do NOT retry these):")
                lines.extend(repeated)

        return "\n".join(lines)

    def _think(self, task: str, context: str, step: int,
               playbook_context: str = "") -> dict[str, Any]:
        """Use LLM to decide the next action."""
        history_summary = self._format_history()

        prompt_parts = [
            f"TASK: {task}",
            f"\nCONTEXT: {context}" if context else "",
            f"\nSTEP: {step + 1}/{self.max_steps}",
            f"\nPREVIOUS ACTIONS:\n{history_summary}",
        ]

        # Inject playbook knowledge if available
        if playbook_context:
            prompt_parts.append(f"\n{playbook_context}")

        prompt_parts.extend([
            f"\n{TOOL_DESCRIPTIONS}",
            f"\n{ANTI_PATTERNS}",
            "\nDecide the next action. Return JSON: "
            '{"reasoning": "why this action", "tool": "tool_name", "args": {...}}',
        ])

        prompt = "\n".join(prompt_parts)

        response = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    f"{CORE_DIRECTIVES}\n\n"
                    "You are an autonomous AI executor. You complete tasks by using tools.\n"
                    "STRATEGY:\n"
                    "1. Read the playbook steps if provided — follow them in order.\n"
                    "2. After navigating to a page, ALWAYS use read_page() to understand "
                    "what's on the page before clicking or filling forms.\n"
                    "3. Use fill_form() to fill multiple fields at once — it's more reliable "
                    "than individual type() calls.\n"
                    "4. For signups that need email, use create_temp_email() first.\n"
                    "5. If a site asks for phone verification, use get_phone() and check_phone_code().\n"
                    "6. After submitting a form, use read_page() to check the result.\n"
                    "7. If something fails, read the error carefully and try a DIFFERENT approach.\n"
                    "8. Call done() with a summary as soon as the task is complete.\n"
                    "9. Call fail() with a clear reason if the task cannot be completed.\n"
                    "10. NEVER waste steps on actions that already failed.\n"
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

            # ── Agent Coordination Tools ─────────────────────────────

            elif tool == "create_temp_email":
                verifier = self._get_email_verifier()
                return verifier.create_temp_email()

            elif tool == "check_email_verification":
                verifier = self._get_email_verifier()
                email_addr = args.get("email", "")
                platform = args.get("platform", "unknown")
                return verifier.wait_for_verification(
                    email_addr, platform, timeout=60, poll_interval=5,
                )

            elif tool == "get_phone":
                phone_prov = self._get_phone_provisioner()
                platform = args.get("platform", "unknown")
                return phone_prov.get_number(
                    platform=platform,
                    requesting_agent="executor",
                )

            elif tool == "check_phone_code":
                phone_prov = self._get_phone_provisioner()
                phone_id = args.get("phone_id", 0)
                return phone_prov.wait_for_code(phone_id, timeout=60)

            # ── API Tools ────────────────────────────────────────────

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

            # ── File & Code Tools ────────────────────────────────────

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
