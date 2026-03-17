"""Code generation agent — writes, tests, and deploys code.

Agents use this when they need to build tools, scripts, websites,
integrations, or any code artifact. Enforces the "no AI slop" rule
by requiring tests that pass before code is considered complete.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

from monai.agents.ethics import CODE_RULES
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

# Where agents can write code
WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent / "workspace"


class Coder:
    """Writes code, writes tests, runs tests, fixes failures. No slop allowed."""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm
        WORKSPACE.mkdir(parents=True, exist_ok=True)

    def generate_module(self, spec: str, project_dir: str | None = None,
                        language: str = "python") -> dict[str, Any]:
        """Generate a code module from a specification.

        0. Ethics + legal review of the spec itself
        1. Generate the code
        1b. Ethics + legal review of the generated code
        2. Generate tests
        3. Run tests
        4. If failures: fix and re-test (up to 3 attempts)
        5. Return only if tests pass

        Args:
            spec: Natural language description of what to build
            project_dir: Where to write files (defaults to workspace/)
            language: Programming language (default python)
        """
        target_dir = Path(project_dir) if project_dir else WORKSPACE
        target_dir.mkdir(parents=True, exist_ok=True)

        self._log("generate_start", f"Generating module: {spec[:100]}")

        # Step 0: Ethics/legal review of the spec — catch bad intent early.
        # Use script_type="spec" so pattern checks are applied to intent
        # (not code syntax). Python patterns like 'requests.' or 'open('
        # must NOT trigger on natural language descriptions.
        from monai.agents.ethics import is_script_ethical
        is_ok, reason = is_script_ethical(
            spec, context=f"Code generation spec: {spec[:200]}",
            script_type="spec", llm=self.llm,
        )
        if not is_ok:
            self._log("ethics_blocked", f"Spec blocked: {reason}")
            return {
                "status": "blocked",
                "reason": f"Code generation blocked by ethics/legal review: {reason}",
            }

        # Step 1: Generate the code
        code_result = self._generate_code(spec, language)
        filename = code_result["filename"]
        code = code_result["code"]

        # Step 1b: Ethics/legal review of the GENERATED code
        script_type = "python" if language == "python" else "custom_tool"
        is_ok, reason = is_script_ethical(
            code, context=f"Generated module for: {spec[:200]}",
            script_type=script_type, llm=self.llm,
        )
        if not is_ok:
            self._log("ethics_blocked", f"Generated code blocked: {reason}")
            return {
                "status": "blocked",
                "reason": f"Generated code blocked by ethics/legal review: {reason}",
            }

        code_path = target_dir / filename
        code_path.write_text(code)
        self._log("code_written", str(code_path))

        # Step 2: Generate tests
        test_result = self._generate_tests(filename, code, spec, language)
        test_filename = test_result["filename"]
        test_code = test_result["code"]

        test_path = target_dir / test_filename
        test_path.write_text(test_code)
        self._log("tests_written", str(test_path))

        # Step 3: Run tests and fix
        for attempt in range(3):
            test_output = self._run_tests(test_path, target_dir)

            if test_output["passed"]:
                self._log("tests_passed", f"All tests passed on attempt {attempt + 1}")
                return {
                    "status": "success",
                    "code_path": str(code_path),
                    "test_path": str(test_path),
                    "attempts": attempt + 1,
                    "test_output": test_output["output"],
                }

            # Tests failed — fix the code
            self._log("tests_failed", f"Attempt {attempt + 1}: {test_output['output'][:500]}")
            fix = self._fix_code(code, test_code, test_output["output"], spec, language)

            if fix.get("fix_code"):
                code = fix["fix_code"]
                # Re-check ethics on fixed code
                is_ok, reason = is_script_ethical(
                    code, context=f"Fixed code for: {spec[:200]}",
                    script_type=script_type, llm=self.llm,
                )
                if not is_ok:
                    self._log("ethics_blocked", f"Fixed code blocked: {reason}")
                    return {
                        "status": "blocked",
                        "reason": f"Code fix blocked by ethics/legal review: {reason}",
                    }
                code_path.write_text(code)
            if fix.get("fix_tests"):
                test_code = fix["fix_tests"]
                test_path.write_text(test_code)

        # All attempts exhausted
        self._log("generation_failed", f"Could not get tests to pass after 3 attempts")
        return {
            "status": "failed",
            "code_path": str(code_path),
            "test_path": str(test_path),
            "attempts": 3,
            "last_error": test_output["output"],
        }

    def generate_script(self, spec: str, filename: str,
                        project_dir: str | None = None) -> dict[str, Any]:
        """Generate a standalone script (no separate test file, but self-testing)."""
        target_dir = Path(project_dir) if project_dir else WORKSPACE
        target_dir.mkdir(parents=True, exist_ok=True)

        # Ethics/legal review of the spec
        from monai.agents.ethics import is_script_ethical
        is_ok, reason = is_script_ethical(
            spec, context=f"Script generation: {spec[:200]}",
            script_type="python", llm=self.llm,
        )
        if not is_ok:
            self._log("ethics_blocked", f"Script spec blocked: {reason}")
            return {
                "status": "blocked",
                "reason": f"Script blocked by ethics/legal review: {reason}",
            }

        code_result = self.llm.chat(
            [
                {"role": "system", "content": (
                    f"{CODE_RULES}\n\n"
                    "You are an expert programmer. Write production-quality code. "
                    "Include a self-test section at the bottom (if __name__ == '__main__'). "
                    "The self-test should verify the script works correctly. "
                    "Return ONLY the code, no markdown fences."
                )},
                {"role": "user", "content": f"Write a script: {spec}"},
            ],
            model=self.config.llm.model,
            temperature=0.2,
        )

        # Clean markdown fences if present
        code = self._clean_code(code_result)

        # Ethics/legal review of the generated code
        is_ok, reason = is_script_ethical(
            code, context=f"Generated script: {spec[:200]}",
            script_type="python", llm=self.llm,
        )
        if not is_ok:
            self._log("ethics_blocked", f"Generated script blocked: {reason}")
            return {
                "status": "blocked",
                "reason": f"Generated script blocked by ethics/legal review: {reason}",
            }

        script_path = target_dir / filename
        script_path.write_text(code)

        # Run self-test
        test_output = self._run_script(script_path)
        return {
            "status": "success" if test_output["returncode"] == 0 else "error",
            "path": str(script_path),
            "output": test_output,
        }

    def review_code(self, code: str, context: str = "") -> dict[str, Any]:
        """Review code for quality, security, and correctness."""
        review = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    "You are a senior code reviewer. Be thorough and honest. "
                    "Check for: bugs, security issues, performance problems, "
                    "code quality, edge cases, error handling."
                )},
                {"role": "user", "content": (
                    f"Review this code:\n```\n{code}\n```\n"
                    f"Context: {context}\n\n"
                    "Return JSON: {\"quality_score\": int (1-10), "
                    "\"issues\": [{\"severity\": str, \"description\": str, \"line\": int}], "
                    "\"suggestions\": [str], \"approved\": bool}"
                )},
            ],
            temperature=0.2,
        )
        return review

    def _generate_code(self, spec: str, language: str) -> dict[str, str]:
        result = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    f"{CODE_RULES}\n\n"
                    f"You are an expert {language} developer. Write production-quality code. "
                    "No shortcuts. Handle edge cases. Proper error handling. "
                    "Return JSON: {\"filename\": str, \"code\": str}"
                )},
                {"role": "user", "content": f"Implement this:\n{spec}"},
            ],
            model=self.config.llm.model,
            temperature=0.2,
        )
        return result

    def _generate_tests(self, filename: str, code: str, spec: str,
                        language: str) -> dict[str, str]:
        result = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    f"{CODE_RULES}\n\n"
                    f"You are an expert {language} test engineer. Write thorough tests. "
                    "Test real behavior, not just that code runs. "
                    "Cover: happy paths, edge cases, error conditions, boundary values. "
                    "Use pytest for Python. "
                    "Return JSON: {\"filename\": str, \"code\": str}"
                )},
                {"role": "user", "content": (
                    f"Write comprehensive tests for this module.\n"
                    f"Filename: {filename}\n"
                    f"Spec: {spec}\n"
                    f"Code:\n```\n{code}\n```"
                )},
            ],
            model=self.config.llm.model,
            temperature=0.2,
        )
        return result

    def _fix_code(self, code: str, test_code: str, error_output: str,
                  spec: str, language: str) -> dict[str, str]:
        result = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    "You are debugging failing tests. Analyze the error, identify root cause, "
                    "and fix the code or tests as needed. "
                    "Return JSON: {\"diagnosis\": str, \"fix_code\": str or null, "
                    "\"fix_tests\": str or null}. "
                    "Only include fix_code or fix_tests if that file needs changes."
                )},
                {"role": "user", "content": (
                    f"Spec: {spec}\n\n"
                    f"Code:\n```\n{code}\n```\n\n"
                    f"Tests:\n```\n{test_code}\n```\n\n"
                    f"Error:\n{error_output[:2000]}"
                )},
            ],
            model=self.config.llm.model,
            temperature=0.2,
        )
        return result

    def _run_tests(self, test_path: Path, work_dir: Path) -> dict[str, Any]:
        from monai.utils.sandbox import sandbox_run
        # Always use "python3" from PATH — sys.executable may point to a
        # different venv than VIRTUAL_ENV, and bwrap only bind-mounts the
        # latter. PATH already includes $VIRTUAL_ENV/bin.
        result = sandbox_run(
            ["python3", "-m", "pytest", str(test_path), "-v", "--tb=short"],
            cwd=work_dir,
            timeout=60,
            allowed_paths=[work_dir],
        )
        return {
            "passed": result["returncode"] == 0,
            "output": result["stdout"] + result["stderr"],
            "returncode": result["returncode"],
        }

    def _run_script(self, script_path: Path) -> dict[str, Any]:
        from monai.utils.sandbox import sandbox_run
        return sandbox_run(
            ["python3", str(script_path)],
            cwd=script_path.parent,
            timeout=60,
            allowed_paths=[script_path.parent],
        )

    def _clean_code(self, code: str) -> str:
        """Remove markdown code fences if present."""
        code = code.strip()
        if code.startswith("```"):
            lines = code.split("\n")
            # Remove first line (```python or ```)
            lines = lines[1:]
            # Remove last line if it's ``
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)
        return code

    def _log(self, action: str, details: str):
        self.db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("coder", action, details),
        )
        logger.info(f"[coder] {action}: {details}")
