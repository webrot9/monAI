"""Deploy the monAI crowdfunding landing page to free static hosting.

Supports Netlify, Vercel, and Cloudflare Pages via their CLIs.
Uses the executor to run deployment commands.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LANDING_DIR = Path(__file__).parent


class HostingProvider(Enum):
    NETLIFY = "netlify"
    VERCEL = "vercel"
    CLOUDFLARE = "cloudflare"


@dataclass
class DeployResult:
    success: bool
    url: str
    provider: str
    error: Optional[str] = None


def _check_cli(command: str) -> bool:
    """Check if a CLI tool is installed."""
    return shutil.which(command) is not None


def _run_command(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out after 120s"
    except FileNotFoundError:
        return 1, "", f"Command not found: {args[0]}"


def deploy_netlify(site_dir: Path, site_name: Optional[str] = None) -> DeployResult:
    """Deploy to Netlify using the netlify-cli.

    Requires: npm install -g netlify-cli && netlify login
    """
    if not _check_cli("netlify"):
        return DeployResult(
            success=False,
            url="",
            provider="netlify",
            error="netlify-cli not installed. Run: npm install -g netlify-cli",
        )

    args = ["netlify", "deploy", "--prod", "--dir", str(site_dir)]
    if site_name:
        args.extend(["--site", site_name])

    code, stdout, stderr = _run_command(args, cwd=site_dir)
    if code != 0:
        logger.error("Netlify deploy failed: %s", stderr)
        return DeployResult(
            success=False, url="", provider="netlify", error=stderr.strip()
        )

    # Parse URL from output — netlify prints "Website URL: https://..."
    url = ""
    for line in stdout.splitlines():
        if "Website URL:" in line or "Website Draft URL:" in line:
            url = line.split(":", 1)[-1].strip().lstrip(" ")
            break
        if line.strip().startswith("https://"):
            url = line.strip()
            break

    logger.info("Deployed to Netlify: %s", url)
    return DeployResult(success=True, url=url, provider="netlify")


def deploy_vercel(site_dir: Path, project_name: Optional[str] = None) -> DeployResult:
    """Deploy to Vercel using the vercel CLI.

    Requires: npm install -g vercel && vercel login
    """
    if not _check_cli("vercel"):
        return DeployResult(
            success=False,
            url="",
            provider="vercel",
            error="vercel CLI not installed. Run: npm install -g vercel",
        )

    args = ["vercel", "--prod", "--yes", str(site_dir)]
    if project_name:
        args.extend(["--name", project_name])

    code, stdout, stderr = _run_command(args, cwd=site_dir)
    if code != 0:
        logger.error("Vercel deploy failed: %s", stderr)
        return DeployResult(
            success=False, url="", provider="vercel", error=stderr.strip()
        )

    # Vercel prints the URL as the last line of stdout
    url = ""
    for line in reversed(stdout.strip().splitlines()):
        stripped = line.strip()
        if stripped.startswith("https://"):
            url = stripped
            break

    logger.info("Deployed to Vercel: %s", url)
    return DeployResult(success=True, url=url, provider="vercel")


def deploy_cloudflare(
    site_dir: Path, project_name: str = "monai-landing"
) -> DeployResult:
    """Deploy to Cloudflare Pages using wrangler.

    Requires: npm install -g wrangler && wrangler login
    """
    if not _check_cli("wrangler"):
        return DeployResult(
            success=False,
            url="",
            provider="cloudflare",
            error="wrangler CLI not installed. Run: npm install -g wrangler",
        )

    args = [
        "wrangler",
        "pages",
        "deploy",
        str(site_dir),
        "--project-name",
        project_name,
    ]

    code, stdout, stderr = _run_command(args, cwd=site_dir)
    if code != 0:
        logger.error("Cloudflare Pages deploy failed: %s", stderr)
        return DeployResult(
            success=False, url="", provider="cloudflare", error=stderr.strip()
        )

    # wrangler prints deployment URL
    url = ""
    combined = stdout + stderr  # wrangler sometimes outputs to stderr
    for line in combined.splitlines():
        stripped = line.strip()
        if "https://" in stripped and ".pages.dev" in stripped:
            # Extract the URL
            start = stripped.index("https://")
            url = stripped[start:].split()[0].rstrip(")")
            break

    logger.info("Deployed to Cloudflare Pages: %s", url)
    return DeployResult(success=True, url=url, provider="cloudflare")


def deploy(
    provider: str | HostingProvider = HostingProvider.NETLIFY,
    site_dir: Optional[Path] = None,
    site_name: Optional[str] = None,
) -> DeployResult:
    """Deploy the landing page to the specified hosting provider.

    Args:
        provider: One of 'netlify', 'vercel', 'cloudflare' (or HostingProvider enum).
        site_dir: Directory containing the built site. Defaults to this package's dir.
        site_name: Optional project/site name on the platform.

    Returns:
        DeployResult with success status and deployed URL.
    """
    if site_dir is None:
        site_dir = LANDING_DIR

    if isinstance(provider, str):
        try:
            provider = HostingProvider(provider.lower())
        except ValueError:
            return DeployResult(
                success=False,
                url="",
                provider=provider,
                error=f"Unknown provider '{provider}'. Use: netlify, vercel, cloudflare",
            )

    deployers = {
        HostingProvider.NETLIFY: lambda: deploy_netlify(site_dir, site_name),
        HostingProvider.VERCEL: lambda: deploy_vercel(site_dir, site_name),
        HostingProvider.CLOUDFLARE: lambda: deploy_cloudflare(
            site_dir, site_name or "monai-landing"
        ),
    }

    logger.info("Deploying to %s from %s", provider.value, site_dir)
    return deployers[provider]()


def deploy_with_executor(executor, provider: str = "netlify") -> DeployResult:
    """Deploy using a monAI executor agent for sandboxed execution.

    This is the preferred method when running inside the monAI system,
    as it goes through the executor's sandbox and logging.

    Args:
        executor: A monAI executor agent instance with execute_task().
        provider: Hosting provider name.

    Returns:
        DeployResult with the deployed URL.
    """
    install_commands = {
        "netlify": "npm install -g netlify-cli && netlify deploy --prod --dir .",
        "vercel": "npm install -g vercel && vercel --prod --yes .",
        "cloudflare": (
            "npm install -g wrangler && "
            "wrangler pages deploy . --project-name monai-landing"
        ),
    }

    if provider not in install_commands:
        return DeployResult(
            success=False,
            url="",
            provider=provider,
            error=f"Unknown provider: {provider}",
        )

    command = install_commands[provider]
    logger.info("Deploying via executor: %s", command)

    try:
        result = executor.execute_task(
            task=f"Deploy static site to {provider}",
            command=command,
            working_dir=str(LANDING_DIR),
        )
        # Extract URL from executor result
        url = ""
        if hasattr(result, "output") and result.output:
            for line in result.output.splitlines():
                if "https://" in line:
                    start = line.index("https://")
                    url = line[start:].split()[0].rstrip(")")
                    break

        return DeployResult(
            success=True,
            url=url,
            provider=provider,
        )
    except Exception as e:
        logger.error("Executor deploy failed: %s", e)
        return DeployResult(
            success=False,
            url="",
            provider=provider,
            error=str(e),
        )
