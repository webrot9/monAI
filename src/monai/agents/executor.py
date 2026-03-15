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

from monai.agents.asset_aware import AssetManager
from monai.agents.ethics import CORE_DIRECTIVES, is_action_blocked, requires_risk_check
from monai.agents.memory import SharedMemory
from monai.agents.proof import ProofOfCompletion
from monai.config import Config
from monai.db.database import Database
from monai.utils.browser import Browser
from monai.utils.llm import LLM
from monai.utils.privacy import get_anonymizer
from monai.utils.sandbox import is_path_allowed, safe_read, safe_write, sandbox_run

logger = logging.getLogger(__name__)

# Built-in tools the executor can use — described for the LLM
BUILTIN_TOOL_DESCRIPTIONS = """
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
16. wait_for(selector, timeout) — Wait for an element to appear on the page (timeout in seconds, default 10)
17. create_tool(name, description, code) — Create a new reusable tool at runtime
18. run_page_script(script, args) — Execute custom JavaScript on the current page. Use this when standard fill_form/click/type fail. Write Playwright-compatible JS that interacts with the DOM directly. args is an optional JSON object passed to the script. The script runs as an async function body with access to `args`. Use document.querySelector, dispatchEvent, etc. For React/Vue apps, trigger proper input events. No network requests or cookie access allowed. Returns {success, result}.
19. done(result) — Signal task completion with a result
20. fail(reason) — Signal task failure with a reason
"""

# Backward-compat alias
TOOL_DESCRIPTIONS = BUILTIN_TOOL_DESCRIPTIONS


class AutonomousExecutor:
    """Executes complex multi-step tasks autonomously using an LLM-driven action loop."""

    # Circuit breaker: abort after this many consecutive tool failures
    MAX_CONSECUTIVE_FAILURES = 5
    # Also abort when total failure ratio exceeds this threshold
    # (prevents LLM from gaming the breaker with read_page/screenshot between fails)
    MAX_FAILURE_RATIO = 0.7  # 70% of steps failing = abort
    MIN_STEPS_FOR_RATIO = 6  # don't apply ratio check before this many steps

    # Class-level cancellation flag — set by the watchdog to stop all
    # in-flight executors when a cycle times out.
    _cycle_cancelled = False

    @classmethod
    def cancel_cycle(cls) -> None:
        """Signal all running executors to stop."""
        cls._cycle_cancelled = True

    @classmethod
    def reset_cycle(cls) -> None:
        """Clear the cancellation flag at the start of a new cycle."""
        cls._cycle_cancelled = False

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
        self._reflection_count = 0  # Cap reflections per task
        self.memory = SharedMemory(db)

        # Dynamic tool registry — agents can create tools at runtime
        self._custom_tools: dict[str, dict] = {}
        self._load_custom_tools()

        # Proof-of-completion: verify claims before accepting "done"
        self._proof = ProofOfCompletion(config, db, llm, self.memory)
        self._verification_failures = 0  # Track consecutive verification fails

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
        self._reflection_count = 0
        start_time = time.time()
        consecutive_failures = 0
        total_failures = 0

        try:
            if self._learner:
                await self._learner.start()
            else:
                await self.browser.start()

            for step in range(self.max_steps):
                # Check cycle-level cancellation (watchdog timeout)
                if self._cycle_cancelled:
                    self._log_task(task, "cancelled", "Cycle cancelled by watchdog")
                    return {
                        "status": "cancelled",
                        "reason": "cycle_timeout",
                        "steps": step,
                        "history": self.action_history,
                    }

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

                # Mid-task reflection: max 2 per task, at failures 3 and 6
                if (total_failures >= 3 and total_failures % 3 == 0
                        and step > 0 and self._reflection_count < 2):
                    reflection = self._reflect_on_failures(task, context)
                    if reflection:
                        self._reflection_count += 1
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

                # Watchdog: detect stuck loops (same non-failing tool+args+result 4+ times)
                # Only fires when the LLM keeps doing the exact same thing with same outcome
                if len(self.action_history) >= 4 and not is_failure:
                    last4 = self.action_history[-4:]
                    sigs = [
                        (a["tool"], json.dumps(a["args"], sort_keys=True), a["result"][:200])
                        for a in last4
                    ]
                    if len(set(sigs)) == 1:
                        reason = (
                            f"Stuck loop: {tool} called 4 times with identical "
                            f"args and result — aborting to prevent waste"
                        )
                        logger.warning(reason)
                        self._log_task(task, "stuck_loop", reason)
                        return {
                            "status": "failed",
                            "reason": reason,
                            "steps": step + 1,
                            "history": self.action_history,
                        }

                # Check if task is done — but verify claims first
                if tool == "done":
                    verification = await self._verify_completion(
                        task, result, context, step)
                    if verification["verified"]:
                        self._log_task(task, "completed", result)
                        self._verification_failures = 0
                        return {
                            "status": "completed",
                            "result": result,
                            "steps": step + 1,
                            "proof": verification,
                        }
                    # Verification failed — inject feedback and keep going
                    self._verification_failures += 1
                    if self._verification_failures >= 3:
                        self._log_task(task, "failed",
                                       f"Claimed done 3x but verification failed: "
                                       f"{verification['reason']}")
                        return {
                            "status": "failed",
                            "reason": (
                                f"Task claimed complete but verification "
                                f"failed 3 times: {verification['reason']}"
                            ),
                            "steps": step + 1,
                            "proof": verification,
                        }
                    # Feed verification failure back into context so executor
                    # knows WHY its claim was rejected and can fix it
                    context = (
                        f"{context}\n\n"
                        f"VERIFICATION FAILED (attempt {self._verification_failures}/3): "
                        f"{verification['reason']}\n"
                        f"Your claim of completion was rejected because it could "
                        f"not be verified. Fix the issue and try again, or call "
                        f"fail() if the task genuinely cannot be completed."
                    )
                    # Don't record as done — loop continues
                    self.action_history.append({
                        "step": step + 1,
                        "tool": "done_rejected",
                        "args": args,
                        "result": f"VERIFICATION FAILED: {verification['reason']}",
                    })
                    continue
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

    def _get_executor_config(self, key: str, default: Any = None) -> Any:
        """Read executor-specific config from agent_config (set by self-improvement)."""
        try:
            rows = self.db.execute(
                "SELECT config_value FROM agent_config "
                "WHERE agent_name = 'executor' AND config_key = ?",
                (key,),
            )
            if rows:
                import json as _json
                try:
                    return _json.loads(rows[0]["config_value"])
                except (json.JSONDecodeError, TypeError):
                    return rows[0]["config_value"]
        except Exception:
            pass
        return default

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

        # Read deployed improvements for executor behavior
        custom_rules = self._get_executor_config("custom_rules", "")
        custom_rules_text = f"\nDEPLOYED RULES:\n{custom_rules}\n" if custom_rules else ""

        # Inject asset inventory so the LLM knows what resources are REAL
        try:
            asset_context = AssetManager(self.db).get_inventory().to_context() + "\n\n"
        except Exception:
            asset_context = ""

        prompt = (
            f"TASK: {task}\n\n"
            f"CONTEXT: {context}\n\n"
            f"STEP: {step + 1}/{self.max_steps}\n\n"
            f"PREVIOUS ACTIONS:\n{history_summary or 'None yet'}\n\n"
            f"{learned_context}"
            f"{asset_context}"
            f"{custom_rules_text}"
            f"{self._get_tool_descriptions()}\n\n"
            "Decide the next action. Return JSON: "
            '{"reasoning": "why this action", "tool": "tool_name", "args": {...}}\n\n'
            "CRITICAL RULES:\n"
            "- NEVER repeat a failed action with the same arguments\n"
            "- If a domain is blocked, try a DIFFERENT domain or approach\n"
            "- If 3+ actions have failed, call done() or fail() instead of burning more steps\n"
            "- read_page/screenshot do NOT count as progress — only use them when genuinely needed\n"
            "- Be efficient. Change strategy when things aren't working.\n"
            "- NEVER post to example.com or made-up/placeholder URLs — they don't exist\n"
            "- NEVER create accounts on platforms NOT mentioned in the task\n"
            "\n"
            "FORM INTERACTION STRATEGY:\n"
            "- fill_form has automatic self-healing and code-gen fallback — try it first\n"
            "- If fill_form STILL fails: use run_page_script to write custom JS that\n"
            "  interacts with the DOM directly. Read the page first to understand the\n"
            "  form structure, then write targeted code.\n"
            "- For React/Angular/Vue apps: use run_page_script with proper input events\n"
            "  (dispatchEvent, React's internal setter) — simple .value= won't work.\n"
            "- For multi-step wizards: write a script that clicks through steps AND fills fields.\n"
            "- You are a CODER. When standard tools fail, WRITE CODE to solve the problem.\n"
            "- NEVER run diagnostic loops (checking IP, proxy status, SSL) unless the task requires it\n"
            "- STAY ON TASK. If the task is 'register on X', only interact with X — not Y or Z\n"
            "- If the core action is impossible (site blocked, missing credentials), call fail() immediately"
        )

        # Temperature can be tuned by self-improvement experiments
        temperature = self._get_executor_config("temperature", 0.3)

        response = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    f"{CORE_DIRECTIVES}\n\n"
                    "You are an autonomous AI executor. You complete tasks by using tools. "
                    "Think step by step. Be resourceful and creative. "
                    "When registering on platforms, use ONLY the credentials provided in the "
                    "task context (email, password, name). NEVER invent, fabricate, or guess "
                    "credentials — if no email/password is provided, call fail() explaining "
                    "what's missing instead of making one up.\n"
                    "Always check results before proceeding. Take screenshots when unsure. "
                    "When writing code, use write_code tool — it generates AND tests code. "
                    "NEVER produce sloppy work. Everything must be production quality.\n\n"
                    "LEARN FROM FAILURES: When an action fails, analyze WHY and try a "
                    "fundamentally different approach. Do NOT just retry the same thing.\n"
                    "IMPORTANT: Always ATTEMPT the task before deciding it's impossible. "
                    "Navigate to the URL first — do NOT preemptively call fail() based on "
                    "learned lessons alone. Only call fail() after you've actually tried "
                    "and received a concrete error (e.g., 403, timeout, all proxies blocked)."
                )},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
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

        # 3. Blocked domains — NOTE: Do NOT inject this into the LLM prompt.
        # Domain blocks are transient (TTL-based) and the proxy fallback chain
        # handles them at runtime. Telling the LLM about blocks causes it to
        # preemptively call fail() at Step 1 without even trying to navigate.
        # The browser layer will raise AllProxiesBlockedError if truly blocked.

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
                    page_info = await self.browser.get_page_info()
                    # Self-healing: auto-solve CAPTCHA after click
                    failure = self._learner._detect_failure(page_info)
                    if failure == "captcha":
                        logger.info("CAPTCHA detected after click, auto-solving")
                        captcha_result = await self._learner._handle_captcha(domain)
                        if captcha_result.get("success"):
                            await asyncio.sleep(2)
                            page_info = await self.browser.get_page_info()
                    return page_info
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
                    skipped = result.get("skipped_fields", [])
                    failed = [k for k, v in result.get("fields", {}).items()
                              if not v.get("success") and not v.get("skipped")]
                    parts = []
                    filled_count = len(fields) - len(skipped) - len(failed)
                    if filled_count > 0:
                        parts.append(f"Filled {filled_count} fields")
                    if skipped:
                        parts.append(
                            f"Skipped fields not present on page: "
                            f"{', '.join(skipped)}. "
                            f"Do NOT include these fields in future fill_form calls"
                        )
                    if failed:
                        parts.append(
                            f"ERROR: Fill failed on: {', '.join(failed)}"
                        )
                    if not result.get("success") and not parts:
                        return "ERROR: Fill form failed"
                    return ". ".join(parts)
                else:
                    await self.browser.fill_form(fields)
                    return f"Filled {len(fields)} fields"

            elif tool == "run_page_script":
                script = args.get("script", "")
                script_args = args.get("args")
                if not script:
                    return "ERROR: script is required"
                if self._learner:
                    result = await self._learner.run_page_script(
                        script, args=script_args)
                    if result.get("success"):
                        return json.dumps(result.get("result", "Script executed"))
                    return f"ERROR: Script failed: {result.get('error', 'unknown')}"
                else:
                    # Fallback: execute directly on page
                    page = await self.browser._get_page()
                    try:
                        if script_args:
                            wrapped = f"async (args) => {{ {script} }}"
                            r = await page.evaluate(wrapped, script_args)
                        else:
                            wrapped = f"async () => {{ {script} }}"
                            r = await page.evaluate(wrapped)
                        return json.dumps(r) if r else "Script executed"
                    except Exception as e:
                        return f"ERROR: Script failed: {e}"

            elif tool == "submit":
                selector = args.get("selector", "form")
                page = await self.browser._get_page()
                # Try clicking submit/button inside the form first (more reliable)
                submit_clicked = False
                for btn_sel in [
                    f'{selector} [type="submit"]',
                    f'{selector} button[type="submit"]',
                    f'{selector} input[type="submit"]',
                    f'{selector} button:not([type="button"])',
                ]:
                    try:
                        btn = page.locator(btn_sel).first
                        if await btn.count() > 0:
                            await btn.click()
                            submit_clicked = True
                            break
                    except Exception:
                        continue
                if not submit_clicked:
                    # Fallback: JS submit — but check element exists first
                    try:
                        await page.evaluate(
                            f'(() => {{ const f = document.querySelector("{selector}"); '
                            f'if (f) f.submit(); else throw new Error("Form not found: {selector}"); }})()'
                        )
                    except Exception as e:
                        return f"ERROR: Submit failed: {e}"
                await asyncio.sleep(2)
                # Self-healing: check for CAPTCHA after submit
                page_info = await self.browser.get_page_info()
                if self._learner:
                    failure = self._learner._detect_failure(page_info)
                    if failure == "captcha":
                        logger.info("CAPTCHA detected after submit, auto-solving")
                        captcha_result = await self._learner._handle_captcha(
                            urlparse(page.url).netloc if hasattr(page, 'url') else "")
                        if captcha_result.get("success"):
                            await asyncio.sleep(2)
                            page_info = await self.browser.get_page_info()
                return page_info

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

            elif tool == "wait_for":
                selector = args.get("selector", "")
                timeout_s = min(args.get("timeout", 10), 30)
                await self.browser.wait_for(selector, timeout=timeout_s * 1000)
                return f"Element '{selector}' appeared"

            elif tool == "create_tool":
                return self._handle_create_tool(args)

            elif tool == "done":
                return args.get("result", "Task completed")

            elif tool == "fail":
                return args.get("reason", "Task failed")

            else:
                # Try custom tools before giving up
                if tool in self._custom_tools:
                    return await self._run_custom_tool(tool, args)
                return f"Unknown tool: {tool}"

        except Exception as e:
            logger.error(f"Tool {tool} failed: {e}")
            return f"ERROR: {e}"

    # ── Proof-of-Completion Verification ────────────────────────
    #
    # When the LLM calls done(), we don't just accept its word.
    # We verify the claimed outcome against reality:
    # 1. Extract claims from the done result
    # 2. Run verification checks (DB, API, screenshot)
    # 3. Accept only if at least one claim is verified
    # 4. Reject + feed back reason if verification fails

    async def _verify_completion(
        self, task: str, result: str, context: str, step: int
    ) -> dict[str, Any]:
        """Verify the executor's claim of completion before accepting it.

        Returns {"verified": True/False, "reason": str, "checks": [...]}.
        """
        try:
            # Get current browser page for screenshot-based verification
            page_url = None
            page_text = None
            try:
                page = await self.browser._get_page()
                page_url = page.url
                page_text = await self.browser.get_text()
            except Exception:
                pass

            return self._proof.verify(
                task=task,
                claimed_result=str(result),
                action_history=self.action_history,
                page_url=page_url,
                page_text=page_text,
            )
        except Exception as e:
            # If verification itself errors, log but don't block
            # (fail-open for the verification layer only)
            logger.warning(f"Proof-of-completion error (non-fatal): {e}")
            return {
                "verified": True,
                "reason": f"Verification skipped due to error: {e}",
                "checks": [],
            }

    # ── Dynamic Tool Factory ─────────────────────────────────────
    #
    # Security model (defense in depth):
    # 1. LLM code review — a second LLM call audits the code for risks
    # 2. Static blocklist — catches obvious dangerous patterns
    # 3. Subprocess sandbox — code runs in an isolated process via sandbox_run
    #    (bubblewrap > unshare > plain subprocess, same as shell tool)
    # 4. Timeout + memory limits — prevents resource exhaustion
    # 5. No object introspection leaks — subprocess has no access to
    #    executor internals, DB, browser, or HTTP client
    #
    # The old approach (exec with __builtins__={}) was bypassable via
    # Python introspection (e.g. ().__class__.__mro__[-1].__subclasses__()).
    # Subprocess isolation is the only real sandbox in Python.

    # Patterns that trigger immediate rejection (before LLM review)
    _TOOL_BLOCKLIST = [
        "import os", "import subprocess", "import shutil",
        "os.system", "subprocess.", "shutil.rmtree",
        "os.remove", "os.unlink", "os.rmdir",
        "__import__", "__subclasses__", "__mro__",
        "__class__.__bases__", "__globals__",
        "eval(", "exec(", "compile(",
        "open(", "open (",
        "requests.", "urllib.", "httpx.",
        "socket.", "import socket",
        "ctypes.", "import ctypes",
    ]

    # Max size for custom tool code (chars)
    _MAX_TOOL_CODE_SIZE = 5000
    # Subprocess timeout for custom tool execution (seconds)
    _TOOL_EXEC_TIMEOUT = 30

    def _load_custom_tools(self) -> None:
        """Load previously created custom tools from the database."""
        try:
            rows = self.db.execute(
                "SELECT tool_name, description, code, args_schema "
                "FROM custom_tools WHERE status = 'active'"
            )
            for row in rows:
                r = dict(row)
                self._custom_tools[r["tool_name"]] = {
                    "description": r["description"],
                    "code": r["code"],
                    "args_schema": r.get("args_schema", ""),
                }
        except Exception:
            pass  # Table might not exist yet

    def _llm_review_tool_code(self, name: str, description: str, code: str) -> tuple[bool, str]:
        """Use a second LLM call to audit custom tool code for security risks.

        Returns (is_safe, reason).
        """
        try:
            from monai.agents.ethics import (
                PRIVACY_RULES, SELF_IMPROVEMENT_RULES,
            )
            audit_model = self.llm.get_model(LLM.TIER_AUDIT)
            review = self.llm.chat(
                [
                    {"role": "system", "content": (
                        "You are a security and ethics auditor for an autonomous AI agent system. "
                        "Be extremely strict. The creator's safety and anonymity are paramount. "
                        "Any tool that could leak identity, bypass privacy protections, or violate "
                        "ethical boundaries MUST be rejected."
                    )},
                    {"role": "user", "content": (
                        f"SECURITY & ETHICS AUDIT — review this Python code that an AI agent "
                        f"wants to create as a reusable tool.\n\n"
                        f"Tool name: {name}\n"
                        f"Description: {description}\n"
                        f"Code:\n```python\n{code}\n```\n\n"
                        f"=== SECURITY CHECKS ===\n"
                        f"1. File system access (read/write/delete)\n"
                        f"2. Network access (HTTP, sockets, DNS)\n"
                        f"3. Process execution (shell, subprocess)\n"
                        f"4. Python sandbox escapes (__class__, __mro__, __subclasses__, "
                        f"__globals__, __builtins__, type(), getattr on dunder attrs)\n"
                        f"5. Privacy violations (accessing credentials, tokens, keys)\n"
                        f"6. Resource exhaustion (infinite loops, huge allocations)\n"
                        f"7. Code injection (eval, exec, compile)\n\n"
                        f"=== ETHICS CHECKS (MANDATORY) ===\n"
                        f"The tool MUST comply with these rules:\n"
                        f"{PRIVACY_RULES}\n"
                        f"{SELF_IMPROVEMENT_RULES}\n\n"
                        f"Specifically verify:\n"
                        f"8. No exposure of creator identity (IP, hostname, timezone, MAC)\n"
                        f"9. No bypassing proxy/Tor for network traffic\n"
                        f"10. No weakening of ethics rules or guardrails\n"
                        f"11. No unauthorized spending or financial commitments\n"
                        f"12. No deceptive practices or misrepresentation\n"
                        f"13. No accessing illegal content or services\n\n"
                        f"Reply with EXACTLY one line:\n"
                        f"SAFE: <reason> — if the code passes ALL security AND ethics checks\n"
                        f"UNSAFE: <reason> — if ANY security or ethics risk is found\n\n"
                        f"Be paranoid. When in doubt, say UNSAFE."
                    )},
                ],
                model=audit_model,
            )
            review = review.strip()
            if review.upper().startswith("SAFE"):
                return True, review
            return False, review
        except Exception as e:
            # If LLM review fails, reject the code (fail-closed)
            return False, f"LLM review failed: {e}"

    def _handle_create_tool(self, args: dict) -> str:
        """Create a new reusable tool at runtime.

        Security: LLM code review + static blocklist + subprocess sandbox.
        The tool code is a pure Python script that receives `args` as a JSON
        string via stdin and must print its result to stdout.
        """
        name = args.get("name", "").strip()
        description = args.get("description", "").strip()
        code = args.get("code", "").strip()

        if not name or not description or not code:
            return "ERROR: create_tool requires name, description, and code"

        if not name.isidentifier() or name in (
            "browse", "click", "type", "screenshot", "fill_form", "submit",
            "read_page", "http_get", "http_post", "shell", "write_file",
            "read_file", "write_code", "run_tests", "wait", "wait_for",
            "done", "fail", "create_tool",
        ):
            return f"ERROR: invalid or reserved tool name: {name}"

        if len(code) > self._MAX_TOOL_CODE_SIZE:
            return f"ERROR: tool code exceeds {self._MAX_TOOL_CODE_SIZE} char limit"

        # Layer 1: Static blocklist (fast, catches obvious stuff)
        code_lower = code.lower()
        for blocked in self._TOOL_BLOCKLIST:
            if blocked.lower() in code_lower:
                return f"BLOCKED: tool code contains forbidden pattern: {blocked}"

        # Layer 2: Validate the code compiles
        try:
            compile(code, f"<custom_tool:{name}>", "exec")
        except SyntaxError as e:
            return f"ERROR: syntax error in tool code: {e}"

        # Layer 3: LLM security review
        is_safe, review_reason = self._llm_review_tool_code(name, description, code)
        if not is_safe:
            logger.warning(f"Tool '{name}' rejected by LLM review: {review_reason}")
            return f"BLOCKED by security review: {review_reason}"

        # All checks passed — store
        self._custom_tools[name] = {
            "description": description,
            "code": code,
        }

        # Persist to database
        try:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS custom_tools ("
                "  tool_name TEXT PRIMARY KEY,"
                "  description TEXT NOT NULL,"
                "  code TEXT NOT NULL,"
                "  args_schema TEXT DEFAULT '',"
                "  status TEXT DEFAULT 'active',"
                "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            self.db.execute(
                "INSERT INTO custom_tools (tool_name, description, code) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(tool_name) DO UPDATE "
                "SET description = excluded.description, "
                "    code = excluded.code",
                (name, description, code),
            )
        except Exception as e:
            logger.warning(f"Could not persist custom tool '{name}': {e}")

        logger.info(f"Custom tool created: {name} — {description}")
        return f"Tool '{name}' created successfully. It is now available for use."

    async def _run_custom_tool(self, tool_name: str, args: dict) -> str:
        """Execute a custom tool in a sandboxed subprocess.

        The tool code runs as a standalone Python script in an isolated process:
        - Receives args as JSON via stdin
        - Prints result to stdout
        - No access to executor internals, DB, browser, or HTTP client
        - Killed after _TOOL_EXEC_TIMEOUT seconds
        - Uses the same sandbox_run infrastructure as the shell tool
        """
        tool_def = self._custom_tools.get(tool_name)
        if not tool_def:
            return f"ERROR: custom tool '{tool_name}' not found"

        code = tool_def["code"]

        # Wrap the tool code in a standalone script that reads args from stdin
        wrapper = (
            "import sys, json\n"
            "args = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}\n"
            "result = ''\n"
            f"{code}\n"
            "print(str(result))\n"
        )

        # Execute in sandboxed subprocess
        try:
            proc_result = sandbox_run(
                [sys.executable, "-c", wrapper],
                timeout=self._TOOL_EXEC_TIMEOUT,
            )
            stdout = proc_result.get("stdout", "").strip()
            stderr = proc_result.get("stderr", "").strip()
            returncode = proc_result.get("returncode", -1)

            if returncode != 0:
                error_msg = stderr[:500] if stderr else f"exit code {returncode}"
                logger.warning(f"Custom tool '{tool_name}' failed: {error_msg}")
                return f"ERROR: custom tool '{tool_name}' failed: {error_msg}"

            return stdout if stdout else "Tool executed (no output)"

        except Exception as e:
            logger.error(f"Custom tool '{tool_name}' execution error: {e}")
            return f"ERROR: custom tool '{tool_name}' failed: {e}"

    def _get_tool_descriptions(self) -> str:
        """Get full tool descriptions including custom tools."""
        desc = BUILTIN_TOOL_DESCRIPTIONS
        if self._custom_tools:
            custom_lines = []
            idx = 20  # Start numbering after builtins
            for name, tool_def in self._custom_tools.items():
                custom_lines.append(
                    f"{idx}. {name}(args) — {tool_def['description']}"
                )
                idx += 1
            if custom_lines:
                desc += "\nCustom tools (created by agent):\n"
                desc += "\n".join(custom_lines) + "\n"
        return desc

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
                "What fundamentally different approach should be tried?\n\n"
                "AVAILABLE TOOLS you can suggest:\n"
                "- browse(url): navigate to a page\n"
                "- click(selector): click an element\n"
                "- type(selector, text): type into a field\n"
                "- fill_form(fields): fill multiple fields at once\n"
                "- submit(selector): submit a form (clicks submit button)\n"
                "- wait_for(selector, timeout): wait for element to appear\n"
                "- wait(seconds): pause execution\n"
                "- screenshot(name): capture current page state\n"
                "- read_page(): get page text\n"
                "- http_get/http_post: direct API calls\n"
                "- shell(command): run shell commands\n\n"
                "Be specific and actionable — reference actual tools above. Max 3 sentences.",
                system="You analyze task execution failures and suggest alternative strategies using the available tools.",
            )
            logger.info(f"Mid-task reflection: {reflection[:200]}")
            return reflection
        except Exception as e:
            logger.debug(f"Reflection failed (non-fatal): {e}")
            return None

    def _post_task_learn(self, task: str, history: list[dict]) -> None:
        """After task completion/failure, analyze and produce ACTIONABLE changes.

        This is how the executor ACTUALLY improves — not just logging lessons,
        but producing concrete behavioral changes:
        1. Lessons → stored in SharedMemory → injected into future system prompts
        2. Domain blocklists → prevent retrying known-broken sites
        3. Successful patterns → stored as playbooks for future tasks
        4. Parameter adjustments → written to agent_config for next run
        """
        if not history or len(history) < 3:
            return

        failures = [a for a in history if a["result"].startswith("ERROR:")
                     or a["result"].startswith("BLOCKED")]
        successes = [a for a in history if not a["result"].startswith("ERROR:")
                      and not a["result"].startswith("BLOCKED")]
        failure_rate = len(failures) / len(history) if history else 0

        # ── ALWAYS learn from successes too ────────────────────────
        # Store successful tool sequences as playbooks
        if not failures and len(successes) >= 3:
            # Task completed cleanly — record the winning pattern
            tool_sequence = " → ".join(a["tool"] for a in history if a["tool"] not in ("done", "fail"))
            self.memory.store_knowledge(
                category="playbook",
                topic=f"successful_task_pattern",
                content=json.dumps({
                    "task_summary": task[:200],
                    "tool_sequence": tool_sequence,
                    "steps": len(history),
                }),
                source_agent="executor",
                confidence=0.8,
                tags=["playbook", "success"],
            )

        # Only deep-analyze tasks with significant failures
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
        failure_types: dict[str, int] = {}
        for a in failures:
            result = a["result"][:100]
            failure_types[result] = failure_types.get(result, 0) + 1

        top_failures = sorted(failure_types.items(), key=lambda x: -x[1])[:3]
        failure_text = "; ".join(f"{msg} (x{n})" for msg, n in top_failures)

        # ── Extract lesson AND concrete action ─────────────────────
        try:
            lesson_response = self.llm.quick(
                f"Task: {task[:200]}\n"
                f"Steps: {len(history)}, Failures: {len(failures)} ({failure_rate:.0%})\n"
                f"Top failures: {failure_text}\n"
                f"Failed domains: {', '.join(failed_domains) or 'N/A'}\n"
                f"Successful tools: {', '.join(a['tool'] for a in successes[:5])}\n\n"
                "Extract:\n"
                "LESSON: (1 sentence — what went wrong)\n"
                "RULE: (1 sentence — concrete rule to prevent this)\n"
                "ACTION: (1 sentence — specific parameter or approach change. "
                "E.g. 'increase timeout to 30s', 'try API before browser', "
                "'skip this domain', 'use different selector strategy')\n",
                system="You analyze task failures and produce actionable improvements.",
            )

            # Parse response
            parts = lesson_response.split("RULE:")
            lesson = parts[0].replace("LESSON:", "").strip()

            rule = ""
            action = ""
            if len(parts) > 1:
                rule_and_action = parts[1]
                if "ACTION:" in rule_and_action:
                    rule_parts = rule_and_action.split("ACTION:")
                    rule = rule_parts[0].strip()
                    action = rule_parts[1].strip()
                else:
                    rule = rule_and_action.strip()

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

            # ── Apply concrete action if identified ────────────────
            if action:
                self._apply_learned_action(action, failed_domains, failure_rate)

        except Exception as e:
            logger.debug(f"Post-task learning failed (non-fatal): {e}")

    def _apply_learned_action(
        self, action: str, failed_domains: set[str], failure_rate: float
    ) -> None:
        """Convert a learned action into a concrete behavioral change.

        This is what makes reflection REAL — not just words in a log,
        but actual changes to how the executor operates next time.
        """
        action_lower = action.lower()

        # NOTE: Do NOT store domain blocklists in the knowledge base.
        # Domain blocks are transient (proxy rotations, Tor circuit changes)
        # and the proxy fallback chain handles them at runtime with TTLs.
        # Permanently marking domains as "blocked" causes the executor to
        # preemptively fail on Step 1 without even trying to navigate.

        # Store as an executor-specific rule that gets injected into _think()
        try:
            existing_rules = ""
            try:
                rows = self.db.execute(
                    "SELECT config_value FROM agent_config "
                    "WHERE agent_name = 'executor' AND config_key = 'custom_rules'"
                )
                if rows:
                    existing_rules = rows[0]["config_value"]
                    try:
                        existing_rules = json.loads(existing_rules)
                    except (json.JSONDecodeError, TypeError):
                        pass
            except Exception:
                pass

            # Append new rule (cap at 10 rules to prevent bloat)
            if existing_rules:
                rules_list = existing_rules.split("\n") if isinstance(existing_rules, str) else [existing_rules]
            else:
                rules_list = []

            rules_list.append(f"- {action[:200]}")
            rules_list = rules_list[-10:]  # Keep last 10 rules

            new_rules = "\n".join(rules_list)
            self.db.execute(
                "INSERT INTO agent_config (agent_name, config_key, config_value, updated_at) "
                "VALUES ('executor', 'custom_rules', ?, datetime('now')) "
                "ON CONFLICT(agent_name, config_key) DO UPDATE "
                "SET config_value = excluded.config_value, updated_at = excluded.updated_at",
                (json.dumps(new_rules),),
            )
            logger.info(f"Applied learned action to executor config: {action[:100]}")
        except Exception as e:
            logger.debug(f"Could not store learned action (non-fatal): {e}")

    def _log_task(self, task: str, status: str, result: Any):
        self.db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details, result) VALUES (?, ?, ?, ?)",
            ("executor", f"task_{status}", task[:500], str(result)[:1000]),
        )
